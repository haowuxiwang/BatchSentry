"""Job management API — upload PDF, check status, cancel, retry."""
import logging
import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException

from config import config
from db.client import get_db
from core.pipeline import launch_pipeline, transition_status, InvalidTransitionError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# Phase 5A: stream upload in chunks instead of reading the whole PDF into
# memory. 8 MB chunks keep peak memory low even for 200 MB PDFs and let us
# enforce the size limit without ever holding the full file in RAM.
_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
_MAX_PDF_BYTES = 200 * 1024 * 1024  # 200 MB

# Concurrency guard — prevents memory exhaustion from many parallel pipelines.
# Each pipeline holds the OCR result + LLM JSON in memory; 3 concurrent 200MB
# PDFs with multi-page OCR results can hit ~2GB. Override via MAX_CONCURRENT_JOBS.
_MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))
_ACTIVE_STATUSES = ("pending", "ocr_running", "ocr_done", "analyzing", "cancelling")


@router.post("")
async def create_job(
    file: UploadFile = File(...),
):
    """Upload a PDF and start OCR + analysis pipeline."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files accepted")

    # Concurrency guard: count active jobs before accepting new work.
    db = await get_db()
    cursor = await db.execute(
        f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(_ACTIVE_STATUSES))})",
        _ACTIVE_STATUSES,
    )
    active_count = (await cursor.fetchone())[0]
    if active_count >= _MAX_CONCURRENT_JOBS:
        logger.warning(
            f"Upload rejected: {active_count} active jobs >= limit {_MAX_CONCURRENT_JOBS}"
        )
        raise HTTPException(
            409,
            f"已有 {active_count} 个任务在处理中，上限为 {_MAX_CONCURRENT_JOBS}。请等待完成或取消后再试。",
        )

    job_id = str(uuid.uuid4())[:12]
    # Phase 5B: use config output_dir (frozen mode → %APPDATA%/PBC/output)
    job_dir = Path(config["app"].output_dir) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize filename — strip path separators from uploaded name to prevent
    # path traversal via crafted Content-Disposition filenames.
    safe_name = Path(file.filename).name
    if not safe_name or safe_name in (".", ".."):
        safe_name = f"{job_id}.pdf"
    logger.info(f"[{job_id}] Upload start: name={safe_name}")
    pdf_path = job_dir / safe_name

    # Stream to disk in chunks; enforce size limit without loading full file
    total_bytes = 0
    try:
        with open(pdf_path, "wb") as f:
            while True:
                chunk = await file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > _MAX_PDF_BYTES:
                    f.close()
                    pdf_path.unlink(missing_ok=True)
                    raise HTTPException(400, "PDF too large (max 200MB)")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        pdf_path.unlink(missing_ok=True)
        # Don't leak internal paths/exception details to client
        logger.error(f"Upload write failed: {e}", exc_info=True)
        raise HTTPException(500, "Upload failed (disk write error)")

    # Magic bytes check: real PDFs start with %PDF-
    if total_bytes < 5:
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(400, "File too small to be a valid PDF")
    try:
        with open(pdf_path, "rb") as f:
            header = f.read(5)
        if header != b"%PDF-":
            pdf_path.unlink(missing_ok=True)
            logger.warning(f"[{job_id}] Upload rejected: bad magic bytes {header!r}")
            raise HTTPException(400, "File is not a valid PDF (missing %PDF- header)")
    except HTTPException:
        raise
    except Exception as e:
        pdf_path.unlink(missing_ok=True)
        logger.error(f"Magic bytes check failed: {e}", exc_info=True)
        raise HTTPException(500, "Upload validation failed")

    if total_bytes == 0:
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(400, "Empty file")

    await db.execute(
        "INSERT INTO jobs (id, filename, status, pdf_path) VALUES (?, ?, 'pending', ?)",
        (job_id, safe_name, str(pdf_path)),
    )
    await db.execute(
        "INSERT INTO audit_log (job_id, action, detail) VALUES (?, 'pipeline_start', ?)",
        (job_id, f"Uploaded {safe_name} ({total_bytes} bytes)"),
    )
    await db.commit()

    # Launch async pipeline（注册到 _pipeline_tasks 以便优雅关闭）
    launch_pipeline(job_id, str(pdf_path))
    logger.info(f"[{job_id}] Upload complete: {total_bytes} bytes, pipeline launched")

    return {"job_id": job_id, "filename": safe_name, "status": "pending"}


@router.get("/{job_id}")
async def get_job_status(job_id: str):
    """Get job status, progress, and findings summary."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, "Job not found")

    cursor = await db.execute(
        "SELECT COUNT(*) FROM page_cache WHERE job_id = ?", (job_id,)
    )
    pages_ocr = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM page_cache WHERE job_id = ? AND structured_json IS NOT NULL",
        (job_id,),
    )
    pages_analyzed = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM findings WHERE job_id = ?", (job_id,)
    )
    total_findings = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM findings WHERE job_id = ? AND status = 'pending'", (job_id,)
    )
    review_findings = (await cursor.fetchone())[0]

    return {
        "id": job["id"],
        "filename": job["filename"],
        "status": job["status"],
        "total_pages": job["total_pages"],
        "failed_pages": job["failed_pages"],
        "pages_ocr_done": pages_ocr,
        "pages_analyzed": pages_analyzed,
        "total_findings": total_findings,
        "review_findings": review_findings,
        "created_at": job["created_at"],
        "finished_at": job["finished_at"],
        "error_message": job["error_message"],
        "stage1_ms": job["stage1_ms"],
        "stage2_ms": job["stage2_ms"],
        "stage3_ms": job["stage3_ms"],
    }


# 终态：SSE 流遇到这些状态时关闭
_TERMINAL_STATUSES = ("review", "partial_review", "error", "cancelled", "archived")


async def _get_job_progress(db, job_id: str) -> dict:
    """获取 job 进度快照（SSE 推送用）。

    复用 get_job_status 的查询逻辑，但返回精简字段。
    """
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        return None

    cursor = await db.execute(
        "SELECT COUNT(*) FROM page_cache WHERE job_id = ?", (job_id,)
    )
    pages_ocr = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM page_cache WHERE job_id = ? AND structured_json IS NOT NULL",
        (job_id,),
    )
    pages_analyzed = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM findings WHERE job_id = ?", (job_id,)
    )
    total_findings = (await cursor.fetchone())[0]

    return {
        "id": job["id"],
        "status": job["status"],
        "total_pages": job["total_pages"] or 0,
        "pages_ocr_done": pages_ocr,
        "pages_analyzed": pages_analyzed,
        "total_findings": total_findings,
        "error_message": job["error_message"],
        "stage1_ms": job["stage1_ms"],
        "stage2_ms": job["stage2_ms"],
        "stage3_ms": job["stage3_ms"],
    }


@router.get("/{job_id}/stream")
async def stream_job_progress(job_id: str):
    """SSE 端点：实时推送 job 进度，直到终态。

    前端通过 EventSource 订阅，每 2 秒收到一次进度更新。
    遇到终态 (review/partial_review/error/cancelled/archived) 后推送最终状态并关闭。
    """
    import asyncio
    from fastapi.responses import StreamingResponse

    async def event_generator():
        db = await get_db()
        while True:
            progress = await _get_job_progress(db, job_id)
            if progress is None:
                yield f"event: error\ndata: {json.dumps({'message': 'Job not found'})}\n\n"
                return

            payload = json.dumps(progress, ensure_ascii=False)
            yield f"data: {payload}\n\n"

            if progress["status"] in _TERMINAL_STATUSES:
                yield f"event: done\ndata: {payload}\n\n"
                return

            await asyncio.sleep(3)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲（如有反向代理）
        },
    )


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Cancel a running job."""
    db = await get_db()
    try:
        await transition_status(db, job_id, "cancelling", "User requested cancel")
        logger.info(f"[{job_id}] Cancel requested by user")
    except InvalidTransitionError as e:
        logger.warning(f"[{job_id}] Cancel blocked: {e}")
        raise HTTPException(400, str(e))
    return {"ok": True, "status": "cancelling"}


@router.get("/{job_id}/pages/{page}")
async def get_page_data(job_id: str, page: int):
    """获取单页数据（AJAX 用）— raw_html + structured_json。

    前端翻页时通过此 API 获取数据，避免整页刷新。
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT raw_html, structured_json FROM page_cache WHERE job_id = ? AND page = ?",
        (job_id, page),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, f"Page {page} not found")

    # 提取 page_confidence + parse_error
    page_confidence = ""
    page_parse_error = False
    if row["structured_json"]:
        import json as _json
        try:
            data = _json.loads(row["structured_json"])
            page_parse_error = bool(data.get("_parse_error"))
            page_confidence = data.get("overall_confidence") or ""
        except _json.JSONDecodeError:
            pass

    return {
        "page": page,
        "raw_html": row["raw_html"] or "",
        "page_confidence": page_confidence,
        "page_parse_error": page_parse_error,
    }


@router.get("/{job_id}/findings")
async def get_page_findings(job_id: str, page: int = None):
    """获取 findings（可按页过滤）— AJAX 用。

    统一由 review.py 的 list_findings 处理（支持 page + status 过滤）。
    """
    db = await get_db()
    severity_order = (
        "CASE severity WHEN 'critical' THEN 0 "
        "WHEN 'warning' THEN 1 "
        "WHEN 'info' THEN 2 ELSE 3 END"
    )
    source_order = (
        "CASE COALESCE(source, 'rule') WHEN 'rule' THEN 0 "
        "WHEN 'llm_fallback' THEN 1 "
        "WHEN 'llm_page' THEN 2 "
        "WHEN 'llm_cross' THEN 3 ELSE 4 END"
    )
    if page:
        cursor = await db.execute(
            f"SELECT * FROM findings WHERE job_id = ? AND page = ? "
            f"ORDER BY {severity_order}, {source_order}, id",
            (job_id, page),
        )
    else:
        cursor = await db.execute(
            f"SELECT * FROM findings WHERE job_id = ? "
            f"ORDER BY {severity_order}, {source_order}, id",
            (job_id,),
        )
    findings = [dict(r) for r in await cursor.fetchall()]
    return {"findings": findings, "count": len(findings), "page": page}


@router.post("/{job_id}/retry")
async def retry_job(job_id: str):
    """Retry a failed or cancelled job from where it left off."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, "Job not found")

    if not job["pdf_path"] or not Path(job["pdf_path"]).exists():
        raise HTTPException(400, "PDF file not found on disk")

    try:
        await transition_status(db, job_id, "pending", f"Retry from {job['status']}")
        logger.info(f"[{job_id}] Retry requested from status={job['status']}")
    except InvalidTransitionError as e:
        logger.warning(f"[{job_id}] Retry blocked: {e}")
        raise HTTPException(400, str(e))

    launch_pipeline(job_id, job["pdf_path"])
    return {"ok": True, "status": "pending"}


# Phase 6: Job lifecycle — archive, delete, auto-cleanup
# 生产环境必需：PDF 和数据库会无限累积，需要归档/删除机制

@router.post("/{job_id}/archive")
async def archive_job(job_id: str, keep_pdf: bool = True):
    """归档 job — 标记为已归档，从前端列表隐藏，但保留数据用于审计。

    Args:
        keep_pdf: True（默认）保留 PDF 用于审计追溯；False 删除 PDF 释放磁盘。
                  数据库记录始终保留。
    """
    db = await get_db()
    try:
        await transition_status(db, job_id, "archived", "User archived")
        logger.info(f"[{job_id}] Archived by user (keep_pdf={keep_pdf})")
    except InvalidTransitionError as e:
        logger.warning(f"[{job_id}] Archive blocked: {e}")
        raise HTTPException(400, str(e))

    # 可选：归档时删除 PDF 释放磁盘（数据库记录保留）
    if not keep_pdf:
        cursor = await db.execute("SELECT pdf_path FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        if row and row["pdf_path"]:
            pdf = Path(row["pdf_path"])
            output_root = Path(config["app"].output_dir).resolve()
            try:
                job_dir = pdf.parent.resolve()
                job_dir.relative_to(output_root)
                if pdf.exists():
                    import shutil
                    shutil.rmtree(job_dir, ignore_errors=True)
                    logger.info(f"[{job_id}] Archived + PDF removed: {job_dir}")
            except (ValueError, RuntimeError) as e:
                logger.warning(f"[{job_id}] Archive PDF cleanup skipped (path check): {e}")

    return {"ok": True, "status": "archived"}


@router.post("/{job_id}/unarchive")
async def unarchive_job(job_id: str):
    """取消归档 — 恢复到 review 状态。"""
    db = await get_db()
    try:
        await transition_status(db, job_id, "review", "User unarchived")
        logger.info(f"[{job_id}] Unarchived by user")
    except InvalidTransitionError as e:
        logger.warning(f"[{job_id}] Unarchive blocked: {e}")
        raise HTTPException(400, str(e))
    return {"ok": True, "status": "review"}


@router.delete("/{job_id}")
async def delete_job(job_id: str, keep_pdf: bool = False):
    """彻底删除 job — 删除数据库记录 + PDF 文件。

    生产环境清理必需，避免数据无限累积。

    Args:
        keep_pdf: True 时保留 PDF 原文件（用于审计），False 时一并删除
    """
    import json as _json
    import shutil

    db = await get_db()
    cursor = await db.execute("SELECT pdf_path, status, filename FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Job not found")

    pdf_path = row["pdf_path"]
    logger.info(f"[{job_id}] Delete requested: filename={row['filename']} keep_pdf={keep_pdf}")

    # 删除数据库记录（级联删除 page_cache, findings, audit_log, llm_call_audit）
    await db.execute("DELETE FROM page_cache WHERE job_id = ?", (job_id,))
    await db.execute("DELETE FROM findings WHERE job_id = ?", (job_id,))
    await db.execute("DELETE FROM audit_log WHERE job_id = ?", (job_id,))
    await db.execute("DELETE FROM llm_call_audit WHERE job_id = ?", (job_id,))
    await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    await db.commit()

    # 删除 PDF 文件
    if not keep_pdf and pdf_path:
        pdf = Path(pdf_path)
        # Security: validate job_dir is inside output_dir to prevent
        # rmtree on arbitrary paths if pdf_path was tampered with.
        output_root = Path(config["app"].output_dir).resolve()
        try:
            job_dir = pdf.parent.resolve()
            job_dir.relative_to(output_root)
        except (ValueError, RuntimeError):
            logger.error(
                f"[{job_id}] Refused delete: job_dir {pdf.parent} "
                f"outside output_root {output_root}"
            )
            raise HTTPException(400, "Refused to delete: path outside output dir")
        if pdf.exists():
            # Phase 7: rmtree failures (file locked on Windows) no longer
            # leave the system inconsistent — DB records are already deleted
            # above, so we log a warning + return partial_success. The
            # orphaned dir is harmless (no DB references it) and can be
            # cleaned up by the user or a future gc pass.
            try:
                shutil.rmtree(job_dir, ignore_errors=False)
                logger.info(f"[{job_id}] Removed job_dir: {job_dir}")
            except (OSError, shutil.Error) as e:
                logger.warning(
                    f"[{job_id}] rmtree failed (DB already cleaned): {e}"
                )
                # Mark as best-effort: DB is consistent, file remains.
                # Caller can retry deletion or manually clean output_dir.
                return {
                    "ok": True, "deleted": True, "keep_pdf": keep_pdf,
                    "warning": f"数据库记录已删除，但文件清理失败: {e}",
                }
        else:
            logger.warning(f"[{job_id}] PDF missing on delete: {pdf_path}")
    logger.info(f"[{job_id}] Delete complete")

    return {"ok": True, "deleted": True, "keep_pdf": keep_pdf}


@router.get("/archived/list")
async def list_archived():
    """列出已归档的 jobs。"""
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, filename, status, total_pages, created_at, finished_at "
        "FROM jobs WHERE status = 'archived' ORDER BY created_at DESC LIMIT 100"
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    return {"archived": rows, "count": len(rows)}


@router.get("/stats/overview")
async def stats_overview():
    """数据库存储统计 — 用于监控累积情况。"""
    import os
    db = await get_db()

    # 数据库文件大小
    db_path = config["app"].database_path
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    # 各表记录数
    cursor = await db.execute("SELECT COUNT(*) FROM jobs")
    total_jobs = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM jobs WHERE status != 'archived'")
    active_jobs = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM page_cache")
    total_pages = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM findings")
    total_findings = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM audit_log")
    total_audit = (await cursor.fetchone())[0]

    # output 目录大小
    output_dir = Path(config["app"].output_dir)
    pdf_size = 0
    pdf_count = 0
    if output_dir.exists():
        for f in output_dir.rglob("*.pdf"):
            pdf_size += f.stat().st_size
            pdf_count += 1

    return {
        "database": {
            "path": str(db_path),
            "size_mb": round(db_size / 1024 / 1024, 2),
        },
        "jobs": {
            "total": total_jobs,
            "active": active_jobs,
            "archived": total_jobs - active_jobs,
        },
        "page_cache": total_pages,
        "findings": total_findings,
        "audit_log": total_audit,
        "pdf_storage": {
            "dir": str(output_dir),
            "count": pdf_count,
            "size_mb": round(pdf_size / 1024 / 1024, 2),
        },
    }
