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
import re
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

# Module-level lock serializing all DB writes on the shared aiosqlite
# connection (single connection does NOT support concurrent execute).
# Used by _is_cancelled and _analyze_one to prevent "Recursive use of
# cursors" errors.
db_lock = asyncio.Lock()

# 分片 OCR 队列轮询间隔（run_ocr_sliced 线程回调 → asyncio.Queue 的
# 等待超时）。生产 2s 足够灵敏；测试可 monkeypatch 加速。
_SLICE_QUEUE_TIMEOUT = 2.0


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


def _get_ocr_chain() -> list[tuple[callable, str]]:
    """返回 OCR 主备链：[(run_ocr, name), ...]，首个为主后端。

    双 OCR 兜底：主后端（OCR_BACKEND 配置）之外的另一个后端若已配置
    token/api_url，则作为 failover 备选。仅当两个后端都可用时链长为 2。
    """
    backend = config["app"].ocr_backend.lower()
    primary = _get_ocr_backend()
    chain = [(primary, backend if backend in ("paddle", "mineru") else "paddle")]
    # 备选：未激活的后端配置完整时才加入 failover 链
    if backend == "mineru":
        paddle_cfg = config["paddle_ocr"]
        if paddle_cfg.api_url and paddle_cfg.token:
            from core.ocr_client import run_ocr as paddle_run
            chain.append((paddle_run, "paddle"))
            logger.info("[Pipeline] OCR failover 备选: PaddleOCR-VL")
    else:
        mineru_cfg = config["mineru"]
        if mineru_cfg.token:
            from core.mineru_client import run_ocr as mineru_run
            chain.append((mineru_run, "mineru"))
            logger.info("[Pipeline] OCR failover 备选: MinerU")
    return chain


def _sanitize_ocr_text(text: str) -> str:
    """清洗 OCR 原始文本（存库前），消除 MinerU/Paddle 产物噪音。

    噪音来源：
    - MinerU 表格 HTML 用字面 "\\n"（反斜杠+n）分隔单元格文本，直出时
      用户看到满屏 "\\n" 而非真实换行；
    - 每个 <td> 都带 style='text-align: center; word-wrap: break-word;'
      行内样式（对 LLM 和 OCR 文本面板都是纯噪音）；
    - img src 是长路径（imgs/img_in_image_box_xxx.jpg），截断为文件名。

    清洗后 raw_html 同时服务于 LLM 输入（page_analyzer 仍会二次剥离）
    与 review 页面 OCR 文本面板（htmlToText 展示）。
    """
    if not text:
        return text
    s = text.replace("\\n", "\n").replace("\\t", "\t")
    s = re.sub(r"""\s*style=['"][^'"]*['"]""", "", s)
    s = re.sub(r"""\s*width=['"][^'"]*['"]""", "", s)
    s = re.sub(r"""(src=["'])[^"']*/([^/"']+)(["'>])""", r"\1\2\3", s)
    # 折叠 3+ 个连续空行为 2 个（保留段落分隔）
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _pdf_page_count(pdf_path: str) -> int | None:
    """读取 PDF 物理页数（PyMuPDF），失败时返回 None（不阻断 OCR 流程）。

    robustness-A1：用于对比 OCR 结果页数，检测"解析成功但静默缺页"。
    """
    try:
        import fitz  # PyMuPDF — 仅需页数，不渲染

        with fitz.open(pdf_path) as doc:
            return doc.page_count
    except Exception as e:
        logger.warning(f"PDF page count probe failed ({pdf_path}): {e}")
        return None


async def _audit_log(db, job_id: str, action: str, detail: str = ""):
    """Write an entry to the audit_log table.

    P-W1 修复：用 db_lock 序列化写入并即时 commit（审计日志应落盘，
    避免与 _analyze_one 等并发写入触发 aiosqlite 游标错误）。
    """
    try:
        async with db_lock:
            await db.execute(
                "INSERT INTO audit_log (job_id, action, detail) VALUES (?, ?, ?)",
                (job_id, action, detail),
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[{job_id}] Audit log write failed: {e}")


# Valid state transitions — 生产级状态机
# 每个状态只能转换到 allowed 集合中的状态
VALID_TRANSITIONS = {
    "pending":          {"ocr_running", "error", "cancelling", "archived"},
    "ocr_running":      {"ocr_done", "error", "cancelling"},
    "ocr_done":         {"analyzing", "error", "cancelling"},
    "analyzing":        {"review", "partial_review", "error", "cancelling"},
    "cancelling":       {"cancelled", "error"},
    "review":           {"archived"},
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
    async with db_lock:
        return await _transition_status_unlocked(db, job_id, new_status, detail)


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
        # Only pop if this task is still the registered one
        # (prevents stale callback from deleting a newer task's entry)
        if _pipeline_tasks.get(jid) is task:
            removed = _pipeline_tasks.pop(jid, None)
        else:
            removed = None
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


async def _run_ocr_with_failover(db, job_id: str, pdf_path: str, progress_cb) -> tuple[list, str, list[str]]:
    """整份 OCR 主备链执行（双 OCR 兜底）。

    返回 (pages, used_backend, failures)：
    - pages: 成功的 OCR 结果，全部失败时 []
    - used_backend: 实际成功执行的后端名（"paddle"/"mineru"）
    - failures: 失败记录列表（每个元素描述一个后端的失败原因）

    失败判定：异常 / 0 页 / 严重页数缺失（缺 >20% 或 >5 页）。
    任一失败 → 切下一个后端整单重试；全部失败时 failures 非空。
    仅整份路径使用；分片路径（MinerU + OCR_SLICES>1）保持原逻辑。
    """
    from logging_config import ocr_job_id_var

    chain = _get_ocr_chain()
    failures: list[str] = []
    for attempt, (run_fn, name) in enumerate(chain):
        if attempt > 0:
            logger.warning(
                f"[{job_id}] OCR failover: {chain[0][1]} failed → trying {name}"
            )
            await _audit_log(
                db, job_id, "ocr_failover",
                f"from={chain[0][1]} to={name} reason={failures[-1] if failures else 'unknown'}",
            )
        _ocr_ctx_token = ocr_job_id_var.set(job_id)
        try:
            try:
                pages = await asyncio.to_thread(run_fn, pdf_path, progress_cb)
            finally:
                ocr_job_id_var.reset(_ocr_ctx_token)
        except Exception as e:
            failures.append(f"{name}: {type(e).__name__}: {str(e)[:300]}")
            logger.error(f"[{job_id}] OCR attempt failed (backend={name}): {failures[-1]}")
            continue
        if not pages:
            failures.append(f"{name}: 0 pages returned")
            logger.error(f"[{job_id}] OCR attempt returned 0 pages (backend={name})")
            continue
        pdf_total = _pdf_page_count(pdf_path)
        if pdf_total is not None and len(pages) != pdf_total:
            missing = pdf_total - len(pages)
            if missing > max(5, int(pdf_total * 0.2)):
                failures.append(f"{name}: page mismatch ({len(pages)}/{pdf_total})")
                logger.error(
                    f"[{job_id}] OCR page count mismatch (backend={name}): {failures[-1]}"
                )
                continue
        return pages, name, failures
    return [], "", failures


async def _run_pipeline_impl(job_id: str, pdf_path: str):
    """Pipeline implementation — guarded by per-job lock from run_pipeline."""
    db = await get_db()
    pipeline_start = time.time()

    try:
        # 清除上次运行的 error_message / finished_at（重试场景下避免 review
        # 页面残留旧的"任务处理失败"提示）。retry 端点也会清除，此处为兜底，
        # 覆盖直接调用 launch_pipeline 的路径（如未来新增的 API）。
        await db.execute(
            "UPDATE jobs SET error_message = NULL, finished_at = NULL, "
            "ocr_progress = NULL WHERE id = ?",
            (job_id,),
        )
        await db.commit()

        # ── Stage 1: OCR ────────────────────────────────────────
        await transition_status(db, job_id, "ocr_running", "Stage 1 start")
        ocr_backend = config["app"].ocr_backend
        await _audit_log(db, job_id, "pipeline_start",
                         f"pdf={Path(pdf_path).name} ocr_backend={ocr_backend}")
        logger.info(f"[{job_id}] Stage 1: Starting OCR (backend={ocr_backend})...")

        stage1_start = time.time()

        # Stage 1 流式反馈：OCR 轮询线程（to_thread）中回调进度 →
        # run_coroutine_threadsafe 调度回主事件循环 → 更新 job.ocr_progress
        # → SSE 推送，让用户实时看到 "OCR 12/51" 而不是 0% 白屏。
        loop = asyncio.get_running_loop()
        last_progress: tuple = (None, None)

        def _ocr_progress_cb(done: int, total: int):
            nonlocal last_progress
            if (done, total) == last_progress:
                return
            last_progress = (done, total)
            try:
                asyncio.run_coroutine_threadsafe(
                    _update_ocr_progress(job_id, done, total), loop
                )
            except Exception as e:
                logger.warning(f"[{job_id}] OCR progress callback failed: {e}")

        # OCR 分片（MinerU, OCR_SLICES>1）：流式输出 — 一片 OCR 完成即
        # 落库并开始该片页面分析，用户无需等全部页 OCR 完成才看到结果。
        # 整份 OCR（默认, 含 Paddle）保持原有阻塞式流程。
        slice_pages = int(getattr(config["app"], "ocr_slices", 1) or 1)
        stage1_ms = 0
        stage2_ms = 0
        failed_pages: list[int] = []
        if ocr_backend == "mineru" and slice_pages > 1:
            logger.info(
                f"[{job_id}] Stage 1 (sliced): OCR_SLICES={slice_pages} pages/slice, "
                f"streaming per-slice analysis enabled"
            )
            stage1_ms, stage2_ms, failed_pages, sliced_total = (
                await _run_sliced_stage1_2(
                    db, job_id, pdf_path, slice_pages, _ocr_progress_cb
                )
            )
            # 0 页兜底：所有片均失败/空
            if sliced_total <= 0:
                err_msg = f"OCR 返回 0 页（backend={ocr_backend}, sliced）— 上游服务未返回任何页面内容"
                logger.error(f"[{job_id}] {err_msg}")
                await db.execute(
                    "UPDATE jobs SET error_message = ?, finished_at = CURRENT_TIMESTAMP, "
                    "stage1_ms = ? WHERE id = ?",
                    (err_msg, stage1_ms, job_id),
                )
                await transition_status(db, job_id, "error", "OCR returned 0 pages")
                await db.commit()
                await _audit_log(db, job_id, "stage1_empty", err_msg)
                return
            # 取消检查（分片函数每片回调后已检查，此处兜底）
            if await _is_cancelled(job_id):
                return
            pages = []  # Stage 3 不依赖 pages（数据已在 page_cache）
        else:
            # 双 OCR 兜底：主后端失败（异常/0 页/严重缺页）自动切换备后端
            # 整单重试，job 记录实际使用的后端（jobs.ocr_backend_used）审计。
            pages, used_backend, ocr_failures = await _run_ocr_with_failover(
                db, job_id, pdf_path, _ocr_progress_cb
            )
            stage1_ms = int((time.time() - stage1_start) * 1000)
            if not pages:
                reason = ocr_failures[0] if ocr_failures else f"backend={ocr_backend}"
                err_parts = [f"OCR 处理失败（{ocr_backend}）— {reason}"]
                if len(ocr_failures) > 1:
                    err_parts.append(
                        "备选失败: " + "; ".join(ocr_failures[1:])
                    )
                err_msg = "; ".join(err_parts)[:2000]
                logger.error(f"[{job_id}] {err_msg}")
                await db.execute(
                    "UPDATE jobs SET error_message = ?, finished_at = CURRENT_TIMESTAMP, "
                    "stage1_ms = ? WHERE id = ?",
                    (err_msg, stage1_ms, job_id),
                )
                await transition_status(db, job_id, "error", "OCR failed (all backends)")
                await db.commit()
                await _audit_log(db, job_id, "stage1_failed", err_msg)
                return

            await db.execute(
                "UPDATE jobs SET ocr_backend_used = ? WHERE id = ?",
                (used_backend, job_id),
            )
            logger.info(
                f"[{job_id}] Stage 1: OCR complete: {len(pages)} pages "
                f"in {stage1_ms}ms (backend={used_backend})"
            )
            await _audit_log(
                db, job_id, "stage1_complete",
                f"pages={len(pages)} duration={stage1_ms}ms backend={used_backend}",
            )

            # 轻微页数差异（缺 ≤5 页且 ≤20%）→ 审计日志可见，不阻断
            pdf_total = _pdf_page_count(pdf_path)
            if pdf_total is not None and len(pages) != pdf_total:
                missing = pdf_total - len(pages)
                warn_msg = (
                    f"OCR 页数与 PDF 物理页数不一致: PDF {pdf_total} 页, "
                    f"OCR 返回 {len(pages)} 页（差异 {missing} 页）— 分析可能不完整"
                )
                logger.warning(f"[{job_id}] {warn_msg}")
                await _audit_log(db, job_id, "stage1_pagemismatch", warn_msg)

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
                # OCR 不完整标记（robustness-A2）：MinerU 低置信度丢弃块计数。
                # 在 raw_html 前注入警告行，LLM 能看到"此页 OCR 不完整"并降低
                # 分析置信度，前端原始页文本区域同样可见。
                discarded_count = page.get("_discarded_count")
                if discarded_count:
                    raw_html = (
                        f"[OCR 警告: 本页有 {discarded_count} 个内容块因置信度过低"
                        f"被 OCR 丢弃, 以下内容可能不完整, 分析仅供参考]\n\n{raw_html}"
                    )
                raw_html = _sanitize_ocr_text(raw_html)
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
            logger.info(
                f"[{job_id}] DB: page_cache inserted {new_pages} new pages, "
                f"jobs.total_pages={len(pages)} ({len(existing_pages)} cached)"
            )

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
            # concurrent execute, so all DB writes must be serialized via the
            # module-level db_lock (also used by _is_cancelled).
            state_lock = asyncio.Lock()
            completed = {"n": 0}
            total_pages = len(pages)
            sem = asyncio.Semaphore(concurrency)

            # Run all page analyses concurrently
            tasks = [
                _analyze_one(db, job_id, pn, pg, sem, failed_pages,
                             state_lock, completed, total_pages)
                for pn, pg in todo
            ]
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
                    if not data.get("_parse_error") and not data.get("_ocr_empty"):
                        page_structures.append({"page": row["page"], "data": data})
                except json.JSONDecodeError:
                    logger.warning(f"[{job_id}] Stage 3: Failed to parse page_cache page={row['page']}")

        # P-C3 修复：analyze_cross_page 调用前检查取消状态，
        # 避免取消后仍进入跨页分析（cancelling → review 非法转换）
        if await _is_cancelled(job_id):
            return
        findings = await analyze_cross_page(page_structures, job_id=job_id)
        # P-C3 修复：analyze_cross_page 调用后再检查一次取消状态，
        # 避免在跨页分析期间用户点取消后继续写入 findings / 转 review
        if await _is_cancelled(job_id):
            return
        stage3_ms = int((time.time() - stage3_start) * 1000)
        logger.info(
            f"[{job_id}] Stage 3: Cross-page analysis done in {stage3_ms}ms "
            f"({len(findings)} findings from {len(page_structures)} pages)"
        )

        # 保存 findings — 按 severity 统计，便于审计和调试
        # 流式输出优化：source='llm_page' 的 findings 已在 Stage 2 每页完成时
        # 写入 DB，此处跳过避免重复。只写入 rule/llm_cross/llm_fallback findings。
        #
        # 对抗审查(cr-1): retry 场景 — B4 指纹查重仅对确定性生成的 rule/
        # llm_fallback 有效；llm_cross/user_rule 的 description 由 LLM 自然
        # 语言生成（temperature=0.1），同一异常两次运行措辞几乎必然不同，
        # 无法指纹判重 → 每次 partial_review/error 重试都会重复插入。方案：
        # Stage 3 重算前删除本 job 待审（pending）的 LLM 生成型 findings
        # （含 user_rule），已人工裁决（confirmed/rejected/corrected）的保留。
        cur = await db.execute(
            "DELETE FROM findings WHERE job_id = ? "
            "AND source IN ('llm_cross', 'llm_fallback', 'user_rule') AND status = 'pending'",
            (job_id,),
        )
        await db.commit()
        severity_counts = {"critical": 0, "warning": 0, "info": 0}
        skipped_llm_page = 0
        inserted = 0
        for f in findings:
            sev = f.get("severity", "info")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
            # 跳过已在 Stage 2 写入的 page-level LLM findings
            if f.get("source") == "llm_page":
                skipped_llm_page += 1
                continue
            # robustness-B4: retry 会重新执行 Stage 3，rule/cross 发现是确定性
            # 生成的，重复 INSERT 会污染审计数据（同一描述翻倍）。先查后插：
            # 按 (job_id, source, page, type, description) 指纹判重，已存在则跳过。
            cur = await db.execute(
                "SELECT 1 FROM findings WHERE job_id = ? AND source = ? AND page = ? "
                "AND type = ? AND description = ? LIMIT 1",
                (job_id, f.get("source", "rule"), f["page"], f["type"], f["description"]),
            )
            if await cur.fetchone():
                logger.debug(
                    f"[{job_id}] findings dup skipped (page={f['page']} "
                    f"source={f.get('source')} type={f['type']})"
                )
                continue
            await db.execute(
                "INSERT INTO findings (job_id, page, type, severity, description, ocr_text, operator, source, user_rule_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, f["page"], f["type"], f["severity"], f["description"],
                 f.get("ocr_text"), f.get("operator"), f.get("source", "rule"),
                 f.get("rule_id") if f.get("source") == "user_rule" else None),
            )
            inserted += 1
        await db.commit()  # Flush findings before status transition (prevent ghost findings)
        logger.info(
            f"[{job_id}] DB: findings inserted ({inserted} new + {skipped_llm_page} "
            f"llm_page skipped, severity={severity_counts})"
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

        # 飞书通知（旁路：失败不影响主流程；成功/部分完成均推送）
        try:
            from core.notify import notify_job
            await notify_job(job_id, final_status)
        except Exception:
            pass  # notify_job 自身已兜底，此处双保险防异常逃逸

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
        # P-C3 修复：Stage 3 期间用户取消 → status=cancelling，pipeline 末尾
        # 调用 transition_status(..., "review") 会因 cancelling → review 非法
        # 而抛 InvalidTransitionError。原代码仅记日志不做状态恢复，job 卡在
        # cancelling（非终态）。此处检查当前状态：cancelling → cancelled（合法
        # 终态），否则 → error，确保 job 进入终态。
        logger.warning(f"[{job_id}] Invalid transition at pipeline end: {e}")
        try:
            cur = await db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
            cur_row = await cur.fetchone()
            cur_status = cur_row["status"] if cur_row else None
            if cur_status == "cancelling":
                await transition_status(db, job_id, "cancelled", "cancelled during stage 3")
            else:
                # 其他非预期状态 → error（直接 UPDATE，与 recover_stuck_jobs 同模式）
                await db.execute(
                    "UPDATE jobs SET status = 'error', error_message = ?, "
                    "finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (f"Pipeline failed: invalid transition {e}", job_id),
                )
                await db.commit()
        except Exception as recover_err:
            # 恢复失败也不抛出，避免异常逃逸导致 job 卡死；记日志供排查
            logger.error(
                f"[{job_id}] Recovery after InvalidTransition failed: {recover_err}",
                exc_info=True,
            )
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
            # 飞书通知（旁路，失败不影响主流程）
            try:
                from core.notify import notify_job
                await notify_job(job_id, "error")
            except Exception:
                pass  # notify_job 自身已兜底，此处双保险防异常逃逸
        except InvalidTransitionError as ie:
            # P-W7 修复：已是终态，直接更新 error_message；但 DB 写入本身可能
            # 失败（连接断开/锁竞争），用内层 try/except 兜住，避免异常逃逸
            # 导致 job 卡在非终态。记日志但不抛出。
            logger.warning(f"[{job_id}] {ie}")
            try:
                await db.execute(
                    "UPDATE jobs SET error_message = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (error_msg, job_id),
                )
                await db.commit()
            except Exception as audit_err:
                logger.error(
                    f"[{job_id}] Failed to update error_message after InvalidTransition: {audit_err}",
                    exc_info=True,
                )


async def _analyze_one(
    db, job_id: str, page_num: int, page: dict,
    sem: asyncio.Semaphore, failed_pages: list[int],
    state_lock: asyncio.Lock, completed: dict, total_pages: int,
) -> None:
    """并发 LLM 分析单页（Stage 2 的原子单元，整份/分片路径共用）。

    LLM call 在 semaphore 限制下并发；DB 写入经 db_lock 串行化，避免
    aiosqlite 共享连接上的 "Recursive use of cursors" 错误。

    流式输出：每页分析完成后立即把该页 findings 写入 findings 表
    （source='llm_page'），用户在 Stage 2 进行中就能在 review 页看到
    已分析页的结果，无需等 Stage 3 完成。Stage 3 的
    _collect_per_page_findings 会跳过已写入的 llm_page findings。

    Args:
        completed: 可变容器 {"n": 已完成页数}（跨路径共享统计）
    """
    # 取消检查：跳过后续页的 LLM 调用；已在运行的调用会自然结束
    # （HTTP 请求无法中途打断）。
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

            # robustness-B3: JSON 解析失败（_parse_error）不抛异常而是返回
            # 标记 dict（见 page_analyzer.analyze_page），此前此类页既不进
            # failed_pages 也不触发 partial_review，job 显示"成功"但实际
            # 缺页。此处与异常路径一致地归入 failed_pages（不 completed++）。
            if structured.get("_parse_error"):
                async with state_lock:
                    failed_pages.append(page_num)
                error_data = {
                    "page_number": page_num,
                    "_parse_error": True,
                    "_error": str(structured.get("_raw", ""))[:200],
                    "overall_confidence": "low",
                }
                async with db_lock:
                    await db.execute(
                        "UPDATE page_cache SET structured_json = ?, analyzed_at = CURRENT_TIMESTAMP "
                        "WHERE job_id = ? AND page = ?",
                        (json.dumps(error_data, ensure_ascii=False), job_id, page_num),
                    )
                    await db.commit()
                logger.warning(
                    f"[{job_id}] Stage 2: Page {page_num} JSON parse failure "
                    f"(counted as failed page)"
                )
                return

            payload = json.dumps(structured, ensure_ascii=False)
            confidence = structured.get("overall_confidence", "unknown")
            measurements_count = len(structured.get("measurements", []))
            page_findings = structured.get("findings", []) or []
            logger.info(
                f"[{job_id}] Stage 2: Page {page_num}/{total_pages} LLM done in {page_ms}ms "
                f"(confidence={confidence}, measurements={measurements_count}, "
                f"findings={len(page_findings)}, payload={len(payload)} bytes)"
            )
            async with db_lock:
                await db.execute(
                    "UPDATE page_cache SET structured_json = ?, analyzed_at = CURRENT_TIMESTAMP "
                    "WHERE job_id = ? AND page = ?",
                    (payload, job_id, page_num),
                )
                # 流式输出：立即把该页 LLM 产生的 findings 写入 findings 表
                for f in page_findings:
                    if not isinstance(f, dict):
                        continue
                    if not {"type", "severity", "description"}.issubset(f.keys()):
                        continue
                    # 对抗审查(cr-3): llm_page 路径补 B4 同款指纹查重 —
                    # 防御"部分提交残留 + retry"组合路径下的重复行。
                    dup_cur = await db.execute(
                        "SELECT 1 FROM findings WHERE job_id = ? AND source = 'llm_page' "
                        "AND page = ? AND type = ? AND description = ? LIMIT 1",
                        (job_id, page_num, f.get("type", "info"),
                         f.get("description", "")),
                    )
                    if await dup_cur.fetchone():
                        continue
                    await db.execute(
                        "INSERT INTO findings (job_id, page, type, severity, "
                        "description, ocr_text, operator, source) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'llm_page')",
                        (job_id, page_num, f.get("type", "info"),
                         f.get("severity", "info"), f.get("description", ""),
                         f.get("ocr_text", ""), f.get("operator", "")),
                    )
                await db.commit()
                logger.info(
                    f"[{job_id}] DB: page_cache updated + {len(page_findings)} "
                    f"page-level findings inserted (page={page_num})"
                )
        except Exception as e:
            logger.error(
                f"[{job_id}] Stage 2: Page {page_num} failed: {e}", exc_info=True
            )
            async with state_lock:
                failed_pages.append(page_num)
            error_data = {
                "page_number": page_num,
                "_parse_error": True,
                "_error": str(e)[:200],
                "overall_confidence": "low",
            }
            async with db_lock:
                # 对抗审查(cr-2): 异常可能发生在 llm_page findings 循环中
                # （NOT NULL 约束/类型错误），部分 INSERT 未提交。若不回滚，
                # 下方 UPDATE + commit 会把残留的半个事务一并提交，页面被标
                # 失败的同时留下半套 findings（retry 后与重分析结果重复）。
                try:
                    await db.rollback()
                except Exception as rb_err:
                    logger.warning(
                        f"[{job_id}] Stage 2: rollback failed (page={page_num}): {rb_err}"
                    )
                await db.execute(
                    "UPDATE page_cache SET structured_json = ?, analyzed_at = CURRENT_TIMESTAMP "
                    "WHERE job_id = ? AND page = ?",
                    (json.dumps(error_data, ensure_ascii=False), job_id, page_num),
                )
                await db.commit()
                logger.warning(f"[{job_id}] DB: page_cache updated with _parse_error (page={page_num})")
            return

    async with state_lock:
        completed["n"] += 1
    logger.info(
        f"[{job_id}] Stage 2: Page {page_num}/{total_pages} analyzed "
        f"({completed['n']} done)"
    )


async def _run_sliced_stage1_2(
    db, job_id: str, pdf_path: str, slice_pages: int, ocr_progress_cb,
) -> tuple[int, int, list[int], int]:
    """MinerU 分片 OCR + 渐进分析（流式输出核心，问题 2）。

    分片 OCR：PDF 切成多片独立提交 MinerU batch 并行轮询；**一片 OCR
    完成立即落库 page_cache 并启动该片页面的 LLM 分析**（不等其他片），
    用户无需等全部页 OCR 完成才看到 findings。

    返回 (stage1_ms, stage2_ms, failed_pages, total_pages)。
    取消时提前返回（stage1/2_ms 可能为部分值），主流程的取消检查兜底。
    """
    from core.mineru_client import run_ocr_sliced

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def _on_batch(start_page: int, pages: list, total: int):
        loop.call_soon_threadsafe(q.put_nowait, (start_page, pages, total))

    ocr_task: asyncio.Task = asyncio.create_task(
        asyncio.to_thread(
            run_ocr_sliced, pdf_path, slice_pages, _on_batch,
            ocr_progress_cb, job_id=job_id,
        )
    )

    concurrency = config["app"].llm_concurrency
    sem = asyncio.Semaphore(concurrency)
    state_lock = asyncio.Lock()
    failed_pages: list[int] = []
    completed = {"n": 0}
    analysis_tasks: list[asyncio.Task] = []

    stage1_start = time.time()
    existing = await _get_existing_pages(db, job_id)
    analyzed = await _get_analyzed_pages(db, job_id)
    new_pages = 0
    total_pages = 0
    seen_max = 0

    while True:
        try:
            start_page, pages, total = await asyncio.wait_for(q.get(), timeout=_SLICE_QUEUE_TIMEOUT)
        except asyncio.TimeoutError:
            if ocr_task.done():
                exc = ocr_task.exception()
                if exc is not None:
                    raise exc
                break
            continue
        total_pages = total
        seen_max = max(seen_max, start_page + len(pages) - 1)
        # 该片页面落库（INSERT OR IGNORE 兼容 resume）
        for i, page in enumerate(pages):
            page_num = start_page + i
            if page_num in existing:
                continue
            raw_html = _sanitize_ocr_text(page.get("markdown", {}).get("text", ""))
            await db.execute(
                "INSERT OR IGNORE INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
                (job_id, page_num, raw_html),
            )
            new_pages += 1
        await db.execute(
            "UPDATE jobs SET total_pages = ? WHERE id = ?", (seen_max, job_id)
        )
        await db.commit()
        logger.info(
            f"[{job_id}] Stage 1 (sliced): slice start={start_page} pages={len(pages)} "
            f"persisted ({new_pages} new)"
        )
        # 立即启动该片页面分析（跳过已分析页）
        for i, page in enumerate(pages):
            page_num = start_page + i
            if page_num in existing or page_num in analyzed:
                continue
            analysis_tasks.append(
                asyncio.create_task(
                    _analyze_one(
                        db, job_id, page_num, page, sem, failed_pages,
                        state_lock, completed, total_pages,
                    )
                )
            )
        if await _is_cancelled(job_id):
            return 0, 0, failed_pages, total_pages

    stage1_ms = int((time.time() - stage1_start) * 1000)
    logger.info(
        f"[{job_id}] Stage 1 (sliced): OCR complete: {new_pages} new pages "
        f"(total={total_pages}) in {stage1_ms}ms"
    )
    await _audit_log(db, job_id, "stage1_complete",
                     f"pages={total_pages} duration={stage1_ms}ms")
    await transition_status(db, job_id, "ocr_done", f"Stage 1 (sliced) complete: {total_pages} pages")
    await db.commit()
    if await _is_cancelled(job_id):
        return stage1_ms, 0, failed_pages, total_pages

    # Stage 2 收尾：等待所有已排队的分析任务（含最后一片刚入队的）
    await transition_status(db, job_id, "analyzing", "Stage 2 (sliced) start")
    stage2_start = time.time()
    await asyncio.gather(*analysis_tasks)
    stage2_ms = int((time.time() - stage2_start) * 1000)
    logger.info(
        f"[{job_id}] Stage 2 (sliced): Complete in {stage2_ms}ms, "
        f"{len(failed_pages)} pages failed"
    )
    await _audit_log(db, job_id, "stage2_complete",
                     f"duration={stage2_ms}ms failed={failed_pages}")
    return stage1_ms, stage2_ms, failed_pages, total_pages


async def _is_cancelled(job_id: str) -> bool:
    """Check if job has been cancelled. Transitions cancelling → cancelled via
    _transition_status_unlocked so the audit_log entry is written (no bypass).

    P-C2 修复：本函数已持 db_lock，调用 _transition_status_unlocked（不加锁）
    而非 transition_status，避免 asyncio.Lock 不可重入导致死锁。
    """
    db = await get_db()
    async with db_lock:
        cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        if row and row["status"] in ("cancelling", "cancelled", "error"):
            if row["status"] == "cancelling":
                # Use _transition_status_unlocked so audit_log captures the transition
                # （已持 db_lock，不能再调用会加锁的 transition_status）
                try:
                    await _transition_status_unlocked(db, job_id, "cancelled", "Pipeline acknowledged cancel")
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


async def _update_ocr_progress(job_id: str, done: int, total: int) -> None:
    """更新 job.ocr_progress（JSON）供 SSE 实时推送。

    由 OCR 轮询线程通过 run_coroutine_threadsafe 调用；用 db_lock
    串行化，避免与 Stage 2 的并发 DB 写入冲突。
    """
    db = await get_db()
    payload = json.dumps({"done": done, "total": total})
    try:
        async with db_lock:
            await db.execute(
                "UPDATE jobs SET ocr_progress = ? WHERE id = ?", (payload, job_id)
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[{job_id}] OCR progress update failed: {e}")


async def _get_existing_pages(db, job_id: str) -> set[int]:
    """Get page numbers that already have raw_html in page_cache."""
    cursor = await db.execute(
        "SELECT page FROM page_cache WHERE job_id = ?", (job_id,)
    )
    return {row["page"] for row in await cursor.fetchall()}


async def _get_analyzed_pages(db, job_id: str) -> set[int]:
    """Get page numbers that already have *successful* structured_json.

    关键修复：之前用 `structured_json IS NOT NULL` 判定已分析，但 LLM 解析
    失败的页也会写入 structured_json（含 `_parse_error: true` 标记），被
    误认为已分析而跳过 retry。这导致用户点"重试"后失败页不会被重新调用
    LLM，错误状态永久保留。

    修复：排除含 `_parse_error` 标记的页，让 retry 能重新分析它们。
    """
    cursor = await db.execute(
        "SELECT page, structured_json FROM page_cache "
        "WHERE job_id = ? AND structured_json IS NOT NULL",
        (job_id,),
    )
    analyzed = set()
    for row in await cursor.fetchall():
        sj = row["structured_json"] or ""
        # 快速包含 _parse_error 标记的检测（避免完整 JSON parse 开销）
        if '"_parse_error"' in sj and "true" in sj:
            # 精确校验：json_extract 在 SQLite 3.38+ 可用，回退到 Python parse
            try:
                import json as _json
                data = _json.loads(sj)
                if data.get("_parse_error"):
                    continue  # 跳过解析失败的页，retry 时重新分析
            except Exception:
                pass
        analyzed.add(row["page"])
    return analyzed
