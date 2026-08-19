"""Stage 2 — concurrent per-page LLM analysis (module refactor)."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from config import config
from core.page_analyzer import AnalysisCancelled
from core.pipeline.state import _audit_log, transition_status

logger = logging.getLogger(__name__)
async def _run_stage2_analysis(
    db, job_id: str, pages: list[dict], failed_pages: list[int],
) -> int:
    """Concurrent per-page LLM analysis; returns stage2_ms. (refactor)"""
    # Runtime resolution — tests patch core.pipeline._is_cancelled.
    from core.pipeline import _is_cancelled as _run_is_cancelled
    # Check cancellation
    if await _run_is_cancelled(job_id):
        return 0

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
        asyncio.create_task(
            _analyze_one(db, job_id, pn, pg, sem, failed_pages,
                         state_lock, completed, total_pages)
        )
        for pn, pg in todo
    ]
    # 取消时中止 in-flight 分析任务：与 sliced 路径（1275-1283）对齐 —
    # 否则取消后孤儿协程继续跑 LLM（单页最长 240s），应用退出时抛
    # "Task was destroyed"（对抗审查同款问题，整份路径此前未修）。
    # 结构化并发原则：任务不得游离于父作用域之外无观察者地运行。
    while tasks:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        tasks = list(pending)
        if await _run_is_cancelled(job_id):
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            break

    stage2_ms = int((time.time() - stage2_start) * 1000)
    logger.info(
        f"[{job_id}] Stage 2: Complete in {stage2_ms}ms, "
        f"{len(failed_pages)} pages failed"
    )
    await _audit_log(db, job_id, "stage2_complete",
                     f"duration={stage2_ms}ms failed={failed_pages}")
    return stage2_ms


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
    # Runtime resolution — tests rebuild core.pipeline.db_lock.
    from core.pipeline import db_lock
    # Cancellation check: skip further LLM calls for later pages
    # 取消检查：跳过后续页的 LLM 调用；已在运行的调用会自然结束
    # （HTTP 请求无法中途打断）。
    # Runtime resolution — tests patch core.pipeline.{_is_cancelled,
    # analyze_page}.
    from core.pipeline import (
        _is_cancelled as _run_is_cancelled,
        analyze_page as _run_analyze_page,
    )
    if await _run_is_cancelled(job_id):
        logger.info(f"[{job_id}] Stage 2: Skipped page {page_num} (cancelled)")
        return

    raw_html = page.get("markdown", {}).get("text", "")
    async with sem:
        try:
            try:
                page_start = time.time()
                # C1: Stage 2 单页开始日志 — long-running job（如 50 页 ×
                # 40s/页 ≈ 30min）需要能从 pipeline.log 实时定位当前分析页。
                logger.info(
                    f"[{job_id}] Stage 2: Page {page_num}/{total_pages} analyzing "
                    f"(len={len(raw_html)})"
                )
                # P1-2: 取消检查点注入 — analyze_page 在 LLM 调用之间（chat_json
                # 后 / schema 修复重试前）轮询 cancel_check，取消后不再发起新的
                # LLM 调用（此前重试链最长 ~12 分钟，cancel 后 job 迟迟不终态，
                # delete/archive 被拒且文案"数秒"严重不符）。
                structured = await _run_analyze_page(
                    raw_html, page_num=page_num, job_id=job_id,
                    cancel_check=lambda: _run_is_cancelled(job_id),
                )
                page_ms = int((time.time() - page_start) * 1000)
            except AnalysisCancelled:
                # 用户取消：该页不计 failed_pages（取消是用户动作，不是分析缺陷）
                logger.info(f"[{job_id}] Stage 2: Page {page_num} analysis cancelled")
                return

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
                        "UPDATE page_cache SET structured_json = ?, analyzed_at = datetime('now','localtime') "
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
                    "UPDATE page_cache SET structured_json = ?, analyzed_at = datetime('now','localtime') "
                    "WHERE job_id = ? AND page = ?",
                    (payload, job_id, page_num),
                )
                # 流式输出：立即把该页 LLM 产生的 findings 写入 findings 表。
                # 对抗审查(cr-3): llm_page 路径同样依赖 idx_findings_dedup UNIQUE
                # 索引（v5）做原子去重，防御"部分提交残留 + retry"组合路径下的重复行。
                llm_page_rows = []
                for f in page_findings:
                    if not isinstance(f, dict):
                        continue
                    if not {"type", "severity", "description"}.issubset(f.keys()):
                        continue
                    llm_page_rows.append((
                        job_id, page_num, f.get("type", "info"),
                        f.get("severity", "info"), f.get("description", ""),
                        f.get("ocr_text", ""), f.get("operator", ""),
                    ))
                if llm_page_rows:
                    await db.executemany(
                        "INSERT OR IGNORE INTO findings "
                        "(job_id, page, type, severity, description, ocr_text, operator, source, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'llm_page', datetime('now','localtime'))",
                        llm_page_rows,
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
                    "UPDATE page_cache SET structured_json = ?, analyzed_at = datetime('now','localtime') "
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
