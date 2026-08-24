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

import asyncio
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from functools import partial

logger = logging.getLogger(__name__)

# 懒初始化单例：spawn 子进程启动有秒级成本（frozen exe 全量引导），
# 只在首次真正需要时创建；worker 常驻复用。max_workers=1 —
# 规范化是串行批处理，多 worker 只会增加内存峰值（每个子进程
# 独立加载 fitz）而无吞吐收益。
_pool: ProcessPoolExecutor | None = None
_pool_broken = False


def _pool_worker_init() -> None:
    """worker 初始化：父进程死亡联动退出守卫。

    e2e 实证（2026-08-24）：父进程被硬终止（TerminateProcess）后
    spawn worker 不随之退出 — 孤儿 pbc-server.exe 持续占用
    dist/pbc-server/pbc-server.exe 文件锁，导致后续 PyInstaller
    构建 PermissionError；生产场景 Electron 杀后端时同样残留。
    守卫线程阻塞在父进程 sentinel（Windows=进程句柄）上，父进程
    一退出立即 os._exit —— 不等队列 EOF（实测不触发/迟滞）。
    """
    import threading

    parent = multiprocessing.parent_process()
    if parent is None:  # 直接运行（非 spawn worker）— 无需守卫
        return

    def _watch() -> None:
        try:
            from multiprocessing.connection import wait

            wait([parent.sentinel])
            # 父进程已退出：立刻退出 worker，不清理（无共享状态需保序）
            import os

            os._exit(0)
        except Exception:
            pass  # 守卫失败不影响 worker 正常功能

    threading.Thread(target=_watch, daemon=True, name="parent-death-watch").start()


def _get_pool() -> ProcessPoolExecutor | None:
    """惰性创建进程池；失败（frozen 环境限制等）只告警一次并返回 None。"""
    global _pool, _pool_broken
    if _pool is not None:
        return _pool
    if _pool_broken:
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
    import pickle

    try:
        pickle.dumps(fn)
    except Exception:
        return False
    return True


async def run_cpu(fn, *args, label: str = ""):
    """在子进程中执行 CPU 密集函数，返回其结果。

    优先级：进程池（GIL 隔离）→ asyncio.to_thread（回退）。
    子进程内异常原样冒泡（与 to_thread 语义一致，调用方已有
    try/except 规范化失败处理）。
    """
    pool = _get_pool() if _is_picklable(fn) else None
    if pool is None:
        if label:
            logger.info(f"CPU task {label}: running in thread (fallback)")
        return await asyncio.to_thread(fn, *args)
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(pool, partial(fn, *args))
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
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
