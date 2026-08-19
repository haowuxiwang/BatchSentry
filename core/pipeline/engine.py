"""Pipeline engine: launch/run entry points + _run_pipeline_impl + sliced path (module refactor)"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from config import config
from db.client import get_db
from core.pipeline.locks import (
    _pipeline_locks, _pipeline_tasks, _locks_guard, _SLICE_QUEUE_TIMEOUT,
)
from core.pipeline.ocr_support import _sanitize_ocr_text
from core.pipeline.state import (
    InvalidTransitionError, _audit_log, transition_status,
)
from core.pipeline.stage1 import _run_stage1_full, _get_existing_pages
from core.pipeline.stage2 import (
    _analyze_one,
    _run_stage2_analysis,
    _get_analyzed_pages,
)
from core.pipeline.stage3 import _run_stage3_cross_analysis
from core.security import redact_urls

logger = logging.getLogger(__name__)
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

    from core.pipeline import run_pipeline as _run_pipeline
    task = asyncio.create_task(_run_pipeline(job_id, pdf_path))
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

    progress_futures：OCR 线程经 run_coroutine_threadsafe 调度回主 loop 的
    进度更新 future。测试场景 loop 即刻关闭会导致协程悬挂（RuntimeWarning
    "was never awaited"）；统一在收尾 flush（超时 1s，不阻塞退出）。

    Per-job lock 用引用计数管理注册表条目（对抗审查 T3.1）：
    - 每个进入 run_pipeline 的协程 refs +1，退出（无论是否拿到锁/被取消）-1
    - 仅当 refs 归零才 pop 条目 —— 等待中的协程仍持有 refs，不会发生
      "等待者被取消时误删持有者锁 entry" 的并发窗口（旧实现无脑 pop：
      A 持锁 B 等待时 B 被取消会删掉 A 的锁条目，第三个协程 C 随后
      setdefault 建新锁而与 B 并行执行同一 job）。
    """
    progress_futures: list = []
    # Acquire per-job lock — prevents two pipelines on the same job_id.
    # Registry entry = {lock, refs}; refs counts every coroutine that
    # entered run_pipeline (holding or waiting/cancelled) so the entry is
    # only removed when no one is involved with this job anymore.
    async with _locks_guard:
        entry = _pipeline_locks.setdefault(job_id, {"lock": asyncio.Lock(), "refs": 0})
        refs_before = entry["refs"]
        entry["refs"] += 1
        lock = entry["lock"]
    if refs_before > 0:
        logger.info(f"[{job_id}] Lock contention: another pipeline still draining, waiting...")
    try:
        if lock.locked():
            logger.info(f"[{job_id}] Per-job lock held by another coroutine, waiting to acquire")
        async with lock:
            logger.info(f"[{job_id}] Per-job lock acquired")
            await _run_pipeline_impl(job_id, pdf_path, progress_futures)
    finally:
        # Flush any in-flight progress coroutines before the loop may close
        if progress_futures:
            try:
                real_futures = [
                    f for f in progress_futures if asyncio.isfuture(f)
                ]
                if real_futures:
                    await asyncio.wait_for(
                        asyncio.gather(*real_futures, return_exceptions=True),
                        timeout=1.0,
                    )
            except asyncio.TimeoutError:
                pass
            progress_futures.clear()
        # Cleanup lock entry — only when the last coroutine for this job
        # exits (waiter cancellation must not delete the holder's entry).
        popped = None
        async with _locks_guard:
            cur = _pipeline_locks.get(job_id)
            if cur is not None:
                cur["refs"] -= 1
                if cur["refs"] <= 0:
                    popped = _pipeline_locks.pop(job_id, None)
        removed = popped
        logger.info(
            f"[{job_id}] Per-job lock released "
            f"(registry_removed={removed is not None})"
        )
        # NOTE: PDF 文件不在此处删除 — 复核页 /api/jobs/{id}/pdf 需要它。
        # PDF 在 job 删除 (DELETE /api/jobs/{id}) 或归档时清理。
        # OCR 完成后 raw_html 已存入数据库，但 PDF 仍需保留用于人工复核预览。
        logger.info(f"[{job_id}] Pipeline exited, PDF retained for review: {Path(pdf_path).name}")


async def _run_pipeline_impl(job_id: str, pdf_path: str, progress_futures: list):
    """Pipeline implementation — guarded by per-job lock from run_pipeline."""
    # Runtime resolution — tests patch core.pipeline.{_is_cancelled,
    # _update_ocr_progress}.
    from core.pipeline import (
        _is_cancelled as _run_is_cancelled,
        _update_ocr_progress as _run_update_ocr_progress,
    )
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
                fut = asyncio.run_coroutine_threadsafe(
                    _run_update_ocr_progress(job_id, done, total), loop
                )
                progress_futures.append(fut)
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
                    "UPDATE jobs SET error_message = ?, finished_at = datetime('now','localtime'), "
                    "stage1_ms = ? WHERE id = ?",
                    (err_msg, stage1_ms, job_id),
                )
                await transition_status(db, job_id, "error", "OCR returned 0 pages")
                await db.commit()
                await _audit_log(db, job_id, "stage1_empty", err_msg)
                return
            # 取消检查（分片函数每片回调后已检查，此处兜底）
            if await _run_is_cancelled(job_id):
                return
            pages = []  # Stage 3 不依赖 pages（数据已在 page_cache）
        else:
            # Stage 1 (模块化): 整份 OCR 流程已拆入 _run_stage1_full —
            # 复用检测 / 双后端 failover / page_cache 写入 / 空页自愈。
            stage_out = await _run_stage1_full(
                db, job_id, pdf_path, _ocr_progress_cb, loop
            )
            if stage_out is None:
                return
            pages, used_backend, stage1_ms, failed_pages = stage_out
            # ── Stage 2 (整份路径专属; 切片路径已在 _run_sliced_stage1_2 完成)
            stage2_ms = await _run_stage2_analysis(
                db, job_id, pages, failed_pages
            )
        # ── Stage 3 (两路径共享) ─────────────────────────────────
        await _run_stage3_cross_analysis(
            db, job_id, stage1_ms, stage2_ms, failed_pages, pipeline_start
        )


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
        final_recovery_status = None
        try:
            cur = await db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
            cur_row = await cur.fetchone()
            cur_status = cur_row["status"] if cur_row else None
            if cur_status == "cancelling":
                await transition_status(db, job_id, "cancelled", "cancelled during stage 3")
                final_recovery_status = "cancelled"
            else:
                # 其他非预期状态 → error（直接 UPDATE，与 recover_stuck_jobs 同模式）
                await db.execute(
                    "UPDATE jobs SET status = 'error', error_message = ?, "
                    "finished_at = datetime('now','localtime') WHERE id = ?",
                    (f"Pipeline failed: invalid transition {e}", job_id),
                )
                await db.commit()
                final_recovery_status = "error"
        except Exception as recover_err:
            # 恢复失败也不抛出，避免异常逃逸导致 job 卡死；记日志供排查
            logger.error(
                f"[{job_id}] Recovery after InvalidTransition failed: {recover_err}",
                exc_info=True,
            )
        # 与正常 error 终态一致地发终态通知（对抗审查：此前该恢复路径
        # 静默无通知，GMP 任务失败用户不知道）
        if final_recovery_status:
            try:
                from core.notify import notify_job

                await notify_job(job_id, final_recovery_status)
            except Exception:
                pass  # notify_job 自身已兜底，此处双保险防异常逃逸
    except Exception as e:
        logger.error(f"[{job_id}] Pipeline failed: {redact_urls(str(e))}", exc_info=True)
        await _audit_log(db, job_id, "pipeline_error", redact_urls(str(e))[:200])
        # P1-7：签名 URL 反刍兜底 — OCR 客户端异常消息可能回显完整签名 URL
        # （requests MaxRetryError），error_message 会出现在报告/飞书通知中
        error_msg = redact_urls(str(e))[:500]
        try:
            await transition_status(db, job_id, "error", f"Pipeline failed: {redact_urls(str(e))[:100]}")
            # transition_status 只更新 status 字段，还需显式写入 error_message + finished_at
            await db.execute(
                "UPDATE jobs SET error_message = ?, finished_at = datetime('now','localtime') WHERE id = ?",
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
                    "UPDATE jobs SET error_message = ?, finished_at = datetime('now','localtime') WHERE id = ?",
                    (error_msg, job_id),
                )
                await db.commit()
            except Exception as audit_err:
                logger.error(
                    f"[{job_id}] Failed to update error_message after InvalidTransition: {audit_err}",
                    exc_info=True,
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
    # Runtime resolution — tests patch core.pipeline.{_is_cancelled,
    # _update_ocr_progress} and rebuild core.pipeline.db_lock.
    from core.pipeline import _is_cancelled as _run_is_cancelled
    from core.pipeline import db_lock
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
    # 对抗审查 P1-1：记录已收到回调的页区间，OCR 结束后计算缺口。
    # 原实现只在 seen_max < total_pages 时补记尾页 — 中间片失败时
    # （如第 2 片失败、第 3 片成功）seen_max 可达 total，缺页静默通过。
    received_ranges: list[tuple[int, int]] = []

    while True:
        try:
            start_page, pages, total = await asyncio.wait_for(q.get(), timeout=_SLICE_QUEUE_TIMEOUT)
        except asyncio.TimeoutError:
            if ocr_task.done():
                exc = ocr_task.exception()
                if exc is not None:
                    # P1-1 修复：单片 OCR 失败不再整单 error — 已产出的片
                    # 照常分析（analysis_tasks 已入队），缺失页由下方
                    # stage1_complete 前的缺页补记并入 failed_pages，job
                    # 以 partial_review 收尾，用户可见而非静默炸单。
                    logger.warning(
                        f"[{job_id}] Sliced OCR failed mid-way: {exc} — "
                        f"continuing with {seen_max} produced pages"
                    )
                    await _audit_log(db, job_id, "stage1_slice_failed", str(exc)[:500])
                break
            continue
        total_pages = total
        if pages:
            seen_max = max(seen_max, start_page + len(pages) - 1)
            received_ranges.append((start_page, start_page + len(pages) - 1))
        # 该片页面落库（INSERT OR IGNORE 兼容 resume）
        # P1-3 修复：落库 + commit 整体持 db_lock — 否则与 _analyze_one 的
        # rollback() 竞争：分析失败回滚会撤销本循环尚未提交的 INSERT，
        # 随后本循环的 commit 提交空事务 → 该片 raw_html 永久丢失。
        async with db_lock:
            for i, page in enumerate(pages):
                page_num = start_page + i
                if page_num in existing:
                    continue
                raw_html = page.get("markdown", {}).get("text", "")
                # OCR 不完整标记（与整份路径 653-658 保持一致）：sliced 路径
                # 曾遗漏 _discarded_count 警告注入 — 分片模式下同一页由单片
                # OCR 产出，丢弃块的风险同样存在，LLM 需感知。
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
                # P0-1 修复（与整份路径一致）：回写内存，_analyze_one 读取
                # 清洗+警告版而非原始文本。
                page["markdown"]["text"] = raw_html
                new_pages += 1
            await db.execute(
                # 对抗审查 P1-1 关联：必须用 split_pdf 的权威总数 total_pages，
                # 不能用 seen_max — 中间片失败时 seen_max < 真实页数，会把
                # jobs.total_pages 永久覆盖成小值（复核页导航/报告全错）
                "UPDATE jobs SET total_pages = ? WHERE id = ?", (total_pages, job_id)
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
        if await _run_is_cancelled(job_id):
            # 取消已排队的分析任务：否则孤儿协程继续跑 LLM（最长 240s）
            # 并写入已取消的 job（对抗审查 — retry 后新旧 pipeline 对同一
            # 页写入互相竞争，应用退出时还抛 "Task was destroyed"）。
            for t in analysis_tasks:
                t.cancel()
            if analysis_tasks:
                await asyncio.gather(*analysis_tasks, return_exceptions=True)
            return 0, 0, failed_pages, total_pages

    stage1_ms = int((time.time() - stage1_start) * 1000)
    # 对抗审查 P1-1：缺页显式暴露 — 按"已收到回调的区间"计算全部缺口
    # （不仅限尾页）。中间片失败（第 2 片失败、第 3 片成功）时原逻辑
    # seen_max==total 会漏报，GMP 复核页数错乱而 job 仍显示成功。
    if total_pages:
        received_ranges.sort()
        missing_pages: list[int] = []
        ptr = 1
        for s, e in received_ranges:
            for pn in range(ptr, min(s, total_pages + 1)):
                missing_pages.append(pn)
            ptr = max(ptr, e + 1)
        for pn in range(ptr, total_pages + 1):
            missing_pages.append(pn)
        for pn in missing_pages:
            if pn not in failed_pages:
                failed_pages.append(pn)
        if missing_pages:
            logger.warning(
                f"[{job_id}] Sliced OCR pages missing: {missing_pages} "
                f"(total={total_pages}, produced={received_ranges})"
            )
            await _audit_log(
                db, job_id, "stage1_pagemismatch",
                f"sliced: missing {len(missing_pages)}/{total_pages} pages "
                f"ranges={received_ranges}",
            )
    logger.info(
        f"[{job_id}] Stage 1 (sliced): OCR complete: {new_pages} new pages "
        f"(total={total_pages}) in {stage1_ms}ms"
    )
    # 分片路径也记录实际后端（分片仅 MinerU 支持）— 与整单路径 :529 对齐，
    # 保证 GMP 审计字段 ocr_backend_used 在所有路径下都有值
    await db.execute(
        "UPDATE jobs SET ocr_backend_used = ? WHERE id = ?", ("mineru", job_id)
    )
    await _audit_log(db, job_id, "stage1_complete",
                     f"pages={total_pages} duration={stage1_ms}ms")
    await transition_status(db, job_id, "ocr_done", f"Stage 1 (sliced) complete: {total_pages} pages")
    await db.commit()
    if await _run_is_cancelled(job_id):
        # 与片内取消分支一致：先取消并等待已排队分析任务再退出
        for t in analysis_tasks:
            t.cancel()
        if analysis_tasks:
            await asyncio.gather(*analysis_tasks, return_exceptions=True)
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
