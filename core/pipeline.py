"""Pipeline orchestration — stages 1→2→3 for a single job.

Stage 1: PaddleOCR-VL OCR → parse JSONL → save page_cache.raw_html
Stage 2: LLM page-by-page analysis → save page_cache.structured_json
Stage 3: Cross-page analysis → generate findings
"""
import asyncio
import json
import logging
import time
from pathlib import Path

from db.client import get_db
from core.ocr_client import run_ocr
from core.page_analyzer import analyze_page
from core.cross_page_analyzer import analyze_cross_page

logger = logging.getLogger(__name__)


async def run_pipeline(job_id: str, pdf_path: str):
    """Main async pipeline. Runs in background task."""
    db = await get_db()
    start_time = time.time()

    try:
        # ── Stage 1: OCR ────────────────────────────────────────
        await _update_status(db, job_id, "ocr_running")
        logger.info(f"[{job_id}] Stage 1: Starting OCR...")

        # OCR is synchronous (blocking network calls) — run in thread pool
        pages = await asyncio.to_thread(run_ocr, pdf_path)
        logger.info(f"[{job_id}] OCR complete: {len(pages)} pages")

        # Save raw HTML to page_cache
        for i, page in enumerate(pages):
            raw_html = page.get("markdown", {}).get("text", "")
            await db.execute(
                "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
                (job_id, i + 1, raw_html),
            )
        await db.execute(
            "UPDATE jobs SET status = 'ocr_done', total_pages = ? WHERE id = ?",
            (len(pages), job_id),
        )
        await db.commit()

        # ── Stage 2: Per-page LLM analysis ──────────────────────
        await _update_status(db, job_id, "analyzing")
        logger.info(f"[{job_id}] Stage 2: Analyzing {len(pages)} pages...")

        for i, page in enumerate(pages):
            raw_html = page.get("markdown", {}).get("text", "")
            structured = await analyze_page(raw_html, page_num=i + 1)
            await db.execute(
                "UPDATE page_cache SET structured_json = ?, analyzed_at = CURRENT_TIMESTAMP "
                "WHERE job_id = ? AND page = ?",
                (json.dumps(structured, ensure_ascii=False), job_id, i + 1),
            )
            await db.commit()
            logger.info(f"[{job_id}] Page {i+1}/{len(pages)} analyzed")

        # ── Stage 3: Cross-page analysis ────────────────────────
        logger.info(f"[{job_id}] Stage 3: Cross-page analysis...")
        cursor = await db.execute(
            "SELECT page, structured_json FROM page_cache WHERE job_id = ? ORDER BY page",
            (job_id,),
        )
        page_structures = []
        for row in await cursor.fetchall():
            if row["structured_json"]:
                page_structures.append({
                    "page": row["page"],
                    "data": json.loads(row["structured_json"]),
                })

        findings = await analyze_cross_page(page_structures)

        # Save findings
        for f in findings:
            await db.execute(
                "INSERT INTO findings (job_id, page, type, severity, description, ocr_text, operator) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, f["page"], f["type"], f["severity"], f["description"],
                 f.get("ocr_text"), f.get("operator")),
            )

        ocr_cost_ms = int((time.time() - start_time) * 1000)
        await db.execute(
            "UPDATE jobs SET status = 'review', finished_at = CURRENT_TIMESTAMP, ocr_cost_ms = ? "
            "WHERE id = ?",
            (ocr_cost_ms, job_id),
        )
        await db.commit()
        logger.info(f"[{job_id}] Pipeline complete: {len(findings)} findings in {ocr_cost_ms}ms")

    except Exception as e:
        logger.error(f"[{job_id}] Pipeline failed: {e}", exc_info=True)
        await db.execute(
            "UPDATE jobs SET status = 'error', error_message = ?, finished_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (str(e)[:500], job_id),
        )
        await db.commit()


async def _update_status(db, job_id: str, status: str):
    await db.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    await db.commit()
