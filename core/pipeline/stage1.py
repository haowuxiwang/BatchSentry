"""Stage 1 — full-path OCR orchestration (module refactor)"""
from __future__ import annotations

import asyncio
import logging
import time

from config import config
from core.pipeline.ocr_support import _sanitize_ocr_text
from core.pipeline.state import _audit_log, transition_status

logger = logging.getLogger(__name__)
async def _run_stage1_full(
    db, job_id: str, pdf_path: str, progress_cb, loop,
) -> tuple | None:
    """Non-sliced OCR flow; returns (pages, used_backend, stage1_ms,
    failed_pages) or None on early exit (cancel / fatal). (refactor)"""
    # Runtime resolution — tests patch core.pipeline.{_is_cancelled,
    # _pdf_page_count, _run_ocr_with_failover}.
    from core.pipeline import (
        _is_cancelled as _run_is_cancelled,
        _pdf_page_count as _run_pdf_page_count,
        _run_ocr_with_failover as _run_failover,
    )
    stage1_start = time.time()
    failed_pages: list[int] = []
    ocr_backend = config["app"].ocr_backend
    # （典型场景：Stage 2/3 失败后 retry），跳过真实 OCR，直接从缓存
    # 重建 pages 列表进入 Stage 2。省掉整个 PDF 重传重 OCR（上游配额
    # + 数分钟等待）。仅整份路径生效（分片路径按片流的语义，不复用）。
    reuse_pages = None
    cursor = await db.execute(
        "SELECT total_pages FROM jobs WHERE id = ?", (job_id,)
    )
    jrow = await cursor.fetchone()
    target_pages = (jrow["total_pages"] or 0) if jrow else 0
    if target_pages > 0:
        cursor = await db.execute(
            "SELECT raw_html FROM page_cache WHERE job_id = ? AND raw_html IS NOT NULL "
            "ORDER BY page",
            (job_id,),
        )
        cached_rows = await cursor.fetchall()
        if len(cached_rows) >= target_pages:
            reuse_pages = [
                {"markdown": {"text": r["raw_html"] or ""}}
                for r in cached_rows
            ]
            await _audit_log(
                db, job_id, "stage1_skipped",
                f"reuse {len(reuse_pages)} cached pages (no re-OCR)",
            )
            logger.info(
                f"[{job_id}] Stage 1 skipped: reusing {len(reuse_pages)} cached pages"
            )
    if reuse_pages is not None:
        # used_backend="cached" 仅用于日志/审计；不覆盖 jobs.ocr_backend_used
        # （保留上次真实后端，见下方 UPDATE 条件）。
        pages, used_backend, ocr_failures = reuse_pages, "cached", []
    else:
        # 双 OCR 兜底：主后端失败（异常/0 页/严重缺页）自动切换备后端
        # 整单重试，job 记录实际使用的后端（jobs.ocr_backend_used）审计。
        pages, used_backend, ocr_failures = await _run_failover(
            db, job_id, pdf_path, progress_cb
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
            "UPDATE jobs SET error_message = ?, finished_at = datetime('now','localtime'), "
            "stage1_ms = ? WHERE id = ?",
            (err_msg, stage1_ms, job_id),
        )
        await transition_status(db, job_id, "error", "OCR failed (all backends)")
        await db.commit()
        await _audit_log(db, job_id, "stage1_failed", err_msg)
        return

    if used_backend != "cached":
        # F3: 缓存复用路径不覆盖 jobs.ocr_backend_used — 保留上次真实后端
        # 供 GMP 溯源（"cached" 不是 OCR 后端，写进去会污染审计显示）。
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

    # 轻微页数差异（缺 ≤5 页且 ≤20%）→ 不阻断，但必须显式暴露：
    # P1-4 修复 — 缺失页码并入 failed_pages，job 以 partial_review 收尾，
    # 复核页横幅列出缺失页码，规则层虽无法分析缺页但用户可见，
    # 不再是"静默通过"（GMP 合规工具的核心要求）。
    pdf_total = await asyncio.to_thread(_run_pdf_page_count, pdf_path)
    if pdf_total is not None and len(pages) != pdf_total:
        missing = pdf_total - len(pages)
        warn_msg = (
            f"OCR 页数与 PDF 物理页数不一致: PDF {pdf_total} 页, "
            f"OCR 返回 {len(pages)} 页（差异 {missing} 页）— 分析可能不完整"
        )
        logger.warning(f"[{job_id}] {warn_msg}")
        await _audit_log(db, job_id, "stage1_pagemismatch", warn_msg)
        missing_pages = list(range(len(pages) + 1, pdf_total + 1))
        for pn in missing_pages:
            if pn not in failed_pages:
                failed_pages.append(pn)

    if await _run_is_cancelled(job_id):
        return None

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
        # P0-1 修复：回写内存 dict — Stage 2 (_analyze_one) 从内存
        # page dict 读取文本，此前只写 DB 不回写 → 首次运行 LLM 收到
        # 未 sanitize、无丢弃警告前缀的原始文本（警告注入/清洗仅在
        # retry 复用 DB 的路径上生效）。回写后两路数据一致。
        page["markdown"]["text"] = raw_html
        new_pages += 1
    await db.execute(
        "UPDATE jobs SET total_pages = ? WHERE id = ?",
        (len(pages), job_id),
    )

    # 空页自动重试（OCR 完整性抗挫折）：
    # MinerU 服务端处理超大 PDF（百 MB 级）存在丢页缺陷 — 同一页
    # 在大文件里输出空、小切片后完整识别（51 页实测丢 6 页，单页
    # 切片 1111-1702 字符全部完整）。对空页做小批量切片重跑并替换
    # page_cache，重跑仍空则保留原样（服务端也识别不了，走既有
    # _ocr_empty 提示路径）。只对 mineru 且页数 >= 10 的文件触发，
    # 避免小文件多余开销；sliced 路径（OCR_SLICES>1）本身就按片跑。
    # F5d 覆盖面修复：原实现只检查本次 OCR 新返回的 pages 且要求
    # used_backend=="mineru" — retry 复用缓存（F3, used_backend=
    # "cached"）时空页检测被跳过，丢页缺陷时期遗留的历史空页永远
    # 不会被恢复（真实 51pages job 6 页空页实证，用户复核只见
    # "## 第 N 页" 标题）。改为对 page_cache 统一检出空页，后端
    # 判定兼容 cached（回查 jobs.ocr_backend_used 保留的上次真实后端）。
    self_heal_backend = used_backend
    if self_heal_backend == "cached":
        cursor = await db.execute(
            "SELECT ocr_backend_used FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        self_heal_backend = row["ocr_backend_used"] if row else ""

    if self_heal_backend in ("mineru", "paddle"):
        from core.pipeline import _self_heal_empty_pages as _run_heal
        await _run_heal(db, job_id, pdf_path, pages, self_heal_backend)

    await transition_status(db, job_id, "ocr_done", f"Stage 1 complete: {len(pages)} pages")
    await db.commit()
    logger.info(
        f"[{job_id}] DB: page_cache inserted {new_pages} new pages, "
        f"jobs.total_pages={len(pages)} ({len(existing_pages)} cached)"
    )
    return pages, used_backend, stage1_ms, failed_pages




async def _get_existing_pages(db, job_id: str) -> set[int]:
    """Get page numbers that already have raw_html in page_cache."""
    cursor = await db.execute(
        "SELECT page FROM page_cache WHERE job_id = ?", (job_id,)
    )
    return {row["page"] for row in await cursor.fetchall()}
