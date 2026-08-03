"""Pipeline orchestration — stages 1→2→3 for a single job.

Stage 1: OCR (PaddleOCR-VL 或 MinerU，由 OCR_BACKEND 配置) → save page_cache.raw_html
Stage 2: LLM page-by-page analysis → save page_cache.structured_json
Stage 3: Cross-page analysis → generate findings

Fault tolerance: single page failure does not kill the pipeline.
Resume: skips pages that already have structured_json in page_cache.
Cancel: checks job status before each page, exits if status=cancelling.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

from config import config
from db.client import get_db
from core.page_analyzer import analyze_page
from core.cross_page_analyzer import analyze_cross_page

logger = logging.getLogger(__name__)

# Phase 7: per-job async lock — prevents cancel+retry race where two
# pipeline coroutines could run simultaneously on the same job_id.
# Keyed by job_id; entries are removed when pipeline exits.
_pipeline_locks: dict[str, asyncio.Lock] = {}

# 活跃 pipeline task 注册表 — 用于优雅关闭时取消所有运行中的任务
# key=job_id, value=asyncio.Task。task 完成后自动从注册表移除。
_pipeline_tasks: dict[str, asyncio.Task] = {}
_locks_guard = asyncio.Lock()


def _get_ocr_backend():
    """根据配置返回 OCR 后端的 run_ocr 函数。

    OCR_BACKEND=paddle (默认): 使用 PaddleOCR-VL
    OCR_BACKEND=mineru:        使用 MinerU 精准解析
    """
    backend = config["app"].ocr_backend.lower()
    if backend == "mineru":
        from core.mineru_client import run_ocr as mineru_run
        logger.info("[Pipeline] OCR 后端: MinerU")
        return mineru_run
    # 默认 PaddleOCR
    from core.ocr_client import run_ocr as paddle_run
    logger.info("[Pipeline] OCR 后端: PaddleOCR-VL")
    return paddle_run


async def _audit_log(db, job_id: str, action: str, detail: str = ""):
    """Write an entry to the audit_log table."""
    try:
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail) VALUES (?, ?, ?)",
            (job_id, action, detail),
        )
    except Exception as e:
        logger.warning(f"[{job_id}] Audit log write failed: {e}")


# Valid state transitions — 生产级状态机
# 每个状态只能转换到 allowed 集合中的状态
VALID_TRANSITIONS = {
    "pending":          {"ocr_running", "error", "cancelling", "archived"},
    "ocr_running":      {"ocr_done", "error", "cancelling"},
    "ocr_done":         {"analyzing", "error", "cancelling"},
    "analyzing":        {"review", "partial_review", "error", "cancelling"},
    "cancelling":       {"cancelled"},
    "review":           {"archived"},
    "partial_review":   {"archived"},
    "error":            {"pending", "archived"},
    "cancelled":        {"pending", "archived"},
    "archived":         {"review"},  # unarchive
}


class InvalidTransitionError(Exception):
    """状态转换非法时抛出。"""
    pass


async def transition_status(db, job_id: str, new_status: str, detail: str = "") -> str:
    """强制状态转换 + audit_log 记录。

    生产级实现：
    1. 校验当前状态 -> 新状态是否合法
    2. 非法时抛 InvalidTransitionError（阻断操作）
    3. 合法时更新 status + 写 audit_log
    4. 返回新状态

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
        raise InvalidTransitionError(
            f"Cannot transition from '{current}' to '{new_status}'. "
            f"Allowed: {allowed}"
        )

    await db.execute("UPDATE jobs SET status = ? WHERE id = ?", (new_status, job_id))
    await db.execute(
        "INSERT INTO audit_log (job_id, action, detail) VALUES (?, ?, ?)",
        (job_id, f"status_transition", f"{current} → {new_status}: {detail}" if detail else f"{current} → {new_status}"),
    )
    await db.commit()
    logger.info(f"[{job_id}] Status: {current} → {new_status}" + (f" ({detail})" if detail else ""))
    return new_status


# 非终态：应用退出时这些状态的 job 永远无法继续（pipeline 进程已死）
_STUCK_STATUSES = ("pending", "ocr_running", "ocr_done", "analyzing", "cancelling")


async def recover_stuck_jobs() -> int:
    """重启时将卡死的 job 标记为 error。

    应用崩溃 / 强杀时，处于 pending / ocr_running / ocr_done / analyzing /
    cancelling 的 job 永远不会完成（pipeline 进程已死）。在启动 lifespan 中
    调用此函数，将这些 job 标记为 error，写入 error_message，允许用户重试。

    Returns:
        被恢复（标记为 error）的 job 数量
    """
    db = await get_db()
    placeholders = ",".join("?" * len(_STUCK_STATUSES))
    cursor = await db.execute(
        f"SELECT id, status, filename, created_at FROM jobs WHERE status IN ({placeholders})",
        _STUCK_STATUSES,
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
        await db.execute(
            "UPDATE jobs SET status = 'error', "
            "error_message = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
            (f"应用重启恢复：原状态 {old_status} 非终态，标记为 error 供重试", job_id),
        )
        await _audit_log(
            db, job_id, "stuck_recovery",
            f"recovered on startup: {old_status} → error",
        )
        logger.info(f"[{job_id}] Stuck job recovered: {old_status} → error")

    await db.commit()
    logger.warning(
        f"[Startup] Recovery complete: {len(stuck)} stuck jobs marked as error "
        f"(ids: {[r['id'] for r in stuck]})"
    )
    return len(stuck)


def launch_pipeline(job_id: str, pdf_path: str) -> asyncio.Task:
    """启动 pipeline 后台 task 并注册到 _pipeline_tasks。

    替代 FastAPI 的 background_tasks.add_task，以便：
    1. 优雅关闭时可通过 _pipeline_tasks 取消所有运行中的任务
    2. task 完成后自动从注册表移除（避免内存泄漏）

    用法（api/jobs.py）：
        from core.pipeline import launch_pipeline
        launch_pipeline(job_id, str(pdf_path))
    """
    def _on_done(task: asyncio.Task, jid: str = job_id):
        # task 完成后从注册表移除（无论成功/失败/取消）
        removed = _pipeline_tasks.pop(jid, None)
        active_after = len(_pipeline_tasks)
        if task.cancelled():
            logger.info(
                f"[{jid}] Pipeline task cancelled and unregistered "
                f"(was_registered={removed is not None}, active_after={active_after})"
            )
        elif task.exception():
            logger.error(
                f"[{jid}] Pipeline task crashed: {task.exception()!r} "
                f"(was_registered={removed is not None}, active_after={active_after})"
            )
        else:
            logger.info(
                f"[{jid}] Pipeline task completed and unregistered "
                f"(was_registered={removed is not None}, active_after={active_after})"
            )

    task = asyncio.create_task(run_pipeline(job_id, pdf_path))
    _pipeline_tasks[job_id] = task
    task.add_done_callback(_on_done)
    logger.info(
        f"[{job_id}] Pipeline task launched and registered "
        f"(pdf={Path(pdf_path).name}, active={len(_pipeline_tasks)})"
    )
    return task


async def run_pipeline(job_id: str, pdf_path: str):
    """Main async pipeline. Runs in background task.

    Phase 7: acquires a per-job async lock to prevent cancel+retry race.
    If a retry is requested while a previous pipeline is still draining,
    the retry waits for the lock, then sees status=cancelled and exits
    cleanly without spawning duplicate LLM calls.

    资源清理：pipeline 结束后（无论成功/失败/取消）删除上传的 PDF 临时文件。
    OCR 完成后 raw_html 已存入数据库，PDF 文件不再需要。
    """
    # Acquire per-job lock — prevents two pipelines on the same job_id
    async with _locks_guard:
        lock_existed = job_id in _pipeline_locks
        lock = _pipeline_locks.setdefault(job_id, asyncio.Lock())
    if lock_existed:
        logger.info(f"[{job_id}] Lock contention: another pipeline still draining, waiting...")
    try:
        if lock.locked():
            logger.info(f"[{job_id}] Per-job lock held by another coroutine, waiting to acquire")
        async with lock:
            logger.info(f"[{job_id}] Per-job lock acquired")
            await _run_pipeline_impl(job_id, pdf_path)
    finally:
        # Cleanup lock entry after pipeline exits
        async with _locks_guard:
            popped = _pipeline_locks.pop(job_id, None)
        logger.info(
            f"[{job_id}] Per-job lock released and removed from registry "
            f"(was_present={popped is not None})"
        )
        # NOTE: PDF 文件不在此处删除 — 复核页 /api/jobs/{id}/pdf 需要它。
        # PDF 在 job 删除 (DELETE /api/jobs/{id}) 或归档时清理。
        # OCR 完成后 raw_html 已存入数据库，但 PDF 仍需保留用于人工复核预览。
        logger.info(f"[{job_id}] Pipeline exited, PDF retained for review: {Path(pdf_path).name}")


async def _run_pipeline_impl(job_id: str, pdf_path: str):
    """Pipeline implementation — guarded by per-job lock from run_pipeline."""
    db = await get_db()
    pipeline_start = time.time()

    try:
        # ── Stage 1: OCR ────────────────────────────────────────
        await transition_status(db, job_id, "ocr_running", "Stage 1 start")
        ocr_backend = config["app"].ocr_backend
        await _audit_log(db, job_id, "pipeline_start",
                         f"pdf={Path(pdf_path).name} ocr_backend={ocr_backend}")
        logger.info(f"[{job_id}] Stage 1: Starting OCR (backend={ocr_backend})...")

        stage1_start = time.time()
        ocr_run = _get_ocr_backend()
        pages = await asyncio.to_thread(ocr_run, pdf_path)
        stage1_ms = int((time.time() - stage1_start) * 1000)
        logger.info(f"[{job_id}] Stage 1: OCR complete: {len(pages)} pages in {stage1_ms}ms")
        await _audit_log(db, job_id, "stage1_complete", f"pages={len(pages)} duration={stage1_ms}ms")

        # Check cancellation
        if await _is_cancelled(job_id):
            return

        # Save raw HTML to page_cache (skip if already exists)
        existing_pages = await _get_existing_pages(db, job_id)
        new_pages = 0
        for i, page in enumerate(pages):
            page_num = i + 1
            if page_num in existing_pages:
                continue
            raw_html = page.get("markdown", {}).get("text", "")
            await db.execute(
                "INSERT OR IGNORE INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
                (job_id, page_num, raw_html),
            )
            new_pages += 1
        await db.execute(
            "UPDATE jobs SET total_pages = ? WHERE id = ?",
            (len(pages), job_id),
        )
        await transition_status(db, job_id, "ocr_done", f"Stage 1 complete: {len(pages)} pages")
        await db.commit()
        logger.info(f"[{job_id}] Stage 1: Saved {new_pages} new pages ({len(existing_pages)} cached)")

        # Check cancellation
        if await _is_cancelled(job_id):
            return

        # ── Stage 2: Per-page LLM analysis (concurrent) ─────────
        await transition_status(db, job_id, "analyzing", "Stage 2 start")
        concurrency = config["app"].llm_concurrency
        logger.info(
            f"[{job_id}] Stage 2: Analyzing {len(pages)} pages (concurrency={concurrency})..."
        )

        stage2_start = time.time()
        failed_pages: list[int] = []

        # Get already-analyzed pages for resume
        analyzed_pages = await _get_analyzed_pages(db, job_id)
        logger.info(
            f"[{job_id}] Stage 2: {len(analyzed_pages)} pages already analyzed, "
            f"resuming from {len(analyzed_pages) + 1}"
        )

        # Build list of pages that still need analysis
        todo: list[tuple[int, dict]] = []
        for i, page in enumerate(pages):
            page_num = i + 1
            if page_num in analyzed_pages:
                continue
            todo.append((page_num, page))

        # Shared state guards — aiosqlite single connection does NOT support
        # concurrent execute, so all DB writes must be serialized via db_lock.
        # failed_pages list and completed counter also need a lock.
        db_lock = asyncio.Lock()
        state_lock = asyncio.Lock()
        completed = 0
        total_pages = len(pages)
        remaining = len(todo)
        sem = asyncio.Semaphore(concurrency)

        async def _analyze_one(page_num: int, page: dict) -> None:
            """Concurrent LLM analysis for a single page.

            LLM call runs concurrently (semaphore-limited); DB write is
            serialized via db_lock to avoid aiosqlite "Recursive use of
            cursors" error on the shared connection.
            """
            nonlocal completed

            # Check cancellation before starting — if cancelled, skip without
            # spawning an LLM call. Already-running tasks will finish on
            # their own (LLM HTTP calls can't be aborted mid-flight).
            if await _is_cancelled(job_id):
                logger.info(f"[{job_id}] Stage 2: Skipped page {page_num} (cancelled)")
                return

            raw_html = page.get("markdown", {}).get("text", "")
            async with sem:
                try:
                    page_start = time.time()
                    structured = await analyze_page(
                        raw_html, page_num=page_num, job_id=job_id
                    )
                    page_ms = int((time.time() - page_start) * 1000)

                    payload = json.dumps(structured, ensure_ascii=False)
                    async with db_lock:
                        await db.execute(
                            "UPDATE page_cache SET structured_json = ?, analyzed_at = CURRENT_TIMESTAMP "
                            "WHERE job_id = ? AND page = ?",
                            (payload, job_id, page_num),
                        )
                        await db.commit()
                except Exception as e:
                    logger.error(f"[{job_id}] Stage 2: Page {page_num} failed: {e}")
                    async with state_lock:
                        failed_pages.append(page_num)
                    error_data = {
                        "page_number": page_num,
                        "_parse_error": True,
                        "_error": str(e)[:200],
                        "overall_confidence": "low",
                    }
                    async with db_lock:
                        await db.execute(
                            "UPDATE page_cache SET structured_json = ?, analyzed_at = CURRENT_TIMESTAMP "
                            "WHERE job_id = ? AND page = ?",
                            (json.dumps(error_data, ensure_ascii=False), job_id, page_num),
                        )
                        await db.commit()
                    return

            async with state_lock:
                completed += 1
            logger.info(
                f"[{job_id}] Stage 2: Page {page_num}/{total_pages} analyzed in {page_ms}ms "
                f"({completed}/{remaining} done)"
            )

        # Run all page analyses concurrently
        tasks = [_analyze_one(pn, pg) for pn, pg in todo]
        await asyncio.gather(*tasks)

        stage2_ms = int((time.time() - stage2_start) * 1000)
        logger.info(
            f"[{job_id}] Stage 2: Complete in {stage2_ms}ms, "
            f"{len(failed_pages)} pages failed"
        )
        await _audit_log(db, job_id, "stage2_complete",
                         f"duration={stage2_ms}ms failed={failed_pages}")

        # Check cancellation
        if await _is_cancelled(job_id):
            return

        # ── Stage 3: Cross-page analysis ────────────────────────
        logger.info(f"[{job_id}] Stage 3: Cross-page analysis...")
        stage3_start = time.time()

        cursor = await db.execute(
            "SELECT page, structured_json FROM page_cache WHERE job_id = ? ORDER BY page",
            (job_id,),
        )
        page_structures = []
        for row in await cursor.fetchall():
            if row["structured_json"]:
                try:
                    data = json.loads(row["structured_json"])
                    # Skip pages with parse errors
                    if not data.get("_parse_error"):
                        page_structures.append({"page": row["page"], "data": data})
                except json.JSONDecodeError:
                    logger.warning(f"[{job_id}] Stage 3: Failed to parse page_cache page={row['page']}")

        findings = await analyze_cross_page(page_structures, job_id=job_id)
        stage3_ms = int((time.time() - stage3_start) * 1000)

        # Save findings
        for f in findings:
            await db.execute(
                "INSERT INTO findings (job_id, page, type, severity, description, ocr_text, operator, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, f["page"], f["type"], f["severity"], f["description"],
                 f.get("ocr_text"), f.get("operator"), f.get("source", "rule")),
            )

        # Determine final status
        total_cost_ms = int((time.time() - pipeline_start) * 1000)
        final_status = "partial_review" if failed_pages else "review"

        await db.execute(
            "UPDATE jobs SET finished_at = CURRENT_TIMESTAMP, "
            "stage1_ms = ?, stage2_ms = ?, stage3_ms = ?, failed_pages = ? "
            "WHERE id = ?",
            (stage1_ms, stage2_ms, stage3_ms,
             json.dumps(failed_pages) if failed_pages else None, job_id),
        )
        await transition_status(db, job_id, final_status,
                                f"Pipeline complete: {len(findings)} findings, {len(failed_pages)} failed")

        logger.info(f"[{job_id}] Pipeline complete: status={final_status}, "
                     f"{len(findings)} findings, {len(failed_pages)} failed pages, "
                     f"total={total_cost_ms}ms (OCR={stage1_ms} LLM={stage2_ms} Cross={stage3_ms})")
        await _audit_log(db, job_id, "pipeline_complete",
                         f"status={final_status} findings={len(findings)} "
                         f"failed={len(failed_pages)} total={total_cost_ms}ms")

    except asyncio.CancelledError:
        # 优雅关闭：应用退出时 background task 被取消
        # 将 job 标记为 error（带"中断"标记），允许用户重试
        logger.warning(f"[{job_id}] Pipeline cancelled (app shutdown or task revoke)")
        try:
            await transition_status(db, job_id, "error", "Pipeline cancelled (interrupted)")
        except InvalidTransitionError:
            # 已是终态（review/cancelled），保持不变
            pass
        # 重新抛出让 asyncio 正常清理
        raise
    except InvalidTransitionError as e:
        logger.error(f"[{job_id}] Invalid state transition: {e}")
        await _audit_log(db, job_id, "transition_error", str(e)[:200])
    except Exception as e:
        logger.error(f"[{job_id}] Pipeline failed: {e}", exc_info=True)
        await _audit_log(db, job_id, "pipeline_error", str(e)[:200])
        error_msg = str(e)[:500]
        try:
            await transition_status(db, job_id, "error", f"Pipeline failed: {str(e)[:100]}")
            # transition_status 只更新 status 字段，还需显式写入 error_message + finished_at
            await db.execute(
                "UPDATE jobs SET error_message = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (error_msg, job_id),
            )
            await db.commit()
        except InvalidTransitionError:
            # 已是终态，直接更新 error_message
            await db.execute(
                "UPDATE jobs SET error_message = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (error_msg, job_id),
            )
            await db.commit()


async def _is_cancelled(job_id: str) -> bool:
    """Check if job has been cancelled. Transitions cancelling → cancelled via
    transition_status so the audit_log entry is written (no bypass)."""
    db = await get_db()
    cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if row and row["status"] == "cancelling":
        # Use transition_status so audit_log captures the transition
        try:
            await transition_status(db, job_id, "cancelled", "Pipeline acknowledged cancel")
        except InvalidTransitionError as e:
            # Race: another caller already transitioned; log and treat as cancelled
            logger.warning(f"[{job_id}] Cancel transition race: {e}")
        # Always set finished_at (transition_status doesn't set this column)
        await db.execute(
            "UPDATE jobs SET finished_at = CURRENT_TIMESTAMP WHERE id = ?",
            (job_id,),
        )
        await db.commit()
        logger.info(f"[{job_id}] Pipeline cancelled")
        return True
    return False


async def _get_existing_pages(db, job_id: str) -> set[int]:
    """Get page numbers that already have raw_html in page_cache."""
    cursor = await db.execute(
        "SELECT page FROM page_cache WHERE job_id = ?", (job_id,)
    )
    return {row["page"] for row in await cursor.fetchall()}


async def _get_analyzed_pages(db, job_id: str) -> set[int]:
    """Get page numbers that already have structured_json in page_cache."""
    cursor = await db.execute(
        "SELECT page FROM page_cache WHERE job_id = ? AND structured_json IS NOT NULL",
        (job_id,),
    )
    return {row["page"] for row in await cursor.fetchall()}
