"""运行时看门狗：把运行期间"永久非终态"的 job 自动收敛为 error。

## 为什么需要（`docs/RUNTIME_WATCHDOG.md` 的结论）

pipeline 与 API **同进程同事件循环**，而 `recover_stuck_jobs` 只在启动
lifespan 调一次 —— 不重启应用就没有任何兜底。一个卡死的 job 会永久停在
非终态，而 SSE（`api/jobs/status.py` 的 `while True`）只认终态，
于是用户看到"进度条永远停住、无提示、无出路"，只能重启应用。

本模块补上"运行期间"的那一半：周期性扫描非终态 job，按 `last_activity_at`
判定停滞，复用 `recover_stuck_jobs` 同款动作收敛（error + 审计 + 通知）。

## 判据（宁缺勿错）

1. **只认 `jobs.last_activity_at`** —— 它由 `state.touch_activity` 写在
   "确实往前走了一步"的位置（状态迁移 / OCR 进度 / 自愈进度 / 跨页进度 /
   单页分析完成）。**心跳绑定前进，不挂定时器**：定时器在 stall 期间照样跳，
   看门狗就永远不会触发（业界共识，见方案文档 §4）。
2. **`NULL` 一律跳过** —— 迁移前的老行、异常写入的行，无法判定就不判。
   绝不用 `created_at` 兜底：那会把一个正常跑了很久的大文档误判成停滞。
3. **阈值按状态分级**：OCR 与逐页 LLM 的合理耗时差一个数量级，同一个阈值
   必然误杀其一。
4. **`pending` 默认不监视**。启动时 `pending` 确实意味着"进程死在起飞前"
   （故 `recover_stuck_jobs` 认它）；但**运行期间 `pending` 是"已建单、pipeline
   尚未推进"的过渡态**：上传与重试都是**先写 `pending`、紧接着 `launch_pipeline`**
   （`upload.py` / `actions.retry_job`），中间只有一个毫秒级窗口。
   把它当停滞 = 误杀刚上传的任务。真正崩掉的 pending 由启动恢复兜底。
   **唯一例外（第二轮回审新增）**：`pending` 且**已被 pipeline 接管**
   （`core.pipeline.locks._pipeline_tasks` 里有未完成的 task）—— 那说明
   pipeline 已经拿到 task 却推进不动（典型是卡在 per-job 锁上，见
   `_terminate_pipeline_task`），此时它不再是过渡态。
   **注**：`MAX_CONCURRENT_JOBS` 是**拒绝**（upload/retry 直接 409）而**不是排队**，
   所以运行期不存在"排队等槽位"的 pending。原判据把它写成"合法排队态"
   与代码不符，2026-09-16 回审时按实际机制更正（结论不变：仍不默认监视）。
5. **收敛动作必须包含"终止孤儿 task"**（第二轮回审新增）。只改状态不够 ——
   见 `_terminate_pipeline_task` 的说明。

## 阈值不变式（第二轮回审新增，机检于 test_watchdog.py）

**基准阈值必须 ≥ 它覆盖的那次上游调用自己的封顶 + 余量。**
否则看门狗会抢在上游超时**之前**，把"上游还在正常等待"判成停滞 —— 这是
假阳性，比晚判几分钟糟得多（本模块的定位是抓"永久停滞"，不是绩效考核）。

OCR 基准取 4200s 的依据（旧值 1800s 是错的）：
空页自愈在**每次探测尝试结束**与**每次拥塞退避开始前**都写心跳
（`self_heal._probe_slice_angle` / `_report_heal_progress`），故心跳缺口
**不跨尝试累加**，上界就是 `self_heal.rotation_silence_bound_s()`
= max(单次尝试 2 × 单页封顶 630s, 单次退避封顶 300s) = **1260s**。

> 2026-09-16 重新推导（缺陷 #120）：此前写法以「3 个候选角 × 630s ≈ 1890s」
> 当上界 —— 既**漏了每角度 2 次尝试**（真实乘积是 3780s），又在自愈改为
> 「逐次尝试写心跳 + 拥塞退避」之后**不再成立**（缺口不再跨尝试累加）。
> 现在这个量由 `rotation_silence_bound_s()` 单一提供，机检断言它 ≤ 本阈值，
> 不再手写推导（手写推导正是它一度算错的原因）。

`cancelling` 基准取 2400s 的依据（旧值 900s 同样是错的，同一条不变式）：
取消检查点只在 `run_cpu` **前后**（`stage1.py:49/54`），所以取消要等 Stage 0
规范化收尾，而那一步的封顶是 `PBC_CPU_TASK_TIMEOUT_S`（默认 1800s）。
旧阈值 900s → 看门狗会把"已请求取消、正在等 Stage 0 收尾"的 job 判成停滞
并改成 `error`，而 `engine.py` 明确禁止覆盖取消语义（"cancelled 被改成 error
会破坏取消审计链，通知也会重发"）。"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from db.client import get_db
from core.pipeline.state import _STUCK_STATUSES
# 上游封顶的**单一真值**：OCR 轮询上限 + 本地 CPU 重活封顶。看门狗基准必须
# 压在它们之上，否则会抢在上游自己超时前误判停滞（见模块 docstring 的不变式）。
from core.ocr_client import POLL_TIMEOUT_MAX
from core.procpool import cpu_task_timeout_seconds

logger = logging.getLogger(__name__)

_TS_FMT = "%Y-%m-%d %H:%M:%S"

# 预筛集（SQL）：全部非终态。最终判定在 Python —— `pending` 需先看注册表
# 才知道是不是"已被接管"（判据 4），SQL 层拿不到这个信息。
_QUERY_STATUSES: tuple[str, ...] = tuple(_STUCK_STATUSES)

# 监视范围：_STUCK_STATUSES 去掉 pending（见模块 docstring 判据 4）。
# 用元组推导而不是重新硬编码一份状态列表 —— 状态串在本项目已有 4 处
# 独立硬编码的历史（docs/ADVERSARIAL_AUDIT.md §2 P1），不再加第 5 处。
_WATCHDOG_STATUSES: tuple[str, ...] = tuple(s for s in _STUCK_STATUSES if s != "pending")

# 基准停滞阈值（秒）
_STALL_MARGIN_S = 600.0
# OCR 基准 = 上游单次封顶 + 余量。**不要**凭手感改小：见 docstring 不变式。
_OCR_BASE_STALL_S = float(POLL_TIMEOUT_MAX) + _STALL_MARGIN_S     # 3600 + 600
# 取消基准 = 本地 CPU 重活封顶 + 余量。取消检查点只在 `run_cpu` 前后
# （stage1.py:49/54），所以取消要等 Stage 0 收尾 —— 若阈值低于该封顶，
# 看门狗会把"已请求取消、正在收尾"的 job 从 cancelled 改成 error
# （破坏取消审计链，且通知重发；engine.py 的终态语义注释明确禁止）。
# 这也是"取消响应性"的固有上限：进程池隔离决定了它无法在跑到一半时被打断。
_CANCEL_BASE_STALL_S = cpu_task_timeout_seconds() + _STALL_MARGIN_S   # 1800 + 600
# pending 被接管（有活 task）后的阈值：正常它只停留到第一次状态迁移（毫秒级），
# 900s 已极度宽松；真正卡住的是下面那条"孤儿持锁 → retry 永远排队"的场景。
_PENDING_TAKEOVER_STALL_S = 900.0

_BASE_STALL_S = {
    "ocr_running": _OCR_BASE_STALL_S,
    "ocr_done": 1800.0,      # 纯过渡态，正常停留不到 1s，留足余量
    "analyzing": 1800.0,     # 逐页 LLM：单次适配器超时 180s，逐页写心跳
    "cancelling": _CANCEL_BASE_STALL_S,
    "pending": _PENDING_TAKEOVER_STALL_S,   # **仅**在有活 task 时生效（判据 4）
}

# 每页增量（秒）：OCR 要覆盖旋转自愈（每页最多 2 角度 × 2 重试的完整上游任务），
# 逐页 LLM 用 180s/页（即 LLM 适配器单次超时）作为上界估计。
_PER_PAGE_S = {
    "ocr_running": 120.0,
    "analyzing": 180.0,
}

# 封顶（秒）：51 页上限下 OCR 10320s / LLM 10800s，均远超实测值。
_CAP_S = {
    "ocr_running": 10800.0,
    "analyzing": 10800.0,
}

# 终止孤儿 task 的等待上限：取消后 pipeline 要跑 finally（flush 进度 future
# ≤1s + 注销注册表 + 写日志），10s 已远超需要；超时说明它已不可取消。
_TERMINATE_TIMEOUT_S = 10.0

AUDIT_ACTION = "watchdog_stall_recovery"

# 自述状态（供 /api/health/watchdog 暴露）。看门狗**自己挂掉**比 job 卡死
# 更糟 —— 用户会以为"有兜底"而不再手动重试，所以巡检是否还活着必须可见。
_STATE: dict = {
    "running": False,
    "started_at": None,
    "last_scan_at": None,
    "last_scan_error": None,
    "last_stalled_found": 0,
    "last_recovered": 0,
    "total_scans": 0,
    "total_recovered": 0,
}


def enabled() -> bool:
    """看门狗总开关（`PBC_WATCHDOG_ENABLED=0` 可关，测试/排障用）。"""
    return os.getenv("PBC_WATCHDOG_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def scan_interval_seconds() -> float:
    """扫描周期（`PBC_WATCHDOG_INTERVAL_S`，下限 5s 防配置成 0 忙等）。"""
    try:
        return max(5.0, float(os.getenv("PBC_WATCHDOG_INTERVAL_S", "60")))
    except (TypeError, ValueError):
        return 60.0


def _scale() -> float:
    """全局缩放系数（`PBC_WATCHDOG_SCALE`）—— 现场可按机器/上游拥堵临时放宽。"""
    try:
        return max(0.1, float(os.getenv("PBC_WATCHDOG_SCALE", "1")))
    except (TypeError, ValueError):
        return 1.0


def is_watched(status: str, *, has_live_task: bool = False) -> bool:
    """该状态是否纳入判定。

    ``has_live_task``：该 job 在 pipeline 注册表里是否有未完成的 task。
    只有 `pending` 会用到它（判据 4 的唯一例外）。
    """
    if status in _WATCHDOG_STATUSES:
        return True
    return status == "pending" and has_live_task


def stall_limit_seconds(
    status: str, total_pages: int | None, *, has_live_task: bool = False,
) -> float:
    """该状态 + 页数下的停滞阈值（秒）。未监视的状态返回 `inf`（永不判定）。

    纯函数：便于单测直接锁定数值契约，无需起 DB。
    """
    if not is_watched(status, has_live_task=has_live_task):
        return float("inf")
    base = _BASE_STALL_S[status]
    pages = max(0, int(total_pages or 0))
    limit = base + _PER_PAGE_S.get(status, 0.0) * pages
    cap = _CAP_S.get(status)
    if cap is not None:
        limit = min(limit, cap)
    return limit * _scale()


def elapsed_seconds(last_activity_at: str, now: datetime | None = None) -> float | None:
    """`last_activity_at`（localtime 字符串）距今秒数；无法解析返回 None。

    全部走 Python 侧解析而非 SQL `julianday`：口径与写入端
    `datetime('now','localtime')` 一致（避免 UTC/localtime 混用算错偏移），
    且判定逻辑可被单测直接覆盖。
    """
    if not last_activity_at:
        return None
    try:
        ts = datetime.strptime(str(last_activity_at).strip(), _TS_FMT)
    except (TypeError, ValueError):
        return None
    ref = now or datetime.now()
    return (ref - ts).total_seconds()


# 停滞预警比例（P2，docs/RUNTIME_WATCHDOG.md §8.7）：空闲达到基准阈值的该
# 比例时，界面先**告知**用户「任务无进展，可取消或重试」，而不是让他一直
# 等到看门狗杀任务。阈值经两轮抬高（1800→4200s）后，这一层从"可选"变成
# "应当做"：否则在收到任何反馈前最长要等约 70 分钟。
_STALL_WARN_FRACTION = 0.6


def stall_report(
    status: str, total_pages: int | None, last_activity_at: str | None,
    *, has_live_task: bool = False, now: datetime | None = None,
) -> dict | None:
    """该 job 的停滞可见性 —— **纯派生**，不写库、不改 schema。

    返回 None 表示"不适用"（状态不在监视范围，或没有可用的心跳）。
    否则返回：

    - ``idle_seconds``：距最后一次真实推进的秒数
    - ``limit_seconds``：该状态 + 页数下的停滞阈值（看门狗收敛动作的触发点）
    - ``warn``：空闲 ≥ 60% 阈值 —— UI 据此提示"可取消或重试"
    - ``overdue``：空闲 ≥ 阈值（看门狗即将/已经收敛为 error）

    与 `stall_limit_seconds` 共用**同一份**阈值表（含 `_scale()` 现场缩放）——
    调用方**不得**另算一套阈值，两处阈值必然漂移（本项目反复踩过）。
    纯函数（`now` 可注入），无需起 DB 即可单测。
    """
    if not is_watched(status, has_live_task=has_live_task):
        return None
    idle = elapsed_seconds(last_activity_at, now=now)
    if idle is None:
        return None
    limit = stall_limit_seconds(status, total_pages, has_live_task=has_live_task)
    if limit == float("inf"):  # 未监视：正常不会走到（is_watched 已挡）
        return None
    return {
        "idle_seconds": int(idle),
        "limit_seconds": int(limit),
        "warn": idle >= limit * _STALL_WARN_FRACTION,
        "overdue": idle >= limit,
    }


def _live_pipeline_tasks() -> dict:
    """注册表快照（job_id → 未完成的 task）。运行时解析以便测试替换。"""
    from core.pipeline.locks import _pipeline_tasks

    return {jid: t for jid, t in list(_pipeline_tasks.items()) if not t.done()}


def _local_now_str() -> str:
    return datetime.now().strftime(_TS_FMT)


async def find_stalled_jobs(*, now: datetime | None = None) -> list[dict]:
    """返回停滞超过阈值的非终态 job（**只读，不改状态** —— 便于单测与预演）。

    每项含 `id / status / filename / total_pages / last_activity_at /
    elapsed_s / limit_s / has_live_task`。
    """
    db = await get_db()
    placeholders = ",".join("?" * len(_QUERY_STATUSES))
    cursor = await db.execute(
        f"SELECT id, status, filename, total_pages, last_activity_at FROM jobs "
        f"WHERE status IN ({placeholders}) AND last_activity_at IS NOT NULL",
        _QUERY_STATUSES,
    )
    rows = await cursor.fetchall()
    live = _live_pipeline_tasks()

    stalled: list[dict] = []
    for row in rows:
        status = row["status"]
        has_live = row["id"] in live
        if not is_watched(status, has_live_task=has_live):
            continue
        elapsed = elapsed_seconds(row["last_activity_at"], now)
        if elapsed is None:
            # 时间戳不可解析 → 不可判定，跳过（绝不猜）
            logger.debug(f"[{row['id']}] last_activity_at 无法解析，跳过判定")
            continue
        limit = stall_limit_seconds(status, row["total_pages"], has_live_task=has_live)
        if elapsed > limit:
            stalled.append({
                "id": row["id"],
                "status": status,
                "filename": row["filename"] if "filename" in row.keys() else "?",
                "total_pages": row["total_pages"] if "total_pages" in row.keys() else None,
                "last_activity_at": row["last_activity_at"],
                "elapsed_s": elapsed,
                "limit_s": limit,
                "has_live_task": has_live,
            })
    return stalled


async def _terminate_pipeline_task(job_id: str) -> str:
    """取消该 job 的孤儿 pipeline task 并等待其收尾；返回处置结论。

    为什么**必须**做（2026-09-16 第二轮回审抓到，属设计缺陷而非遗漏）：
    `engine.run_pipeline` 用 **per-job 锁**串行同一 job 的 pipeline，而
    `retry` 端点把 `error → pending` 后调用 `launch_pipeline`。若孤儿 task
    仍持有那把锁，retry 会**永远**停在 `async with lock`（该等待点没有任何
    上限），而 `pending` 默认不被监视 → 用户照着看门狗的提示点了"重试"，
    换来的却是一个永久无声的 pending。**看门狗自己的恢复动作，制造了它
    本要消灭的那种状态。**

    除锁之外，孤儿还会继续写 `ocr_progress` / `last_activity_at` /
    `page_cache`（这些 UPDATE 都不带状态条件）：重试轮的心跳会被孤儿的
    写入刷新成"在动"，既污染数据，也可能**掩盖重试轮的真正停滞**。

    与 `api/jobs/shutdown_endpoint` 的取消同款，只是作用域收窄到单个 job。
    用 `asyncio.wait` 而非直接 `await task`：后者会把 task 的 CancelledError
    抛进调用方，而调用方（`watchdog_loop`）绝不能因单个 job 而中断 ——
    看门狗自己挂掉比 job 卡死更糟。
    """
    task = _live_pipeline_tasks().get(job_id)
    if task is None:
        return "no-task"
    task.cancel()
    try:
        await asyncio.wait({task}, timeout=_TERMINATE_TIMEOUT_S)
    except Exception as e:      # asyncio.wait 理论上不抛；兜底防逃逸
        logger.warning(f"[{job_id}] 等待孤儿 task 收尾异常: {e}")
        return "wait-failed"
    if task.done():
        logger.info(f"[{job_id}] Watchdog terminated orphan pipeline task")
        return "cancelled"
    logger.error(
        f"[{job_id}] 孤儿 pipeline task 在 {_TERMINATE_TIMEOUT_S:g}s 内未退出 —— "
        f"per-job 锁可能仍被持有，重试会阻塞在 pending（需人工介入）"
    )
    return "not-exited"


async def recover_stalled_jobs(*, now: datetime | None = None) -> int:
    """把停滞的 job 标记为 error（+ 终止孤儿 task + 审计 + 通知），返回处理条数。

    与 `recover_stuck_jobs` 的差异只有判定来源；**收敛动作是同款再加一步
    终止孤儿**：直接 UPDATE（`ocr_done → error` 等不在 `VALID_TRANSITIONS`
    里，属"异常收敛"场景，绕过状态机但必须留审计），UPDATE 带 `status = ?`
    条件防并发改写（快照与 UPDATE 之间用户可能已 retry）。
    """
    # Runtime resolution — tests rebuild core.pipeline.db_lock.
    from core.pipeline import db_lock
    from core.pipeline.state import _audit_log

    stalled = await find_stalled_jobs(now=now)
    _STATE["last_stalled_found"] = len(stalled)
    if not stalled:
        return 0

    db = await get_db()
    logger.warning(
        f"[Watchdog] {len(stalled)} stalled job(s): "
        f"{[(r['id'][:8], r['status'], int(r['elapsed_s'])) for r in stalled]}"
    )

    recovered = 0
    for item in stalled:
        job_id = item["id"]
        old_status = item["status"]
        async with db_lock:
            cursor = await db.execute(
                "UPDATE jobs SET status = 'error', error_message = ?, "
                "finished_at = datetime('now','localtime') "
                "WHERE id = ? AND status = ?",
                (f"运行时看门狗：任务在「{old_status}」停滞 {item['elapsed_s']:.0f} 秒"
                 f"（阈值 {item['limit_s']:.0f} 秒）无进展，已标记为失败供重试", job_id, old_status),
            )
            await db.commit()
            changed = cursor.rowcount
        if changed == 0:
            # 并发路径已把它推进/收敛了 —— 不重复动作、不误发通知
            logger.info(f"[Watchdog] {job_id} 状态已变化（原 {old_status}），跳过")
            continue
        recovered += 1
        # 终止孤儿 task **必须在 db_lock 之外**：pipeline 的 CancelledError
        # 分支自己要 transition_status → 取 db_lock；持锁等待就是死锁
        # （与下面 _audit_log 同一类坑，asyncio.Lock 不可重入）。
        orphan = await _terminate_pipeline_task(job_id)
        detail = (
            f"stalled in {old_status}: no progress for {item['elapsed_s']:.0f}s "
            f"(limit {item['limit_s']:.0f}s, last_activity_at={item['last_activity_at']}); "
            f"orphan_task={orphan}"
        )
        # 审计**必须在 db_lock 之外**写：_audit_log 自己会取 db_lock，而
        # asyncio.Lock 不可重入 —— 持锁调用会直接死锁（P-C2 记录过的坑；
        # 本模块首版就是这么写的，被 test_marks_error_audits_and_notifies
        # 跑成永久挂起当场抓出）。
        await _audit_log(db, job_id, AUDIT_ACTION, detail)
        logger.warning(f"[{job_id}] Watchdog recovered stalled job: {old_status} → error ({detail})")
        # 通知在锁外（飞书 HTTP 退避可达数秒，持全局 db_lock 会阻塞所有写入方
        # —— 与 round-21 的取消通知同款教训）。通知失败绝不影响收敛结果。
        try:
            from core.notify import notify_job
            await notify_job(job_id, "error")
        except Exception:  # notify_job 自身已兜底，此处双保险防异常逃逸
            pass

    if recovered:
        _STATE["total_recovered"] += recovered
        logger.warning(f"[Watchdog] Recovery complete: {recovered} stalled job(s) marked as error")
    _STATE["last_recovered"] = recovered
    return recovered


async def watchdog_loop(stop_event: asyncio.Event | None = None) -> None:
    """后台周期扫描。由 lifespan 以 task 形式启动，随进程退出而取消。

    绝不因单次扫描失败而退出循环 —— 看门狗自己挂掉比 job 卡死更糟
    （用户会以为"有兜底"）。每轮异常只记日志，下一轮继续。
    """
    interval = scan_interval_seconds()
    _STATE["running"] = True
    _STATE["started_at"] = _local_now_str()
    logger.info(
        f"[Watchdog] started (interval={interval:g}s, scale={_scale():g}, "
        f"ocr_limit={stall_limit_seconds('ocr_running', 1):.0f}s@1p)"
    )
    try:
        while True:
            try:
                if stop_event is not None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=interval)
                        logger.info("[Watchdog] stop event set, exiting")
                        return
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(interval)
                await recover_stalled_jobs()
                _STATE["last_scan_error"] = None
            except asyncio.CancelledError:
                logger.info("[Watchdog] cancelled")
                raise
            except Exception as e:
                # 单轮失败也要留下痕迹（否则"看门狗在跑但一直失败"无法区分于正常）
                _STATE["last_scan_error"] = f"{type(e).__name__}: {e}"
                logger.error(f"[Watchdog] scan failed: {e}", exc_info=True)
            finally:
                _STATE["total_scans"] += 1
                _STATE["last_scan_at"] = _local_now_str()
    finally:
        _STATE["running"] = False


def status_snapshot() -> dict:
    """看门狗自述（`/api/health/watchdog`）。

    只暴露可观测性信息，不含任何密钥；`last_scan_at` 停滞即说明巡检已死。
    """
    return {
        "enabled": enabled(),
        "interval_s": scan_interval_seconds(),
        "scale": _scale(),
        "watch_statuses": list(_WATCHDOG_STATUSES),
        "pending_watch_requires_live_task": True,
        "stall_limits_s": {
            s: _BASE_STALL_S[s] for s in sorted(_BASE_STALL_S)
        },
        "per_page_s": dict(_PER_PAGE_S),
        "cap_s": dict(_CAP_S),
        "ocr_upstream_cap_s": float(POLL_TIMEOUT_MAX),
        "cpu_task_cap_s": cpu_task_timeout_seconds(),
        **_STATE,
    }
