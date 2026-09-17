"""BatchSentry — FastAPI entry point."""
import logging
import sys
from contextlib import asynccontextmanager

import json
import re
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, FileResponse
from markupsafe import Markup

from config import config, UPLOAD_LIMITS
from db.client import get_db, close_db
from logging_config import setup_logging, generate_request_id, request_id_var
from core.pipeline import recover_stuck_jobs
from core.finding_quality import attach_review_tier, tier_counts
from core.rules.registry import rule_coverage

setup_logging()
logger = logging.getLogger(__name__)

# Application version — single source of truth.
# Avoids duplicate hardcoded "1.1.0" in FastAPI(app=...) and /health endpoint.
# 与 package.json 的 version 必须一致（tests/unit/test_version_consistency.py 机检）。
APP_VERSION = "1.1.8"


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
from core.zh_map import zh_ocr_backend


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
    # 启动时恢复卡死的 job（应用上次崩溃/强杀时留下的非终态 job）。
    # 异步执行：不阻塞 lifespan yield，避免 Electron 首屏延迟。
    # 竞态防护（B7）：process_started_at = 本进程启动时刻 — 仅恢复 created_at
    # 早于该时刻的 job；lifespan yield 后马上可接收新上传（pending job 活着，
    # pipeline 即将运行），若按"全部非终态"恢复会把这些新任务误标为 error。
    # B7 竞态防护 cutoff + P0-5 时区统一：created_at 现存本地时间
    # （datetime('now','localtime')），cutoff 必须同口径（此前 utcnow 会
    # 让本地时间的新任务全部晚于 cutoff——方向恰好安全，但统一后消除歧义）。
    from datetime import datetime

    process_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def _recover_bg():
        try:
            recovered = await recover_stuck_jobs(process_started_at=process_started_at)
            if recovered:
                logger.warning(f"  recovered {recovered} stuck jobs (marked as error)")
        except Exception as e:
            logger.error(f"  stuck job recovery failed: {e}", exc_info=True)

    import asyncio as _asyncio

    # 后台任务统一登记，关停时**先取消并 join 全部**再关库。
    # 踩过的坑：这些任务原先都是 fire-and-forget（`create_task` 不留引用），
    # 于是存在"close_db() 之后任务才被调度 → get_db() 重新打开连接 → 该连接
    # 再没人关"的竞态。测试里的表现是 tmp_path 上的 test.db 删不掉
    # （WinError 32 另一个程序正在使用此文件），生产里的表现是关停后残留
    # 一个 DB 句柄。日志线索是关停后的 "kb seed skipped: Cannot operate on
    # a closed database." —— 那句话本身就是"任务跑晚了一步"的证据。
    bg_tasks: list = []

    def _spawn(coro) -> "_asyncio.Task":
        task = _asyncio.create_task(coro)
        bg_tasks.append(task)
        return task

    _spawn(_recover_bg())

    # 运行时看门狗（v12）：补齐"运行期间"的兜底。上面那个 recover 只在启动时
    # 跑一次 —— 不重启应用，卡死的 job 就永远停在非终态、SSE 无限等待
    # （见 docs/RUNTIME_WATCHDOG.md）。看门狗按 jobs.last_activity_at 判定停滞，
    # 收敛动作与启动恢复一致（error + 审计 + 通知）。
    async def _watchdog_bg():
        try:
            from core.watchdog import enabled, watchdog_loop
            if not enabled():
                logger.info("  watchdog disabled via PBC_WATCHDOG_ENABLED")
                return
            await watchdog_loop()
        except Exception as e:
            logger.error(f"  watchdog crashed: {e}", exc_info=True)

    _spawn(_watchdog_bg())

    # v8: 知识库条目镜像装载（幂等；JSON 缺失/异常不致命——检索走内存）
    async def _kb_seed_bg():
        try:
            from core.kb import ensure_db_seeded

            db = await get_db()
            n = await ensure_db_seeded(db)
            if n:
                logger.info(f"  knowledge base seeded: {n} entries")
        except Exception as e:
            logger.warning(f"  kb seed skipped: {e}")

    _spawn(_kb_seed_bg())
    yield
    # 关停顺序：先把后台任务全部取消并 join（等它们真正结束），再关库。
    # 反过来做就会让"晚一步被调度"的任务重新 open 一个没人关的连接。
    for t in bg_tasks:
        t.cancel()
    for t in bg_tasks:
        try:
            await t
        except _asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"  background task shutdown error: {e}")
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
# 对抗审查（cr-17）：守卫 is_local_request 放行 localhost（任意端口），
# 但 CORS 只认 127.0.0.1 — 浏览器用 http://localhost:8000 打开设置页时
# 所有 fetch 读不到响应（无 ACAO 头）、POST 全被 preflight 拦截，设置页
# 在 localhost 下完全不可用。补 localhost 同端口白名单，与守卫口径一致
# （恶意页面 Origin 不会命中白名单，安全性不变）。
# B8（P2-x 端口常量集中）：白名单端口不再硬编码 8000/58765 双份 — 由
# config["app"].port 动态生成（该值感知 PORT/APP_PORT env；Electron 传
# PORT=58765，dev 默认 8000）。Electron 修改 SERVER_PORT 或 dev 换端口时
# CORS 与守卫口径自动一致，不会把合法页面拦在门外。
def _cors_origins() -> list[str]:
    port = config["app"].port
    return [f"http://{h}:{port}" for h in ("127.0.0.1", "localhost")]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
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
    # setdefault: endpoint-declared Cache-Control (e.g. page_image
    # private,max-age) survives; no-cache + ETag revalidation still applies.
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    else:
        response.headers.setdefault("Cache-Control", "no-cache")
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
    # UX P1-1: 去掉 target="_blank" — Electron 中 target=_blank 会经
    # setWindowOpenHandler 把 http(s) 交给系统浏览器打开（跳出应用，
    # 用户回不来原复核上下文）；应用内同页跳转保持复核流不断。
    def repl(m):
        page = m.group(1)
        return (
            f'<a href="/jobs/{job_id}/review?page={page}" '
            f'class="page-link">第{page}页</a>'
        )
    return Markup(re.sub(r"第(\d+)页", repl, escaped))


templates.env.filters["render_page_links"] = render_page_links

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def asset_ver(path: str) -> str:
    """Cache-busting version for static assets (frontend build hygiene).

    Returns the file mtime as version string — editing a CSS/JS file
    automatically invalidates browser cache, replacing the manual
    `?v=N` bumping (settings.js?v=12 style) that silently served stale
    assets when a bump was forgotten. Frozen (PyInstaller) mode: files
    are extracted with their mtimes preserved, so versions stay stable
    across restarts until the bundle is rebuilt.
    """
    try:
        return str(int((_STATIC_DIR / path).stat().st_mtime))
    except OSError:
        return "0"


templates.env.globals["asset_ver"] = asset_ver

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
            # 上传限额下发前端（单一真值 = config.UPLOAD_LIMITS）——
            # 前端的预检与提示文案不再各自写死，避免"前端放行、后端拒绝"漂移
            "limits": UPLOAD_LIMITS,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page — configure LLM / OCR credentials."""
    return templates.TemplateResponse(request, "settings.html", {})


@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/health/watchdog")
async def health_watchdog(request: Request):
    """运行时看门狗的自述：存活 + 判定口径 + 最近一轮结论。

    为什么必须单独暴露（`docs/RUNTIME_WATCHDOG.md` §8）：看门狗**自己挂掉**
    比 job 卡死更糟 —— 用户会以为"有兜底"而不再手动重试。`last_scan_at`
    停滞、或 `last_scan_error` 持续非空，就是巡检失效的证据。

    与 `/health` 分开而不合并：`/health` 的返回体是探针（Electron 启动、
    e2e harness）依赖的稳定契约，不为其增删字段。守卫口径与
    `/api/health/downstream` 对齐（同属 `/api/health/*` 家族）。
    """
    from core.security import is_local_request
    if not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    from core.watchdog import status_snapshot
    return status_snapshot()


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
async def health_downstream(request: Request):
    """Probe configured OCR + LLM services for reachability.

    Used by Settings page 'Test connection' button and pre-flight checks.
    Does NOT submit real OCR/LLM work — just verifies auth + connectivity.
    """
    # 对抗审查：该端点是简单 GET（无 preflight），此前无任何守卫，任意
    # 网页可跨站循环触发，每次真实消耗本地 LLM/OCR API 配额（1-token
    # ping + OPTIONS 探测）— 与 /api/settings/* 的守卫对齐。
    from core.security import is_local_request
    if not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
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
        raise HTTPException(404, "任务不存在")

    total_pages = job["total_pages"] or 0

    # Get OCR text + structured_json for requested page
    cursor = await db.execute(
        "SELECT raw_html, ocr_diagnostics, structured_json, regions_json "
        "FROM page_cache WHERE job_id = ? AND page = ?",
        (job_id, page),
    )
    row = await cursor.fetchone()
    raw_html = row["raw_html"] if row else ""
    ocr_diagnostics = None
    if row and row["ocr_diagnostics"]:
        try:
            ocr_diagnostics = json.loads(row["ocr_diagnostics"])
        except (TypeError, json.JSONDecodeError):
            pass
    structured_json = row["structured_json"] if row else None
    # P0-3：本页区域级证据锚（与 AJAX 端点同源同函数 —— SSR 首屏与翻页后
    # 刷新必须是同一套锚，否则会出现"翻页前能定位、翻页后不能"的假缺陷）
    regions_payload = None
    if row and row["regions_json"]:
        try:
            _rp = json.loads(row["regions_json"])
            if isinstance(_rp, dict) and _rp.get("regions"):
                regions_payload = _rp
        except (TypeError, json.JSONDecodeError):
            pass
    # Strip HTML tags for display — keep line breaks so tables stay readable.
    # Full raw_html goes to the template separately as ocr_raw_html and is
    # converted client-side by review.js htmlToText (same path as AJAX paging,
    # no 5000-char truncation — F1 fix).
    ocr_text = re.sub(r"<[^>]+>", " ", raw_html) if raw_html else ""
    ocr_text = re.sub(r"[ \t]+", " ", ocr_text)
    ocr_text = re.sub(r"\n{3,}", "\n\n", ocr_text).strip()

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
    # v8: 解码 kb_refs JSON → 列表供模板折叠渲染（坏数据安全退化）
    for f in findings:
        raw = f.get("kb_refs")
        refs = []
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    refs = [x for x in parsed if isinstance(x, dict)]
            except ValueError:
                pass
        f["kb_refs_list"] = refs
        f.pop("kb_refs", None)
        # P0-3：区域级证据锚（与 api.review.list_findings 共用同一纯函数）
        from core.pipeline.regions import region_anchor
        f["region_ref"] = region_anchor(
            f"{f.get('description') or ''} {f.get('ocr_text') or ''}",
            regions_payload,
        )

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
        # Todo 11: 截断透出 — HTML 超上限被截 → 复核横幅提示以 PDF 原图为准
        page_ocr_truncated = bool(data.get("_ocr_truncated"))
        # C3 修复（Round 3）：OCR 不完整警告（MinerU 低置信度丢弃块）透出
        # 到 review 横幅 — LLM 已被系统警告降级置信度，复核者需知道缺失原因
        page_ocr_warning = str(data.get("_ocr_warning") or "")
        # 幻觉防护：LLM 提取数值未在 OCR 原文找到 — 横幅提醒复核重点核对
        page_grounding_warn = data.get("_grounding_warn") or []
        # 对抗审查 P1：LLM 输出截断已被 _repair_truncated_json 静默恢复 /
        # schema 校验重试后仍不合规 — 此前这两个标记无任何消费终端，
        # 复核者看不到"数据可能缺失"，必须与 OCR 横幅同等透出
        page_llm_truncated = bool(data.get("_truncated_warn"))
        page_schema_warn = data.get("_schema_warn") or []
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
        page_ocr_truncated = False
        page_ocr_warning = ""
        page_grounding_warn = []
        page_llm_truncated = False
        page_schema_warn = []
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

    # Parse failed_pages —— 与 JSON 接口共用同一解析器（api/jobs/status.py），
    # 避免同一列在 SSR 与 API 两处各有一套降级规则（仓库约定：可见性字段
    # 必须与真值同源）。call-time 导入：main.py 顶层已 `from api.jobs import
    # router`，此处若顶层导入 status 会与之形成循环。
    from api.jobs.status import _parse_failed_pages
    # 模板契约是"可迭代列表"（`{% if ... and failed_pages %}` + join），
    # 故把"未知"（None）落成空列表再交给模板。API 侧保留 None/[] 之分。
    failed_pages = _parse_failed_pages(job["failed_pages"]) or []

    # 三色复核分级（M6/T6.4）：本页 finding 按来源分层（规则=红 / LLM=蓝），
    # 并给出全 job 的规则覆盖面（系统校验通过=绿）。tier 由 core 单一来源
    # 计算，SSR 与 AJAX 共用同一函数（前端不各持映射表）。
    attach_review_tier(findings)
    tier_count = tier_counts(findings)
    cursor = await db.execute(
        "SELECT type, COUNT(*) AS cnt FROM findings WHERE job_id = ? GROUP BY type",
        (job_id,),
    )
    type_count_map = {r["type"]: r["cnt"] for r in await cursor.fetchall()}
    coverage = rule_coverage(type_count_map)

    # P0-3：首屏锚点映射（finding id → region_ref）随 ctx 注入模板。
    # 首屏 findings 由 SSR 渲染，不经过 AJAX 渲染函数，锚点必须显式传递，
    # 否则首屏"定位原图"按钮点了没反应（翻页后才生效 —— 假缺陷）。
    region_refs = {
        f["id"]: f["region_ref"] for f in findings if f.get("region_ref")
    }

    return templates.TemplateResponse(request, "review.html", {
        "job_id": job_id,
        "filename": job["filename"],
        "status": job["status"],
        "error_message": job["error_message"] if "error_message" in job.keys() else None,
        "page": page,
        "total_pages": total_pages,
        "ocr_text": ocr_text,
        "ocr_raw_html": raw_html,
        "ocr_diagnostics": ocr_diagnostics,
        "findings": findings,
        "region_refs": region_refs,
        "severity_counts": severity_counts,
        "page_finding_counts": page_finding_counts,
        "tier_counts": tier_count,
        "rule_coverage": coverage,
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
        "page_ocr_truncated": page_ocr_truncated,
        "page_ocr_warning": page_ocr_warning,
        "page_grounding_warn": page_grounding_warn,
        "page_llm_truncated": page_llm_truncated,
        "page_schema_warn": page_schema_warn,
        "page_confidence": page_confidence,
        # cr-19: 实际 OCR 后端（failover 后与配置不同 — GMP 复核可见性）
        "ocr_backend_used": job["ocr_backend_used"] if "ocr_backend_used" in job.keys() else None,
        "ocr_backend_display": (
            zh_ocr_backend(job["ocr_backend_used"])
            if ("ocr_backend_used" in job.keys() and job["ocr_backend_used"]) else None
        ),
    })


@app.get("/api/jobs/{job_id}/pdf")
async def serve_pdf(job_id: str, request: Request):
    """Serve the original PDF for in-browser preview.

    Security: validate pdf_path is within output_dir to prevent path
    traversal. If DB is tampered (SQL injection or direct edit), the
    pdf_path could point to arbitrary system files like C:\\Windows\\...
    """
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute("SELECT pdf_path FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row or not row["pdf_path"]:
        raise HTTPException(404, "PDF 不存在")
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
        raise HTTPException(404, "PDF 文件缺失")
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
