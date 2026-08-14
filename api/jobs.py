"""Job management API — upload PDF, check status, cancel, retry."""
import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

import fitz  # PyMuPDF — 页码 PNG 渲染

from fastapi import APIRouter, UploadFile, File, HTTPException, Request
from fastapi.responses import Response

from config import config
from db.client import get_db
from core.pipeline import launch_pipeline, transition_status, InvalidTransitionError, db_lock

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
# 对抗审查(cr-11): 非法 env 值兜底为默认 3，避免 import 崩溃。
try:
    _MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))
except (TypeError, ValueError):
    _MAX_CONCURRENT_JOBS = 3
_ACTIVE_STATUSES = ("pending", "ocr_running", "ocr_done", "analyzing", "cancelling")

# PDF 文档句柄缓存（fitz Document）— 页码 PNG 渲染用，避免每次翻页重新
# 打开大 PDF（200MB 文件打开需数秒）。简单容量 + TTL 淘汰。
_pdf_doc_cache: dict[str, tuple] = {}  # job_id -> (fitz.Document, last_ts)
_PDF_CACHE_MAX = 6
_PDF_CACHE_TTL = 1800.0  # 秒 — 大 PDF（百 MB 级）重开成本高，拉长 TTL
# 渲染输出上限：批记录扫描件常为 300dpi+（单页 27MP），按 72dpi 基准
# zoom=1.5 输出 4500x6000px/9-18MB PNG，浏览器解码慢且每翻页重传。
# 限制输出宽度 ≤2000px（CSS fit-width 实际显示 ~1000px，2000px 足够清晰），
# 体积下降 ~10 倍。zoom 取 min(1.5, 2000/page_width_pt)。
_PDF_RENDER_ZOOM = 1.5  # ~108 dpi，批记录扫描件清晰度/体积平衡
_PDF_RENDER_MAX_W = 2000  # 输出最大宽度（px）


def _pdf_render_zoom(page) -> float:
    """按页面物理宽度自适应渲染缩放，输出宽度不超过 _PDF_RENDER_MAX_W。"""
    w_pt = max(page.rect.width, 1.0)
    return min(_PDF_RENDER_ZOOM, _PDF_RENDER_MAX_W / w_pt)


def _get_pdf_doc(job_id: str, pdf_path: str):
    """取 job 的 fitz Document 句柄（带缓存），缓存过期/超容量时淘汰。"""
    now = time.time()
    stale = [k for k, (_, ts) in _pdf_doc_cache.items() if now - ts > _PDF_CACHE_TTL]
    for k in stale:
        try:
            _pdf_doc_cache[k][0].close()
        except Exception:
            pass
        _pdf_doc_cache.pop(k, None)
    cached = _pdf_doc_cache.get(job_id)
    if cached:
        _pdf_doc_cache[job_id] = (cached[0], now)
        return cached[0]
    doc = fitz.open(pdf_path)
    if len(_pdf_doc_cache) >= _PDF_CACHE_MAX:
        oldest = min(_pdf_doc_cache, key=lambda k: _pdf_doc_cache[k][1])
        try:
            _pdf_doc_cache[oldest][0].close()
        except Exception:
            pass
        _pdf_doc_cache.pop(oldest, None)
    _pdf_doc_cache[job_id] = (doc, now)
    return doc


@router.post("")
async def create_job(
    file: UploadFile = File(...),
    force: bool = False,
):
    """Upload a PDF and start OCR + analysis pipeline.

    Duplicate detection: the MD5 of the streamed content is stored in
    jobs.md5; re-uploading identical content returns 409 unless force=1
    (query param), which lets users re-analyze the same batch record
    intentionally (e.g. after SOP/rule changes).
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files accepted")

    # 友好拦截：未配置 LLM 服务商时拒绝上传。
    # 批记录审查核心价值是 LLM 结构化分析，未配置时上传必然在 analysis
    # 阶段失败，浪费用户上传时间（PDF 可能很大）。拦截比"上传后失败"体验更好。
    # 注意：config 的键是 "providers"（与 config.py:200 / main.py:227 /
    # llm/client.py:30 保持一致），不是 "llm_providers"。早期实现误用
    # "llm_providers" 导致 production 永远拿不到 provider，所有上传被错误拒绝。
    # 检查"是否有非空 api_key"即可（UI 的 _is_real_api_key 严格筛掉 test
    # 占位 key，那是 UI 显示用途；上传守卫只需要"用户配过任意 key"）。
    providers = config.get("providers", {}) or {}
    has_any_key = any(
        bool(p.get("api_key")) if isinstance(p, dict)
        else bool(getattr(p, "api_key", None))
        for p in providers.values()
    )
    if not has_any_key:
        logger.warning("Upload rejected: no LLM provider configured")
        raise HTTPException(
            400,
            "尚未配置 LLM 服务商，无法进行结构化分析。请先前往「设置」完成配置后再上传。",
        )

    # Concurrency guard: count active jobs before accepting new work.
    # 性能优化：COUNT 检查在 db_lock 内（保证读一致性），但 PDF 写盘移到锁外，
    # 避免大文件上传期间阻塞所有 DB 操作（cancel/retry/transition_status）。
    # COUNT 与 INSERT 之间有间隙，但 MAX_CONCURRENT_JOBS 是软限制，
    # 偶尔多一个 job 不会导致系统崩溃（pipeline 内部有 per-job lock 保护）。
    db = await get_db()
    async with db_lock:
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

    # PDF 写盘 + 校验在 db_lock 外执行，不阻塞其他 DB 操作
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
    file_md5 = hashlib.md5()
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
                file_md5.update(chunk)
    except HTTPException:
        raise
    except Exception as e:
        pdf_path.unlink(missing_ok=True)
        # Don't leak internal paths/exception details to client
        logger.error(f"Upload write failed: {e}", exc_info=True)
        raise HTTPException(500, "Upload failed (disk write error)")
    content_md5 = file_md5.hexdigest()

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

    # 上传后立即读取 PDF 总页数 — OCR/分析期间 review 页面就能显示正确的
    # "X / Y" 页码（之前 total_pages 在 OCR 完成后才写入，导致显示 "1 / 0"）。
    # 用 PyMuPDF (fitz) 读取，开销 < 100ms 即使 200MB PDF。
    # 线程池执行避免阻塞事件循环（损坏/超大 PDF 的 xref 修复可能秒级）。
    def _count_pdf_pages_sync(path: str) -> int:
        import fitz  # PyMuPDF
        with fitz.open(path) as doc:
            return doc.page_count

    pdf_page_count = 0
    try:
        pdf_page_count = await asyncio.to_thread(_count_pdf_pages_sync, str(pdf_path))
        logger.info(f"[{job_id}] PDF page count: {pdf_page_count}")
    except Exception as e:
        # 读页数失败不阻断上传 — pipeline 仍会在 OCR 完成后设置 total_pages
        logger.warning(f"[{job_id}] Failed to read PDF page count: {e}")
        pdf_page_count = 0

    # INSERT 在 db_lock 内（与去重检查 + 其他 DB 写入序列化，避免两个相同
    # 上传并发都通过检查）。去重：内容 md5 相同 → 409 提示已有任务，不创建
    # 重复 job（重复全流程 OCR/LLM 是纯浪费）。force=1 绕过（同一批记录在
    # 规则/SOP 变更后重新分析的合法场景）。
    async with db_lock:
        try:
            cursor = await db.execute(
                "SELECT id, filename, status, created_at FROM jobs "
                "WHERE md5 = ? AND status != 'archived' "
                "ORDER BY created_at DESC LIMIT 1",
                (content_md5,),
            )
            dup = await cursor.fetchone()
            if dup and not force:
                logger.info(
                    f"[{job_id}] Upload rejected: duplicate content of job {dup['id']}"
                )
                shutil.rmtree(job_dir, ignore_errors=True)
                raise HTTPException(
                    409,
                    f"该文件已上传过（任务 {dup['id']}「{dup['filename']}」，"
                    f"状态 {dup['status']}）。点击历史记录即可查看；"
                    f"确需重新分析请先删除旧任务或重新上传（将创建新任务）。",
                )
            await db.execute(
                "INSERT INTO jobs (id, filename, status, pdf_path, total_pages, md5) "
                "VALUES (?, ?, 'pending', ?, ?, ?)",
                (job_id, safe_name, str(pdf_path), pdf_page_count or None, content_md5),
            )
            await db.execute(
                "INSERT INTO audit_log (job_id, action, detail) VALUES (?, 'pipeline_start', ?)",
                (job_id, f"Uploaded {safe_name} ({total_bytes} bytes, {pdf_page_count} pages)"),
            )
            await db.commit()
        except HTTPException:
            # 去重 409 等已由业务分支 raise 的异常直接透传，不落入通用兜底
            raise
        except Exception as e:
            # INSERT 失败时清理孤儿 PDF 文件 + job_dir，避免磁盘累积
            logger.error(f"[{job_id}] DB INSERT failed, cleaning up job_dir: {e}", exc_info=True)
            try:
                await db.rollback()
            except Exception:
                pass
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(500, "数据库写入失败，请重试")

    # Launch async pipeline（注册到 _pipeline_tasks 以便优雅关闭）
    launch_pipeline(job_id, str(pdf_path))
    logger.info(f"[{job_id}] Upload complete: {total_bytes} bytes, pipeline launched")

    return {"job_id": job_id, "filename": safe_name, "status": "pending"}


@router.get("")
async def list_jobs(page: int = 1, page_size: int = 20):
    """List active (non-archived) jobs with pagination.

    Returns JSON for AJAX-loaded history list (no full page reload).
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 100))  # cap to prevent abuse
    offset = (page - 1) * page_size

    db = await get_db()
    count_cursor = await db.execute(
        "SELECT COUNT(*) FROM jobs WHERE status != 'archived'"
    )
    total_jobs = (await count_cursor.fetchone())[0]
    total_pages = (total_jobs + page_size - 1) // page_size

    cursor = await db.execute(
        "SELECT id, filename, status, total_pages, created_at, finished_at, pdf_path, ocr_progress "
        "FROM jobs WHERE status != 'archived' "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    # Don't expose pdf_path in JSON response
    for r in rows:
        r.pop("pdf_path", None)
        r["ocr_progress"] = _parse_ocr_progress(r.get("ocr_progress"))

    return {
        "jobs": rows,
        "page": page,
        "page_size": page_size,
        "total_jobs": total_jobs,
        "total_pages": total_pages,
    }


async def _live_jobs_snapshot(db) -> list[dict]:
    """收集所有活跃任务的进度快照（/api/jobs/live 推送体）。

    抽成纯函数便于单测（httpx ASGITransport 无法交付永不结束的
    SSE 流——它要等 app 完成后才返回 Response）。
    """
    placeholders = ",".join("?" * len(_ACTIVE_STATUSES))
    cursor = await db.execute(
        f"SELECT id FROM jobs WHERE status IN ({placeholders})",
        _ACTIVE_STATUSES,
    )
    rows = await cursor.fetchall()
    snapshots = []
    for r in rows:
        progress = await _get_job_progress(db, r["id"])
        if progress:
            snapshots.append(progress)
    return snapshots


@router.get("/live")
async def stream_all_live_jobs(request: Request):
    """SSE 聚合端点：单连接推送所有活跃任务的进度快照。

    解决 HTTP/1.1 每域 6 条 EventSource 连接上限：upload 页多任务并行
    时不再每个任务各开一条连接（MAX_CONCURRENT_JOBS=3 时多标签页
    很容易撞上限），而是聚合为一条流，事件体为
    {"jobs": [{job 快照}, ...]}，前端按 job_id 分发。
    """
    import asyncio
    from fastapi.responses import StreamingResponse

    async def event_generator():
        db = await get_db()
        seq = 0
        yield "retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                return
            snapshots = await _live_jobs_snapshot(db)
            seq += 1
            yield f"id: {seq}\ndata: {json.dumps({'jobs': snapshots}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{job_id}/page/{page_num}")
async def get_job_page_image(job_id: str, page_num: int):
    """Render a PDF page to PNG for inline preview.

    替代浏览器原生 PDF viewer（iframe）：新版 Chromium/Electron 的 PDF
    viewer 忽略 toolbar=0 参数，自带打印/下载/更多操作按钮且缩放不可控。
    PyMuPDF 渲染 PNG 后以 <img> 展示 — 无浏览器工具栏、缩放 fit-width
    由 CSS 控制、页码与渲染页严格对应。

    Security: pdf_path 校验同 serve_pdf（必须在 output_dir 内，防路径穿越）。
    page_num 1-based；越界返回 404。文档句柄缓存 _pdf_doc_cache 避免
    大 PDF 反复打开。
    """
    db = await get_db()
    cursor = await db.execute("SELECT pdf_path FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row or not row["pdf_path"]:
        raise HTTPException(404, "PDF not found")
    pdf_path = Path(row["pdf_path"]).resolve()
    output_root = Path(config["app"].output_dir).resolve()
    try:
        pdf_path.relative_to(output_root)
    except ValueError:
        logger.warning(
            f"Path traversal blocked: pdf_path={pdf_path} outside output_dir={output_root}"
        )
        raise HTTPException(403, "Access denied")
    if not pdf_path.exists():
        raise HTTPException(404, "PDF file missing")
    try:
        doc = _get_pdf_doc(job_id, str(pdf_path))
    except Exception as e:
        logger.error(f"[{job_id}] Failed to open PDF for rendering: {e}")
        raise HTTPException(500, "PDF cannot be rendered")
    if page_num < 1 or page_num > doc.page_count:
        raise HTTPException(404, f"Page out of range (1-{doc.page_count})")
    # 渲染在线程池执行：大扫描页 get_pixmap 需秒级，同步执行会阻塞整个
    # 事件循环（所有 API/SSE 请求排队，前端"正在渲染"卡住）。尺寸由
    # _pdf_render_zoom 限制（≤2000px 宽，体积 ~10x 下降，解码更快）。
    def _render_sync() -> bytes:
        page = doc.load_page(page_num - 1)
        zoom = _pdf_render_zoom(page)
        pix = page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom), alpha=False, colorspace=fitz.csRGB
        )
        return pix.tobytes("png")

    try:
        png = await asyncio.to_thread(_render_sync)
    except Exception as e:
        logger.error(f"[{job_id}] Page render failed (p{page_num}): {e}")
        raise HTTPException(500, "Page render failed")
    return Response(
        content=png,
        media_type="image/png",
        # 注：Cache-Control 由 main.py 全局中间件统一设置（非 /static/ 一律 no-cache）
    )


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

    page_finding_counts = await _page_finding_counts(db, job_id)

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
        "ocr_progress": _parse_ocr_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None),
        "page_finding_counts": page_finding_counts,
        "ocr_backend_used": job["ocr_backend_used"] if "ocr_backend_used" in job.keys() else None,
    }


async def _page_finding_counts(db, job_id: str) -> dict[int, dict]:
    """每页 finding 统计（severity）— 前端页码导航圆点实时更新用。"""
    cursor = await db.execute(
        "SELECT page, severity, COUNT(*) AS cnt FROM findings "
        "WHERE job_id = ? GROUP BY page, severity",
        (job_id,),
    )
    counts: dict[int, dict] = {}
    for r in await cursor.fetchall():
        p = r["page"]
        entry = counts.setdefault(
            p, {"critical": 0, "warning": 0, "info": 0, "total": 0}
        )
        sev = r["severity"]
        if sev in entry:
            entry[sev] += r["cnt"]
        entry["total"] += r["cnt"]
    return counts


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

    page_finding_counts = await _page_finding_counts(db, job_id)

    return {
        "id": job["id"],
        "status": job["status"],
        "total_pages": job["total_pages"] or 0,
        "pages_ocr_done": pages_ocr,
        "pages_analyzed": pages_analyzed,
        "total_findings": total_findings,
        "error_message": job["error_message"],
        "failed_pages": job["failed_pages"],
        "stage1_ms": job["stage1_ms"],
        "stage2_ms": job["stage2_ms"],
        "stage3_ms": job["stage3_ms"],
        "ocr_progress": _parse_ocr_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None),
        "page_finding_counts": page_finding_counts,
        "ocr_backend_used": job["ocr_backend_used"] if "ocr_backend_used" in job.keys() else None,
    }


def _parse_ocr_progress(raw) -> dict:
    """解析 jobs.ocr_progress JSON 字符串 → {"done": N, "total": M}。

    容忍 None / 空 / 非法 JSON（返回空 dict，前端回退到 pages_ocr_done）。
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {"done": int(data.get("done", 0)), "total": int(data.get("total", 0))}
    except (ValueError, TypeError):
        pass
    return {}


@router.get("/{job_id}/stream")
async def stream_job_progress(job_id: str, request: Request):
    """SSE 端点：实时推送 job 进度，直到终态。

    前端通过 EventSource 订阅，每 2 秒收到一次进度更新。
    遇到终态 (review/partial_review/error/cancelled/archived) 后推送最终状态并关闭。
    """
    import asyncio
    from fastapi.responses import StreamingResponse

    async def event_generator():
        db = await get_db()
        seq = 0
        yield "retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                logger.info(f"[{job_id}] SSE client disconnected, stopping progress stream")
                return
            progress = await _get_job_progress(db, job_id)
            if progress is None:
                seq += 1
                # 注意：不能用 `event: error` 帧 — SSE 规范中 error 是保留事件
                # 类型，浏览器收到后立即断开连接且不暴露 data，前端无法区分
                # "job 不存在" 与网络抖动。改用普通 message 帧携带 type 字段。
                yield (f"id: {seq}\n"
                       f"data: {json.dumps({'type': 'error', 'message': 'Job not found'}, ensure_ascii=False)}\n\n")
                return

            payload = json.dumps(progress, ensure_ascii=False)
            seq += 1
            # 每条事件带自增 id：EventSource 断线重连时自动携带
            # Last-Event-ID；快照是幂等全量，重放/重连后立即自愈。
            yield f"id: {seq}\ndata: {payload}\n\n"

            if progress["status"] in _TERMINAL_STATUSES:
                seq += 1
                yield f"id: {seq}\nevent: done\ndata: {payload}\n\n"
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


@router.post("/{job_id}/retry")
async def retry_job(job_id: str):
    """Retry a failed or cancelled job from where it left off."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, "Job not found")

    # Concurrency guard: retry must respect MAX_CONCURRENT_JOBS like upload.
    # Without this, mass-retrying failed jobs spawns unbounded pipelines,
    # defeating the memory protection (OCR results held in RAM per job).
    db_active = await get_db()
    async with db_lock:
        cursor = await db_active.execute(
            f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(_ACTIVE_STATUSES))})",
            _ACTIVE_STATUSES,
        )
        active_count = (await cursor.fetchone())[0]
        if active_count >= _MAX_CONCURRENT_JOBS:
            raise HTTPException(
                409,
                f"已有 {active_count} 个任务在处理中，上限为 {_MAX_CONCURRENT_JOBS}。请等待完成或取消后再试。",
            )

    if not job["pdf_path"] or not Path(job["pdf_path"]).exists():
        raise HTTPException(400, "PDF file not found on disk")

    try:
        await transition_status(db, job_id, "pending", f"Retry from {job['status']}")
        # 清除上次的 error_message，避免 review 页面残留旧的错误提示
        # （recover_stuck_jobs / 之前失败会写入 error_message，重试应视为全新尝试）
        await db.execute(
            "UPDATE jobs SET error_message = NULL, finished_at = NULL WHERE id = ?",
            (job_id,),
        )
        await db.commit()
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
                # 对抗审查(cr-8): 纵深防御 — pdf_path 恰为 output 根时
                # relative_to 返回 '.' 通过校验，rmtree 会删除全部 job 目录
                if job_dir == output_root or job_dir.name != job_id:
                    logger.warning(
                        f"[{job_id}] Archive PDF cleanup skipped (unsafe path: {job_dir})"
                    )
                elif pdf.exists():
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
    # 对抗审查(cr-9): 归档时 keep_pdf=False 会删除 PDF，恢复后 review 页
    # PDF 预览 404、retry 报"PDF file not found"。无 PDF 的 job 允许恢复
    # 查看 findings（数据仍完整），但给出明确提示由前端展示。
    cursor = await db.execute("SELECT pdf_path FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    pdf_missing = bool(row and (not row["pdf_path"] or not Path(row["pdf_path"]).exists()))
    try:
        await transition_status(db, job_id, "review", "User unarchived")
        logger.info(f"[{job_id}] Unarchived by user (pdf_missing={pdf_missing})")
    except InvalidTransitionError as e:
        logger.warning(f"[{job_id}] Unarchive blocked: {e}")
        raise HTTPException(400, str(e))
    return {"ok": True, "status": "review", "pdf_missing": pdf_missing}


@router.delete("/{job_id}")
async def delete_job(job_id: str, keep_pdf: bool = False):
    """彻底删除 job — 删除数据库记录 + PDF 文件。

    生产环境清理必需，避免数据无限累积。

    Args:
        keep_pdf: True 时保留 PDF 原文件（用于审计），False 时一并删除
    """
    import shutil

    db = await get_db()
    cursor = await db.execute("SELECT pdf_path, status, filename FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Job not found")

    # 安全检查：拒绝删除正在运行的 job。运行中的 pipeline task 仍会
    # 向 DB / 文件系统写入，强行删除会留下孤儿 task 与不一致状态。
    # 用户应先 cancel 等待终态后再删除。
    if row["status"] in _ACTIVE_STATUSES:
        logger.warning(
            f"[{job_id}] Delete blocked: job is active (status={row['status']})"
        )
        if row["status"] == "cancelling":
            msg = "任务正在取消中，请等待取消完成（可能需要数秒等 LLM 调用返回）后再删除。"
        else:
            msg = f"任务正在处理中（状态: {row['status']}），请先取消并等待任务进入终态后再删除。"
        raise HTTPException(409, msg)

    pdf_path = row["pdf_path"]
    # P-ADV3 修复：DELETE 操作的 audit_log INSERT + 级联 DELETE 必须在 db_lock
    # 内原子执行，与 pipeline 的 transition_status / _record_llm_call 共享
    # 同一锁，防止 aiosqlite 单连接上的事务边界被穿插（一个 commit 可能
    # 提前提交另一方的未完成写入）。
    async with db_lock:
        # Record deletion in a separate audit row (survives cascade delete)
        # GMP traceability: record destruction must itself be traceable
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail) VALUES (?, ?, ?)",
            ("_system", "job_deleted", f"job_id={job_id} filename={row['filename']} status={row['status']} keep_pdf={keep_pdf}"),
        )
        logger.info(f"[{job_id}] Delete requested: filename={row['filename']} keep_pdf={keep_pdf}")

        # 删除数据库记录（级联删除 page_cache, findings, audit_log, llm_call_audit）
        try:
            await db.execute("DELETE FROM page_cache WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM findings WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM audit_log WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM llm_call_audit WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # 删除 PDF 文件
    if not keep_pdf and pdf_path:
        pdf = Path(pdf_path)
        # Security: validate job_dir is inside output_dir to prevent
        # rmtree on arbitrary paths if pdf_path was tampered with.
        output_root = Path(config["app"].output_dir).resolve()
        try:
            job_dir = pdf.parent.resolve()
            job_dir.relative_to(output_root)
            # 对抗审查(cr-8): 纵深防御 — pdf_path 恰为 output 根时
            # relative_to 返回 '.' 通过校验，rmtree 会删除全部 job 目录
            if job_dir == output_root:
                raise ValueError("job_dir is output root")
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
