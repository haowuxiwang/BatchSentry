"""BatchSentry — FastAPI entry point."""
import logging
import sys
from contextlib import asynccontextmanager

import re
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, FileResponse
from markupsafe import Markup

from config import config
from db.client import get_db, close_db
from logging_config import setup_logging, generate_request_id, request_id_var
from core.pipeline import recover_stuck_jobs

setup_logging()
logger = logging.getLogger(__name__)

# Application version — single source of truth.
# Avoids duplicate hardcoded "1.0.0" in FastAPI(app=...) and /health endpoint.
APP_VERSION = "1.0.0"


# Phase 5B: resolve resource paths under both dev and PyInstaller frozen mode.
# In frozen mode, sys._MEIPASS points to the temporary bundle directory where
# PyInstaller unpacks data files (templates/, static/).
def _resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).parent


_RESOURCE_DIR = _resource_dir()
_TEMPLATES_DIR = _RESOURCE_DIR / "templates"
_STATIC_DIR = _RESOURCE_DIR / "static"

# Ensure static dir exists (dev mode: always; frozen mode: should already exist)
_STATIC_DIR.mkdir(parents=True, exist_ok=True)

from api.jobs import router as jobs_router
from api.review import router as review_router
from api.report import router as report_router
from api.settings import router as settings_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting BatchSentry...")
    logger.info(f"  resource_dir: {_RESOURCE_DIR}")
    logger.info(f"  templates_dir: {_TEMPLATES_DIR}")
    logger.info(f"  static_dir: {_STATIC_DIR}")
    logger.info(f"  database_path: {config['app'].database_path}")
    logger.info(f"  output_dir: {config['app'].output_dir}")
    logger.info(f"  frozen: {getattr(sys, 'frozen', False)}")
    await get_db()  # initializes schema on first connect
    # 启动时恢复卡死的 job（应用上次崩溃/强杀时留下的非终态 job）
    # 异步执行：不阻塞 lifespan yield，避免 Electron 首屏延迟
    async def _recover_bg():
        try:
            recovered = await recover_stuck_jobs()
            if recovered:
                logger.warning(f"  recovered {recovered} stuck jobs (marked as error)")
        except Exception as e:
            logger.error(f"  stuck job recovery failed: {e}", exc_info=True)

    import asyncio as _asyncio
    _asyncio.create_task(_recover_bg())
    yield
    await close_db()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="BatchSentry",
    description="GMP 批生产记录半自动合规检查系统",
    version=APP_VERSION,
    lifespan=lifespan,
)

# Phase 8 adversarial review: tightened CORS — only 127.0.0.1 variants.
# Removed localhost:* to align with project constraint (127.0.0.1 only).
# Electron renderer loads http://127.0.0.1:58765/, dev server uses 8000.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8000",   # dev server (uvicorn)
        "http://127.0.0.1:58765",  # Electron default port
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE"],  # PUT: /api/settings/rules
    allow_headers=["Content-Type", "X-Request-ID"],
    allow_credentials=False,
)

# Gzip 压缩 — 生产环境减少传输体积（CSS/JS/JSON 等文本响应）
app.add_middleware(GZipMiddleware, minimum_size=1024)

# 安全响应头中间件 — CSP / X-Content-Type-Options / X-Frame-Options / Referrer-Policy
# 防御 clickjacking、MIME sniffing、XSS（CSP 禁止内联脚本和外部资源）
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    # CSP: 只允许同源资源。
    # script-src 'unsafe-inline': 模板用内联 <script> 注入 window.__PBC__
    #   SSR 桥接数据（Jinja2 → JS），onclick 调用外部 JS 函数。
    #   后续可用 CSP nonce 重构移除 'unsafe-inline'。
    # style-src 'unsafe-inline': Tailwind 工具类需要内联样式。
    # frame-ancestors 'self' + X-Frame-Options SAMEORIGIN:
    #   允许同源 iframe 嵌入 PDF 预览（review.html 的 <iframe src="/api/jobs/{id}/pdf">），
    #   仍禁止跨站嵌入（clickjacking 防御不削弱）。
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'self'; "
        "base-uri 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "0"  # 现代浏览器用 CSP，关闭旧的 XSS Auditor
    # 静态资源缓存：CSS/JS/字体长期缓存（文件名不变即可），HTML 不缓存
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    else:
        response.headers["Cache-Control"] = "no-cache"
    return response

# Mount static files
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# Phase 6: request_id 中间件 — 为每个 HTTP 请求注入追踪 ID + 请求/响应日志
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """注入 request_id 到每条日志和响应头，并记录请求/响应摘要。

    日志格式：
      [req_id] METHOD path → STATUS (duration_ms)
    排除 /static/ 和 /health 以减少噪声。
    """
    import time as _time
    import re as _re
    raw_id = request.headers.get("X-Request-ID") or ""
    req_id = raw_id if _re.fullmatch(r"[a-zA-Z0-9_\-]{8,64}", raw_id) else generate_request_id()
    token = request_id_var.set(req_id)
    path = request.url.path
    # 静态文件和健康检查不打 access log（减少噪声）
    skip_log = path.startswith("/static") or path == "/health"
    if not skip_log:
        logger.info(f"[{req_id}] {request.method} {path}")
    start = _time.time()
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        if not skip_log:
            duration_ms = int((_time.time() - start) * 1000)
            logger.info(f"[{req_id}] → {response.status_code} ({duration_ms}ms)")
        return response
    except Exception as e:
        duration_ms = int((_time.time() - start) * 1000)
        logger.error(f"[{req_id}] ✗ {request.method} {path} failed: {e} ({duration_ms}ms)")
        raise
    finally:
        request_id_var.reset(token)

# Templates
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def render_page_links(text: str, job_id: str) -> str:
    """Phase 3: convert '第N页' in finding description to clickable review links.

    Helps reviewers navigate cross-page findings (e.g. R1-b "第10页工序3 早于
    第9页工序2 结束" — both page numbers become clickable links).

    Security: description may contain attacker-controlled content (LLM output
    or rule-generated text). We escape HTML FIRST, then insert links on the
    escaped text. Without escaping, Markup() would render raw <script> tags.
    """
    if not text:
        return ""
    # Step 1: escape HTML entities to neutralize any tag in the source text
    from markupsafe import escape as _escape
    escaped = _escape(str(text))
    # Step 2: insert clickable links on the escaped text
    def repl(m):
        page = m.group(1)
        return (
            f'<a href="/jobs/{job_id}/review?page={page}" '
            f'class="page-link" target="_blank">第{page}页</a>'
        )
    return Markup(re.sub(r"第(\d+)页", repl, escaped))


templates.env.filters["render_page_links"] = render_page_links

app.include_router(review_router)  # 先注册 review（findings 路由优先）
app.include_router(jobs_router)
app.include_router(report_router)
app.include_router(settings_router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, page: int = 1):
    """Upload page with job list (excluding archived).

    分页：每页 20 条，超过时显示翻页控件。
    首次运行检测：若无 provider 配置了 API key，显示引导横幅。
    """
    page = max(1, page)
    page_size = 20

    db = await get_db()
    # 总数
    count_cursor = await db.execute(
        "SELECT COUNT(*) FROM jobs WHERE status != 'archived'"
    )
    total_jobs = (await count_cursor.fetchone())[0]
    total_pages = (total_jobs + page_size - 1) // page_size  # 向上取整

    offset = (page - 1) * page_size
    cursor = await db.execute(
        "SELECT * FROM jobs WHERE status != 'archived' "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    jobs = [dict(r) for r in await cursor.fetchall()]
    # 检查是否需要首次配置（无 provider 配置了 API key）
    providers = config["providers"]
    needs_setup = not any(p.api_key for p in providers.values())
    # robustness-C8: 日志路径暴露到页面底部，frozen/开发模式路径均可见
    from logging_config import _default_log_dir

    log_dir = _default_log_dir()
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "jobs": jobs,
            "needs_setup": needs_setup,
            "page": page,
            "total_pages": total_pages,
            "total_jobs": total_jobs,
            "log_dir": log_dir,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page — configure LLM / OCR credentials."""
    return templates.TemplateResponse(request, "settings.html", {})


@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.post("/api/shutdown")
async def shutdown_endpoint(request: Request):
    """Electron 退出前调用此端点，让后端优雅关闭。

    流程：
      1. 标记正在运行的 pipeline task 为取消（asyncio.CancelledError）
      2. 等待 2s 让 in-flight LLM/OCR 调用完成或超时
      3. close_db() 由 lifespan 的 yield 后部分处理

    Electron main.js 在 before-quit 事件中 fetch 此端点，然后才 kill 进程。
    """
    import asyncio
    from core.pipeline import _pipeline_tasks
    from core.security import is_local_request

    if not is_local_request(request):
        raise HTTPException(403, "Shutdown only allowed from local host")

    logger.info(f"[shutdown] Requested by Electron. Cancelling {len(_pipeline_tasks)} active pipeline task(s)...")
    # 取消所有活跃 pipeline task（CancelledError 会被 pipeline 的 except 捕获）
    for job_id, task in list(_pipeline_tasks.items()):
        if not task.done():
            logger.info(f"[shutdown] Cancelling pipeline task for job {job_id}")
            task.cancel()
    # 等待 2s 让 task 清理（写 error 状态 + audit_log）
    if _pipeline_tasks:
        await asyncio.sleep(2)
    logger.info("[shutdown] Graceful shutdown preparation complete")
    return {"status": "shutting_down", "cancelled_tasks": len(_pipeline_tasks)}


@app.get("/api/health/downstream")
async def health_downstream():
    """Probe configured OCR + LLM services for reachability.

    Used by Settings page 'Test connection' button and pre-flight checks.
    Does NOT submit real OCR/LLM work — just verifies auth + connectivity.
    """
    from core.health import probe_all
    import logging
    logger = logging.getLogger("main.health")
    logger.info("Downstream health probe requested")
    result = await probe_all()
    logger.info(f"Health probe result: ocr={result['ocr']['ok']} llm={result['llm']['ok']}")
    return result


@app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
async def review_page(job_id: str, request: Request, page: int = 1):
    """Review UI page: left PDF viewer + right OCR text + findings."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, "Job not found")

    total_pages = job["total_pages"] or 0

    # Get OCR text + structured_json for requested page
    cursor = await db.execute(
        "SELECT raw_html, structured_json FROM page_cache WHERE job_id = ? AND page = ?",
        (job_id, page),
    )
    row = await cursor.fetchone()
    raw_html = row["raw_html"] if row else ""
    structured_json = row["structured_json"] if row else None
    # Strip HTML tags for display
    ocr_text = re.sub(r"<[^>]+>", " ", raw_html) if raw_html else ""
    ocr_text = re.sub(r"\s+", " ", ocr_text).strip()

    # Phase 3: get findings for THIS page, ordered by severity then source.
    # Critical + rule findings surface at the top so reviewers see them first.
    severity_order = (
        "CASE severity WHEN 'critical' THEN 0 "
        "WHEN 'warning' THEN 1 "
        "WHEN 'info' THEN 2 ELSE 3 END"
    )
    source_order = (
        "CASE source WHEN 'rule' THEN 0 "
        "WHEN 'llm_fallback' THEN 1 "
        "WHEN 'llm_page' THEN 2 "
        "WHEN 'llm_cross' THEN 3 ELSE 4 END"
    )
    cursor = await db.execute(
        f"SELECT * FROM findings WHERE job_id = ? AND page = ? "
        f"ORDER BY {severity_order}, {source_order}, id",
        (job_id, page),
    )
    findings = [dict(r) for r in await cursor.fetchall()]

    # Phase 3: extract measurement matrix from structured_json so the template
    # can render the 9×8 cell grid with in_spec colors without an extra API call.
    measurements = []
    matrix_columns: list[str] = []
    if structured_json:
        import json as _json
        try:
            data = _json.loads(structured_json)
        except _json.JSONDecodeError:
            data = {}
        # Phase 5C: extract page-level confidence + parse_error flag for the
        # template so reviewers can see when LLM parsing failed or was unsure.
        page_parse_error = bool(data.get("_parse_error"))
        page_ocr_empty = bool(data.get("_ocr_empty"))
        page_ocr_sparse = bool(data.get("_ocr_sparse"))
        page_confidence = data.get("overall_confidence") or ""
        col_set: dict[str, None] = {}
        for step in data.get("steps", []) or []:
            for m in step.get("measurements", []) or []:
                values = m.get("values") or {}
                for col in values.keys():
                    col_set.setdefault(col, None)
                measurements.append({
                    "step_no": step.get("step_no"),
                    "time": m.get("time"),
                    "values": values,
                })
        matrix_columns = list(col_set.keys())
    else:
        page_parse_error = False
        page_ocr_empty = False
        page_ocr_sparse = False
        page_confidence = ""

    # Count findings by severity (all pages, for status bar)
    cursor = await db.execute(
        "SELECT severity FROM findings WHERE job_id = ?", (job_id,)
    )
    severity_counts = {"critical": 0, "warning": 0, "info": 0}
    for r in await cursor.fetchall():
        sev = r["severity"]
        if sev in severity_counts:
            severity_counts[sev] += 1

    # Per-page finding counts (for left sidebar page navigation markers)
    cursor = await db.execute(
        "SELECT page, severity, COUNT(*) as cnt FROM findings WHERE job_id = ? GROUP BY page, severity",
        (job_id,),
    )
    page_finding_counts: dict[int, dict] = {}
    for r in await cursor.fetchall():
        p = r["page"]
        page_finding_counts.setdefault(p, {"critical": 0, "warning": 0, "info": 0, "total": 0})
        sev = r["severity"]
        if sev in page_finding_counts[p]:
            page_finding_counts[p][sev] += r["cnt"]
        page_finding_counts[p]["total"] += r["cnt"]

    # Parse failed_pages
    failed_pages = []
    if job["failed_pages"]:
        try:
            failed_pages = __import__("json").loads(job["failed_pages"])
        except Exception:
            pass

    return templates.TemplateResponse(request, "review.html", {
        "job_id": job_id,
        "filename": job["filename"],
        "status": job["status"],
        "error_message": job["error_message"] if "error_message" in job.keys() else None,
        "page": page,
        "total_pages": total_pages,
        "ocr_text": ocr_text[:5000],
        "findings": findings,
        "severity_counts": severity_counts,
        "page_finding_counts": page_finding_counts,
        "failed_pages": failed_pages,
        "pdf_url": f"/api/jobs/{job_id}/pdf",
        "stage1_ms": job["stage1_ms"],
        "stage2_ms": job["stage2_ms"],
        "stage3_ms": job["stage3_ms"],
        # Phase 3 additions
        "measurements": measurements,
        "matrix_columns": matrix_columns,
        # Phase 5C: transparency flags for reviewer trust
        "page_parse_error": page_parse_error,
        "page_ocr_empty": page_ocr_empty,
        "page_ocr_sparse": page_ocr_sparse,
        "page_confidence": page_confidence,
    })


@app.get("/api/jobs/{job_id}/pdf")
async def serve_pdf(job_id: str):
    """Serve the original PDF for in-browser preview.

    Security: validate pdf_path is within output_dir to prevent path
    traversal. If DB is tampered (SQL injection or direct edit), the
    pdf_path could point to arbitrary system files like C:\\Windows\\...
    """
    db = await get_db()
    cursor = await db.execute("SELECT pdf_path FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row or not row["pdf_path"]:
        raise HTTPException(404, "PDF not found")
    pdf_path = Path(row["pdf_path"]).resolve()
    # 路径遍历防护：pdf_path 必须在 output_dir 内
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
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=pdf_path.name,
        content_disposition_type="inline",
    )


@app.get("/jobs", response_class=HTMLResponse)
async def job_list(request: Request):
    """List all jobs — redirects to index."""
    return await index(request)
