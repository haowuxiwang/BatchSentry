"""Pipeline state machine: transitions, stuck-job recovery, audit writes.
Also hosts job-state queries (_is_cancelled, _update_ocr_progress) that
stage modules share. (module refactor)
"""
from __future__ import annotations

import json
import logging
import sqlite3

from db.client import get_db
from core.zh_map import zh_job_status

logger = logging.getLogger(__name__)
async def _audit_log(db, job_id: str, action: str, detail: str = ""):
    """Write an entry to the audit_log table.

    P-W1 修复：用 db_lock 序列化写入并即时 commit（审计日志应落盘，
    避免与 _analyze_one 等并发写入触发 aiosqlite 游标错误）。
    """
    # Runtime resolution — tests rebuild core.pipeline.db_lock.
    from core.pipeline import db_lock
    try:
        async with db_lock:
            await db.execute(
                "INSERT INTO audit_log (job_id, action, detail, created_at) "
                "VALUES (?, ?, ?, datetime('now','localtime'))",
                (job_id, action, detail),
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[{job_id}] Audit log write failed: {e}")


VALID_TRANSITIONS = {
    "pending":          {"ocr_running", "error", "cancelling", "archived"},
    "ocr_running":      {"ocr_done", "error", "cancelling"},
    "ocr_done":         {"analyzing", "error", "cancelling"},
    "analyzing":        {"review", "partial_review", "error", "cancelling"},
    "cancelling":       {"cancelled", "error"},
    "review":           {"pending", "archived"},  # pending: 重新分析（retry）
    "partial_review":   {"pending", "archived"},  # pending: retry 补分析失败页
    "error":            {"pending", "archived"},
    "cancelled":        {"pending", "archived"},
    "archived":         {"review"},  # unarchive
}


class InvalidTransitionError(Exception):
    """状态转换非法时抛出。"""
    pass


async def _transition_status_unlocked(db, job_id: str, new_status: str, detail: str = "") -> str:
    """transition_status 的内部实现（不加 db_lock）。

    P-C2 修复：调用方已持 db_lock 时调用此函数，避免 asyncio.Lock 不可重入
    导致的死锁。其他场景请使用公开的 transition_status（自动加锁）。

    生产级实现：
    1. 校验当前状态 -> 新状态是否合法
    2. 非法时抛 InvalidTransitionError（阻断操作）
    3. 合法时更新 status + 写 audit_log
    4. 返回新状态
    """
    cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row:
        raise InvalidTransitionError(f"Job {job_id} not found")

    current = row["status"]
    allowed = VALID_TRANSITIONS.get(current, set())

    if new_status not in allowed:
        # 生产环境：阻断非法转换，而非"仍然更新"
        logger.warning(
            f"[{job_id}] Blocked invalid transition: {current} → {new_status} "
            f"(allowed: {allowed})"
        )
        # 对抗审查 cr-17：被拒转换也写审计 — GMP 追溯要求记录"尝试过但
        # 被拒绝的状态变更"（如对终态 job 重复 cancel/retry），仅日志
        # 不足以防异常路径静态可追踪。
        try:
            await db.execute(
                "INSERT INTO audit_log (job_id, action, detail) "
                "VALUES (?, 'status_transition_blocked', ?)",
                (job_id, f"{current} → {new_status} (allowed: {allowed|set()})"),
            )
            await db.commit()
        except Exception as audit_err:
            logger.warning(f"[{job_id}] Audit log write failed: {audit_err}")
        # 中文化：异常消息直达 HTTP 400 detail + 前端错误提示，英文状态码
        # 对用户无意义（换算 zh_map 中文状态名）
        raise InvalidTransitionError(
            f"不能从「{zh_job_status(current)}」转换到「{zh_job_status(new_status)}」，"
            f"允许的转换：{', '.join(sorted(zh_job_status(s) for s in allowed))}"
        )

    await db.execute(
        "UPDATE jobs SET status = ?, last_activity_at = datetime('now','localtime') "
        "WHERE id = ?",
        (new_status, job_id),
    )
    await db.execute(
        "INSERT INTO audit_log (job_id, action, detail, created_at) "
                "VALUES (?, ?, ?, datetime('now','localtime'))",
        (job_id, "status_transition", f"{current} → {new_status}: {detail}" if detail else f"{current} → {new_status}"),
    )
    await db.commit()
    logger.info(f"[{job_id}] Status: {current} → {new_status}" + (f" ({detail})" if detail else ""))
    return new_status


async def transition_status(db, job_id: str, new_status: str, detail: str = "") -> str:
    """强制状态转换 + audit_log 记录（加 db_lock 串行化）。

    P-C2 修复：把 SELECT-check-UPDATE-INSERT-commit 整个序列包在 db_lock 内，
    防止与 _analyze_one 等并发写入触发 aiosqlite 游标错误或 TOCTOU。
    已持 db_lock 的内部调用方应使用 _transition_status_unlocked。

    Args:
        db: 数据库连接
        job_id: Job ID
        new_status: 目标状态
        detail: 转换原因（写入 audit_log）

    Returns:
        新状态

    Raises:
        InvalidTransitionError: 非法转换
    """
    # Runtime resolution — tests rebuild core.pipeline.db_lock.
    from core.pipeline import db_lock
    async with db_lock:
        return await _transition_status_unlocked(db, job_id, new_status, detail)


_STUCK_STATUSES = ("pending", "ocr_running", "ocr_done", "analyzing", "cancelling")


async def recover_stuck_jobs(process_started_at: str | None = None) -> int:
    """重启时将卡死的 job 标记为 error。

    应用崩溃 / 强杀时，处于 pending / ocr_running / ocr_done / analyzing /
    cancelling 的 job 永远不会完成（pipeline 进程已死）。在启动 lifespan 中
    调用此函数，将这些 job 标记为 error，写入 error_message，允许用户重试。

    Args:
        process_started_at: 进程启动时刻（UTC "YYYY-MM-DD HH:MM:SS"）。
            仅恢复 created_at **早于**该时刻的 job — 晚于启动时刻创建的
            job 是当前进程存活期间新上传的任务（pipeline 正在/即将运行），
            不能误判为上次崩溃的遗留。竞态场景：lifespan 中 recover 以
            background task 方式执行，可能与本进程的新上传并发。

    Returns:
        被恢复（标记为 error）的 job 数量
    """
    db = await get_db()
    placeholders = ",".join("?" * len(_STUCK_STATUSES))
    params: tuple = _STUCK_STATUSES
    where_status = f"status IN ({placeholders})"
    if process_started_at:
        where_status += " AND created_at < ?"
        params = _STUCK_STATUSES + (process_started_at,)
    cursor = await db.execute(
        f"SELECT id, status, filename, created_at FROM jobs WHERE {where_status}",
        params,
    )
    stuck = await cursor.fetchall()
    if not stuck:
        logger.info("[Startup] No stuck jobs found (all jobs in terminal state)")
        return 0

    logger.warning(
        f"[Startup] Found {len(stuck)} stuck job(s) to recover: "
        f"{[(r['id'][:8], r['status']) for r in stuck]}"
    )

    for row in stuck:
        job_id = row["id"]
        old_status = row["status"]
        filename = row["filename"] if "filename" in row.keys() else "?"
        created_at = row["created_at"] if "created_at" in row.keys() else "?"
        logger.warning(
            f"[{job_id}] Recovering stuck job: status={old_status} "
            f"filename={filename} created_at={created_at}"
        )
        # 直接 UPDATE 而非 transition_status：状态机不允许 ocr_running→error
        # 之外的路径（如 pending→error 是允许的），但 ocr_done→error 不在
        # VALID_TRANSITIONS 中。这里属于"崩溃恢复"场景，绕过状态机校验，
        # 直接标记 + 审计日志记录。
        # 对抗审查（中文化收尾）：UPDATE 带 status IN (...) 条件 — SELECT
        # 快照与逐行 UPDATE 之间可能被并发路径改写状态（如用户 retry 已恢复
        # 的 job），无条件覆盖会把新状态打回 error。条件更新影响 0 行时跳过
        # 通知，避免对已恢复的 job 误发"应用重启恢复"失败通知。
        placeholders = ",".join("?" * len(_STUCK_STATUSES))
        cursor = await db.execute(
            f"UPDATE jobs SET status = 'error', "
            f"error_message = ?, finished_at = datetime('now','localtime') "
            f"WHERE id = ? AND status IN ({placeholders})",
            (f"应用重启恢复：原状态 {old_status} 非终态，标记为 error 供重试",
             job_id, *_STUCK_STATUSES),
        )
        if cursor.rowcount == 0:
            logger.info(
                f"[{job_id}] Recovery skipped: status already changed "
                f"(expected {old_status}) — no longer stuck"
            )
            continue
        await _audit_log(
            db, job_id, "stuck_recovery",
            f"recovered on startup: {old_status} → error",
        )
        logger.info(f"[{job_id}] Stuck job recovered: {old_status} → error")
        # P1-8：应用重启恢复也是终态（error）— 与 pipeline 正常 error 路径
        # 一致地发飞书通知。用户不在 GUI 前时（崩溃重启典型场景）也能得知
        # 任务失败可重试。旁路：通知失败绝不影响启动流程。
        try:
            from core.notify import notify_job
            await notify_job(job_id, "error")
        except Exception:
            pass  # notify_job 自身已兜底，此处双保险防异常逃逸

    await db.commit()
    logger.warning(
        f"[Startup] Recovery complete: {len(stuck)} stuck jobs marked as error "
        f"(ids: {[r['id'] for r in stuck]})"
    )
    return len(stuck)


async def _is_cancelled(job_id: str) -> bool:
    """Check if job has been cancelled. Transitions cancelling → cancelled via
    _transition_status_unlocked so the audit_log entry is written (no bypass).

    P-C2 修复：本函数已持 db_lock，调用 _transition_status_unlocked（不加锁）
    而非 transition_status，避免 asyncio.Lock 不可重入导致死锁。
    """
    # Runtime resolution — tests rebuild core.pipeline.db_lock.
    from core.pipeline import db_lock
    db = await get_db()
    cancelled = False
    notify_cancelled = False
    async with db_lock:
        cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        if row and row["status"] in ("cancelling", "cancelled", "error"):
            if row["status"] == "cancelling":
                # Use _transition_status_unlocked so audit_log captures the transition
                # （已持 db_lock，不能再调用会加锁的 transition_status）
                try:
                    await _transition_status_unlocked(db, job_id, "cancelled", "取消已确认")
                except InvalidTransitionError as e:
                    # Race: another caller already transitioned; log and treat as cancelled
                    logger.warning(f"[{job_id}] Cancel transition race: {e}")
                # 对抗审查（round-20）：取消终态此前无任何通知调用点——README
                # 承诺"取消时推送飞书"。所有取消路径（stage0/stage1/stage2/
                # stage3 检查点、OCR 线程中止）都经本函数确认，此处是唯一
                # 收口点。仅在确认迁移本次触发（重复探测由 notify 审计查重兜底）。
                notify_cancelled = True
            # Always set finished_at (transition_status doesn't set this column)
            await db.execute(
                "UPDATE jobs SET finished_at = datetime('now','localtime') WHERE id = ?",
                (job_id,),
            )
            await db.commit()
            logger.info(f"[{job_id}] Pipeline cancelled")
            cancelled = True
    if notify_cancelled:
        # 锁外通知（round-21 对抗审查）：飞书 HTTP 重试退避可达数秒，
        # 持全局 db_lock 等网络会阻塞所有 DB 写入方。
        try:
            from core.notify import notify_job
            await notify_job(job_id, "cancelled")
        except Exception:
            pass  # notify_job 自身已兜底，此处双保险防异常逃逸
    return cancelled


def is_job_stopping_sync(job_id: str) -> bool:
    """Thread-safe cancellation probe for blocking OCR worker threads.

    OCR 轮询跑在 asyncio.to_thread 的线程里，无法 await 异步检查；本探针
    用独立只读 sqlite3 连接（WAL 允许并发读）轮询 jobs.status。

    只读、不做状态迁移 —— cancelling→cancelled 的正式转换 + 审计仍由
    _is_cancelled() 在阻塞调用中止后于事件循环上完成，避免两个写入方
    竞争同一状态行。
    """
    from config import config as _cfg

    try:
        conn = sqlite3.connect(_cfg["app"].database_path, timeout=2)
        try:
            row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return bool(row and row[0] in ("cancelling", "cancelled"))
        finally:
            conn.close()
    except Exception:
        # 探针失败绝不阻断 OCR 本身（下次 tick 再试）
        return False


async def touch_activity(db, job_id: str, *, commit: bool = False) -> None:
    """心跳：把 job 的"最后推进时刻"推到当前时间（运行时看门狗的唯一信号源）。

    **必须绑定真实前进**，不能挂独立定时器 —— 定时器在 stall 期间照样跳，
    看门狗永远不会触发（业界共识，见 `docs/RUNTIME_WATCHDOG.md` §4）。
    故只在四种"确实往前走了一步"的时刻调用：状态迁移、进度更新、
    单页分析完成、**自愈的有界退避等待开始前**
    （`self_heal._probe_slice_angle`，2026-09-16 缺陷 #120）。

    最后一种为何算"前进"：它是一次**由预算封顶**的刻意等待（等上游从
    拥塞中恢复），循环上界明确，故不会退化成"用假心跳掩盖永久停滞"——
    预算之外的真实停滞照样会被看门狗抓住。反之若不写心跳，看门狗会把
    "正在按预算等待"误判为停滞（阈值不变式，见 watchdog 模块 docstring）。

    设计取舍：
    - 失败**绝不抛异常**：心跳只是可观测性，不能因为它把正常管线打断
      （与 `_update_ocr_progress` 同款兜底）。
    - ``commit=False`` 是默认：进度写入点通常紧接着自己 commit，
      此处额外 commit 只会多一次 fsync（也避免与调用方事务边界打架）。
    """
    try:
        await db.execute(
            "UPDATE jobs SET last_activity_at = datetime('now','localtime') WHERE id = ?",
            (job_id,),
        )
        if commit:
            await db.commit()
    except Exception as e:
        logger.warning(f"[{job_id}] Activity heartbeat failed: {e}")


async def _update_ocr_progress(job_id: str, done: int, total: int) -> None:
    """更新 job.ocr_progress（JSON）供 SSE 实时推送。

    由 OCR 轮询线程通过 run_coroutine_threadsafe 调用；用 db_lock
    串行化，避免与 Stage 2 的并发 DB 写入冲突。
    """
    # Runtime resolution — tests rebuild core.pipeline.db_lock.
    from core.pipeline import db_lock
    db = await get_db()
    payload = json.dumps({"done": done, "total": total})
    try:
        async with db_lock:
            await db.execute(
                "UPDATE jobs SET ocr_progress = ?, "
                "last_activity_at = datetime('now','localtime') WHERE id = ?",
                (payload, job_id),
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[{job_id}] OCR progress update failed: {e}")


async def _update_self_heal_progress(
    job_id: str, main_done: int, main_total: int, done: int, total: int, pages: list[int]
) -> None:
    """更新空页自愈进度 — 独立子对象合并进 ocr_progress JSON。

    主 OCR 完成后自愈切片重跑期间，SSE 客户端看到 ocr_progress
    done==total 且状态不前进，误以为卡死。self_heal 键让前端能
    显示"空页自愈 2/6"，不干扰 _parse_ocr_progress 对主进度的解析。
    """
    from core.pipeline import db_lock
    db = await get_db()
    # total<=0 → 自愈结束，清除 self_heal 子键（保留主 OCR 进度）
    if total <= 0:
        payload = json.dumps({"done": main_done, "total": main_total})
    else:
        payload = json.dumps({
            "done": main_done,
            "total": main_total,
            "self_heal": {"done": done, "total": total, "pages": pages},
        })
    try:
        async with db_lock:
            await db.execute(
                "UPDATE jobs SET ocr_progress = ?, "
                "last_activity_at = datetime('now','localtime') WHERE id = ?",
                (payload, job_id),
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[{job_id}] Self-heal progress update failed: {e}")


async def _update_cross_progress(job_id: str, done: int, total: int, label: str) -> None:
    """跨页分析（Stage 3）子进度 — 合并进 ocr_progress JSON 的 cross 键。

    Stage 3 内部此前无任何进度信号：规则校验 + LLM 兜底 + LLM 语义
    合计可达 1-2 分钟，SSE 客户端只能靠 phase 推断"跨页分析中"，
    无法区分卡死与正常推进。total<=0 → 清除子键。
    """
    from core.pipeline import db_lock
    db = await get_db()
    try:
        async with db_lock:
            cursor = await db.execute(
                "SELECT ocr_progress FROM jobs WHERE id = ?", (job_id,)
            )
            row = await cursor.fetchone()
            data = {}
            if row and row["ocr_progress"]:
                try:
                    data = json.loads(row["ocr_progress"])
                    if not isinstance(data, dict):
                        data = {}
                except (ValueError, TypeError):
                    data = {}
            if total <= 0:
                data.pop("cross", None)
            else:
                data["cross"] = {"done": done, "total": total, "label": label}
            payload = json.dumps(data, ensure_ascii=False)
            await db.execute(
                "UPDATE jobs SET ocr_progress = ?, "
                "last_activity_at = datetime('now','localtime') WHERE id = ?",
                (payload, job_id),
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[{job_id}] Cross progress update failed: {e}")

