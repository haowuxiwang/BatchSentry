"""PDF page preview — cached fitz document + PNG rendering."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

import fitz  # PyMuPDF — 页码 PNG 渲染

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from config import config
from db.client import get_db
from api.jobs import router

logger = logging.getLogger(__name__)

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
    """Return a cached fitz.Document for the job, opening + caching if needed.

    TTL/容量淘汰时 close 句柄；Windows 上未关闭的 fitz 句柄会锁住 PDF
    文件（delete_job 的 rmtree 会因此失败），删除前须 _invalidate_pdf_doc。
    """
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

def _invalidate_pdf_doc(job_id: str):
    """Close + drop the cached fitz handle for a job (releases the file lock).

    Windows 上未 close 的 fitz Document 保持对 PDF 文件的独占锁，
    使 delete_job 的 shutil.rmtree 抛 WinError 32（文件被占用），
    任务删除后 output 目录残留。删除前必须先失效缓存。
    """
    entry = _pdf_doc_cache.pop(job_id, None)
    if entry:
        try:
            entry[0].close()
        except Exception:
            pass

@router.get("/{job_id}/page/{page_num}")
async def get_job_page_image(job_id: str, page_num: int, request: Request = None):
    """Render a PDF page to JPEG for inline preview.

    替代浏览器原生 PDF viewer（iframe）：新版 Chromium/Electron 的 PDF
    viewer 忽略 toolbar=0 参数，自带打印/下载/更多操作按钮且缩放不可控。
    PyMuPDF 渲染后以 <img> 展示 — 无浏览器工具栏、缩放 fit-width
    由 CSS 控制、页码与渲染页严格对应。

    输出格式 JPEG（质量 82）：扫描件是照片类内容（白底 + 密集文字），
    PNG 无损压缩单页仍 3-6MB（2000px @ 300dpi），弱机上 <img> 解码 +
    本地传输要数秒，用户感知"正在渲染第 N 页"卡住。JPEG 体积降低
    5-10 倍（单页 ~300-800KB），视觉上白底文档无可见差异。

    Security: pdf_path 校验同 serve_pdf（必须在 output_dir 内，防路径穿越）。
    page_num 1-based；越界返回 404。文档句柄缓存 _pdf_doc_cache 避免
    大 PDF 反复打开。
    """
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
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
    # Runtime resolution — tests monkeypatch api.jobs._get_pdf_doc.
    from api.jobs import _get_pdf_doc
    try:
        doc = _get_pdf_doc(job_id, str(pdf_path))
    except Exception as e:
        logger.error(f"[{job_id}] Failed to open PDF for rendering: {e}")
        raise HTTPException(500, "PDF cannot be rendered")
    if page_num < 1 or page_num > doc.page_count:
        raise HTTPException(404, f"Page out of range (1-{doc.page_count})")
    # 渲染在线程池执行：大扫描页 get_pixmap 需秒级，同步执行会阻塞整个
    # 事件循环（所有 API/SSE 请求排队，前端"正在渲染"卡住）。尺寸由
    # _pdf_render_zoom 限制（≤2000px 宽），输出 JPEG 体积再降 5-10x。
    def _render_sync() -> bytes:
        page = doc.load_page(page_num - 1)
        zoom = _pdf_render_zoom(page)
        pix = page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom), alpha=False, colorspace=fitz.csRGB
        )
        return pix.tobytes("jpeg", jpg_quality=82)

    try:
        jpeg = await asyncio.to_thread(_render_sync)
    except Exception as e:
        logger.error(f"[{job_id}] Page render failed (p{page_num}): {e}")
        raise HTTPException(500, "Page render failed")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        # 注：Cache-Control 由 main.py 全局中间件统一设置（非 /static/ 一律 no-cache）
    )

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
