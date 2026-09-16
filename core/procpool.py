"""CPU-bound 重活子进程执行器（GIL 隔离）。

背景（e2e 实证 2026-08-24，job 0e5eb863）：138MB/51 页扫描件的
Stage 0 规范化（3000x4000pt 异常页面盒 → 300dpi 重渲染）通过
asyncio.to_thread 执行时，PyMuPDF 的 C 调用（get_pixmap /
insert_image / save(garbage=4)）长时间持有 GIL —— 事件循环与
aiosqlite 工作线程被饿死，正常毫秒级的 DB 操作退化到 1s+/条，
GET /api/jobs/{id} 在 10s 内无法完成（ReadTimeout），SSE 同样停摆。

修复：此类批量 CPU 重活改投 spawn 子进程（独立 GIL），主进程
事件循环保持满速。线程池保留为回退路径（函数不可 pickle —
测试替身/动态补丁 — 或进程池启动失败时），行为退化为旧版
（阻塞但可用），不阻断流程。
"""
from __future__ import annotations

import atexit
import asyncio
import logging
import multiprocessing
import os
import pickle
from concurrent.futures import ProcessPoolExecutor
from functools import partial

logger = logging.getLogger(__name__)

# 懒初始化单例：spawn 子进程启动有秒级成本（frozen exe 全量引导），
# 只在首次真正需要时创建；worker 常驻复用。max_workers=1 —
# 规范化是串行批处理，多 worker 只会增加内存峰值（每个子进程
# 独立加载 fitz）而无吞吐收益。
_pool: ProcessPoolExecutor | None = None
_pool_broken = False


def _pool_worker_init() -> None:  # pragma: no cover - 仅 spawn worker 子进程内执行
    """worker 初始化：std 流重定向 + 父进程死亡联动退出守卫。

    std 流重定向（2026-08-24 pytest 实证）：spawn worker 继承父进程
    stdout/stderr 管道句柄 — 父进程退出后管道 EOF 不关闭，包裹
    shell（CI/测试 harness）永久等待"挂起"；worker 内日志/打印
    也会与父进程输出交错。重定向 devnull（worker 失败经 future
    异常回传，不依赖 std 流）。

    父进程死亡守卫（e2e 实证 2026-08-24）：父进程被硬终止
    （TerminateProcess）后 spawn worker 不随之退出 — 孤儿
    pbc-server.exe 持续占用 dist/pbc-server/pbc-server.exe 文件锁，
    导致后续 PyInstaller 构建 PermissionError；生产场景 Electron
    杀后端时同样残留。守卫线程阻塞在父进程 sentinel（Windows=
    进程句柄）上，父进程一退出立即 os._exit —— 不等队列 EOF
    （实测不触发/迟滞）。
    """
    import threading

    # 1) std 流与父进程解耦（防管道 EOF 持有 + 输出交错）
    try:
        devnull = open(os.devnull, "w", encoding="utf-8")
        import sys

        sys.stdout = devnull
        sys.stderr = devnull
    except Exception:
        pass  # 重定向失败不影响核心功能

    parent = multiprocessing.parent_process()
    if parent is None:  # 直接运行（非 spawn worker）— 无需守卫
        return

    def _watch() -> None:
        try:
            from multiprocessing.connection import wait

            wait([parent.sentinel])
            # 父进程已退出：立刻退出 worker，不清理（无共享状态需保序）
            os._exit(0)
        except Exception:
            pass  # 守卫失败不影响 worker 正常功能

    threading.Thread(target=_watch, daemon=True, name="parent-death-watch").start()


def _in_pytest() -> bool:
    """pytest 运行环境检测（测试不需要进程隔离，spawn 只引入不确定性）。"""
    return "PYTEST_CURRENT_TEST" in os.environ


def _get_pool() -> ProcessPoolExecutor | None:
    """惰性创建进程池；失败（frozen 环境限制等）只告警一次并返回 None。"""
    global _pool, _pool_broken
    if _pool is not None:
        return _pool
    if _pool_broken or _in_pytest():
        return None
    try:
        ctx = multiprocessing.get_context("spawn")
        _pool = ProcessPoolExecutor(
            max_workers=1, mp_context=ctx, initializer=_pool_worker_init
        )
        logger.info("CPU process pool ready (spawn, max_workers=1)")
        return _pool
    except Exception as e:  # pragma: no cover - 环境相关
        _pool_broken = True
        logger.warning(f"Process pool unavailable, CPU tasks fall back to threads: {e}")
        return None


def _is_picklable(fn) -> bool:
    """模块级真实函数可 pickle；测试替身（Mock/lambda/局部补丁）不可。"""
    try:
        pickle.dumps(fn)
    except Exception:
        return False
    return True


# 单任务超时（秒）。默认 30 分钟 —— 这不是"性能限制"而是"永久等待兜底"：
# 本机实测 138MB/51 页的 Stage 0 规范化在百秒量级，30 分钟留足一个数量级
# 余量；一旦超过，几乎必然是 worker 挂死（fitz 处理畸形页面盒/severe OOM
# 后无响应/worker 进程僵死），而非"任务真慢"。可用环境变量覆盖。
# 为什么必须有它：run_in_executor / to_thread 的等待**没有内建上限**——
# 唯一的真实性"永久非终态"入口（外部 HTTP 调用均有 timeout：LLM 180s、
# MinerU 60/300s、Paddle 轮询封顶 3600s，唯有此处原本无上限）。
_DEFAULT_TIMEOUT_S = float(os.getenv("PBC_CPU_TASK_TIMEOUT_S", "1800"))


def _recycle_pool(reason: str) -> None:
    """丢弃当前进程池并尽力终止其 worker（超时路径专用）。

    为什么必须回收而不能只超时返回：``max_workers=1`` 的池里，一个挂死的
    worker 会**永久占住唯一槽位** —— 后续所有 job 的 Stage 0 提交都会排队，
    表现从"某个 job 卡住"扩散成"整个应用不再处理新任务"。仅让调用方不再
    等待（future.cancel）**不会**释放槽位，故必须重建池。

    ``shutdown(wait=False)`` 默认不杀在跑的 worker（且被 cancel 的 future
    所对应的 worker 仍在消耗 CPU），因此额外尽力 kill —— 失败只记日志，
    不影响"新池可建"这一核心目标。
    """
    global _pool
    pool, _pool = _pool, None
    if pool is None:  # pragma: no cover - 调用点保证非空
        return
    for proc in list(getattr(pool, "_processes", {}).values()):
        try:
            if proc.is_alive():
                proc.kill()
        except Exception:  # pragma: no cover - 句柄失效/已退出
            pass  # 已退出/句柄失效 — 回收目的已达成
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception as e:  # pragma: no cover - 退出路径防御
        logger.warning(f"Process pool shutdown raised during recycle: {e}")
    logger.warning(f"CPU process pool recycled ({reason}) — a fresh pool will be created")


async def run_cpu(fn, *args, label: str = "", timeout: float | None = None):
    """在子进程中执行 CPU 密集函数，返回其结果。

    优先级：进程池（GIL 隔离）→ asyncio.to_thread（回退）。
    子进程内异常原样冒泡（与 to_thread 语义一致，调用方已有
    try/except 规范化失败处理）。

    ``timeout``：单任务等待上限（秒），默认 :data:`_DEFAULT_TIMEOUT_S`。
    超时后**回收进程池**并抛 :class:`TimeoutError` —— 让 pipeline 走正常
    error 路径（job 进入终态、用户可重试），而不是让 job 永久停在
    非终态、SSE 无限等待。传 ``0`` 或负数表示不设限（仅测试用）。
    """
    limit = _DEFAULT_TIMEOUT_S if timeout is None else timeout
    pool = _get_pool() if _is_picklable(fn) else None
    if pool is None:
        if label and _in_pytest():
            logger.debug(f"CPU task {label}: running in thread (pytest mode)")
        coro = asyncio.to_thread(fn, *args)
        if limit and limit > 0:
            try:
                return await asyncio.wait_for(coro, limit)
            except asyncio.TimeoutError:
                # 线程无法强杀（Python 限制）— 只能止损：不再等待，抛错让
                # 上层走 error 路径。线程泄漏一个，但 job 不再永久非终态。
                raise TimeoutError(
                    f"CPU task {label or fn.__name__} timed out after {limit:g}s "
                    f"(thread fallback; thread is leaked — cannot be killed)"
                ) from None
        return await coro
    loop = asyncio.get_running_loop()
    try:
        fut = loop.run_in_executor(pool, partial(fn, *args))
        if limit and limit > 0:
            return await asyncio.wait_for(fut, limit)
        return await fut
    except asyncio.TimeoutError:
        _recycle_pool(f"{label or fn.__name__} timed out after {limit:g}s")
        raise TimeoutError(
            f"CPU task {label or fn.__name__} timed out after {limit:g}s "
            f"(process pool recycled)"
        ) from None
    except (pickle.PicklingError, AttributeError, TypeError) as e:
        # 提交期 pickling 失败（如 partial 包装后仍不可序列化）—
        # 回退线程执行，不丢失任务。
        logger.warning(
            f"CPU task {label}: process submit failed ({e}), falling back to thread"
        )
        return await asyncio.to_thread(fn, *args)


def shutdown_pool() -> None:
    """优雅关闭进程池（应用退出时调用；未启动则无操作）。"""
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # pragma: no cover - 退出路径防御
            pass


# 解释器退出时自动关池（早于非 daemon 线程 join）：显式发送 shutdown
# 哨兵，避免依赖 concurrent.futures 全局退出钩子在异常状态下的不确定
# join 行为（pytest 全量套件实证挂起）。
atexit.register(shutdown_pool)
