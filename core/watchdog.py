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
4. **`pending` 不在监视范围**。启动时 `pending` 确实意味着"进程死在起飞前"
   （故 `recover_stuck_jobs` 认它）；但**运行期间 `pending` 是合法的排队态**
   —— `MAX_CONCURRENT_JOBS` 个长任务在跑时，新上传的任务本来就要等着。
   把它当停滞 = 误杀用户排队的上传。真正崩掉的 pending 由启动恢复兜底。

## 阈值怎么定

基准值 + 页数增量，全部刻意宽松：**本模块的职责是抓"永久停滞"，
不是绩效考核**。所有上游调用（LLM 180s / MinerU 60-300s / Paddle 封顶
3600s）与本地 CPU 重活（`PBC_CPU_TASK_TIMEOUT_S` 默认 1800s）都已有上限，
所以"真正的永久停滞"只会来自 bug 或进程级异常 —— 晚判几分钟毫无代价，
误杀一个跑了 40 分钟的真实任务代价极高。

实测基准（`docs/ADVERSARIAL_AUDIT.md` §5 / CLAUDE.md Round 15/23）：
- 51 页真实批记录：OCR 644s、逐页 LLM 825-1209s、跨页 122s
- 4 页旋转自愈轮：整轮 1031s（几乎全在 OCR —— 每个角度探测都是完整上游任务）
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from db.client import get_db
from core.pipeline.state import _STUCK_STATUSES

logger = logging.getLogger(__name__)

_TS_FMT = "%Y-%m-%d %H:%M:%S"

# 监视范围：_STUCK_STATUSES 去掉 pending（见模块 docstring 判据 4）。
# 用元组推导而不是重新硬编码一份状态列表 —— 状态串在本项目已有 4 处
# 独立硬编码的历史（docs/ADVERSARIAL_AUDIT.md §2 P1），不再加第 5 处。
_WATCHDOG_STATUSES: tuple[str, ...] = tuple(s for s in _STUCK_STATUSES if s != "pending")

# 基准停滞阈值（秒）
_BASE_STALL_S = {
    "ocr_running": 1800.0,
    "ocr_done": 1800.0,      # 纯过渡态，正常停留不到 1s，留足余量
    "analyzing": 1800.0,
    "cancelling": 900.0,     # 取消要等一次在途 OCR 轮询收尾
}

# 每页增量（秒）：OCR 要覆盖旋转自愈（每页最多 2 角度 × 2 重试的完整上游任务），
# 逐页 LLM 用 180s/页（即 LLM 适配器单次超时）作为上界估计。
_PER_PAGE_S = {
    "ocr_running": 120.0,
    "analyzing": 180.0,
}

# 封顶（秒）：51 页上限下 OCR 6390s / LLM 10800s，均远超实测值。
_CAP_S = {
    "ocr_running": 10800.0,
    "analyzing": 10800.0,
}

AUDIT_ACTION = "watchdog_stall_recovery"


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


def is_watched(status: str) -> bool:
    return status in _WATCHDOG_STATUSES


def stall_limit_seconds(status: str, total_pages: int | None) -> float:
    """该状态 + 页数下的停滞阈值（秒）。未监视的状态返回 `inf`（永不判定）。

    纯函数：便于单测直接锁定数值契约，无需起 DB。
    """
    if not is_watched(status):
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


async def find_stalled_jobs(*, now: datetime | None = None) -> list[dict]:
    """返回停滞超过阈值的非终态 job（**只读，不改状态** —— 便于单测与预演）。

    每项含 `id / status / filename / total_pages / last_activity_at /
    elapsed_s / limit_s`。
    """
    db = await get_db()
    placeholders = ",".join("?" * len(_WATCHDOG_STATUSES))
    cursor = await db.execute(
        f"SELECT id, status, filename, total_pages, last_activity_at FROM jobs "
        f"WHERE status IN ({placeholders}) AND last_activity_at IS NOT NULL",
        _WATCHDOG_STATUSES,
    )
    rows = await cursor.fetchall()

    stalled: list[dict] = []
    for row in rows:
        status = row["status"]
        if not is_watched(status):
            continue
        elapsed = elapsed_seconds(row["last_activity_at"], now)
        if elapsed is None:
            # 时间戳不可解析 → 不可判定，跳过（绝不猜）
            logger.debug(f"[{row['id']}] last_activity_at 无法解析，跳过判定")
            continue
        limit = stall_limit_seconds(status, row["total_pages"])
        if elapsed > limit:
            stalled.append({
                "id": row["id"],
                "status": status,
                "filename": row["filename"] if "filename" in row.keys() else "?",
                "total_pages": row["total_pages"] if "total_pages" in row.keys() else None,
                "last_activity_at": row["last_activity_at"],
                "elapsed_s": elapsed,
                "limit_s": limit,
            })
    return stalled


async def recover_stalled_jobs(*, now: datetime | None = None) -> int:
    """把停滞的 job 标记为 error（+ 审计 + 通知），返回处理条数。

    与 `recover_stuck_jobs` 的差异只有判定来源；**收敛动作完全一致**：
    直接 UPDATE（`ocr_done → error` 等不在 `VALID_TRANSITIONS` 里，属
    "异常收敛"场景，绕过状态机但必须留审计），UPDATE 带 `status = ?`
    条件防并发改写（快照与 UPDATE 之间用户可能已 retry）。
    """
    # Runtime resolution — tests rebuild core.pipeline.db_lock.
    from core.pipeline import db_lock
    from core.pipeline.state import _audit_log

    stalled = await find_stalled_jobs(now=now)
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
        detail = (
            f"stalled in {old_status}: no progress for {item['elapsed_s']:.0f}s "
            f"(limit {item['limit_s']:.0f}s, last_activity_at={item['last_activity_at']})"
        )
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
        # 审计**必须在 db_lock 之外**写：_audit_log 自己会取 db_lock，而
        # asyncio.Lock 不可重入 —— 持锁调用会直接死锁（P-C2 记录过的坑；
        # 本模块首版就是这么写的，被 test_marks_error_audits_and_notifies
        # 跑成永久挂起当场抓出）。
        await _audit_log(db, job_id, AUDIT_ACTION, detail)
        recovered += 1
        logger.warning(f"[{job_id}] Watchdog recovered stalled job: {old_status} → error ({detail})")
        # 通知在锁外（飞书 HTTP 退避可达数秒，持全局 db_lock 会阻塞所有写入方
        # —— 与 round-21 的取消通知同款教训）。通知失败绝不影响收敛结果。
        try:
            from core.notify import notify_job
            await notify_job(job_id, "error")
        except Exception:  # notify_job 自身已兜底，此处双保险防异常逃逸
            pass

    if recovered:
        logger.warning(f"[Watchdog] Recovery complete: {recovered} stalled job(s) marked as error")
    return recovered


async def watchdog_loop(stop_event: asyncio.Event | None = None) -> None:
    """后台周期扫描。由 lifespan 以 task 形式启动，随进程退出而取消。

    绝不因单次扫描失败而退出循环 —— 看门狗自己挂掉比 job 卡死更糟
    （用户会以为"有兜底"）。每轮异常只记日志，下一轮继续。
    """
    interval = scan_interval_seconds()
    logger.info(f"[Watchdog] started (interval={interval:g}s, scale={_scale():g})")
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
        except asyncio.CancelledError:
            logger.info("[Watchdog] cancelled")
            raise
        except Exception as e:
            logger.error(f"[Watchdog] scan failed: {e}", exc_info=True)
