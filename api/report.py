"""Report API — generate and download Markdown + JSON reports.

缓存策略：报告内容缓存在内存中（FIFO，maxsize=32），key 为 (job_id, findings_count,
last_finding_id)。findings 数量或最后一条 finding id 变化时自动失效。
job 删除时缓存项自然淘汰。

并发安全：_report_cache 用 asyncio.Lock 保护，防止 SSE 请求 + 报告请求并发读写导致
字典迭代器失效或 key 覆盖。
"""
import asyncio
import html
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from db.client import get_db
from core.zh_map import zh_finding_status, zh_severity

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["report"])


# 缓存：key=(job_id, findings_count, last_finding_id) → markdown 文本
_report_cache: dict[tuple, str] = {}
_report_cache_lock = asyncio.Lock()
_REPORT_CACHE_MAX = 32


async def _audit_report_export(job_id: str, fmt: str, size: int) -> None:
    """报告导出写 audit_log（对抗审查：导出是质量体系事件，此前无留痕）。

    失败不阻断导出（审计是附属动作）。
    """
    try:
        db = await get_db()
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail) VALUES (?, ?, ?)",
            (job_id, "report_export", f"format={fmt} size={size}"),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to write report_export audit log: {e}")


async def _generate_report_md_cached(job_id: str) -> str:
    """生成 Markdown 报告（带缓存，并发安全）。

    缓存 key 为 (job_id, findings_count, last_finding_id)。
    复核操作改变 findings 时，下次请求会因 key 变化而重新生成。
    """
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, "Job not found")

    cursor = await db.execute(
        "SELECT * FROM findings WHERE job_id = ? ORDER BY page, "
        "CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 WHEN 'info' THEN 2 END, id",
        (job_id,),
    )
    findings = [dict(r) for r in await cursor.fetchall()]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM page_cache WHERE job_id = ?", (job_id,)
    )
    total_pages = (await cursor.fetchone())[0]

    # 缓存 key：findings 数量 + 最后一条 finding 的 id
    last_id = findings[-1]["id"] if findings else 0
    # Include status hash in cache key so review operations (confirm/reject/correct)
    # invalidate the cache — without this, reports show stale finding statuses.
    # 对抗审查 P1-A：必须把 corrected_text/reviewer_note/reviewed_at 也纳入 —
    # 复核接口允许不改 status 单独更新这两个字段（同状态二次修正），原 key
    # 只看 (id, status) → 修正内容变化后报告仍返回旧缓存文本（GMP 场景下
    # 报告静默携带过期内容，用户以为导出的是最新版本）。
    status_hash = hash(tuple(sorted(
        (f["id"], f["status"], f.get("corrected_text") or "", f.get("reviewer_note") or "")
        for f in findings
    )))
    cache_key = (job_id, len(findings), last_id, status_hash)

    # 并发安全：用锁保护字典读写
    async with _report_cache_lock:
        if cache_key in _report_cache:
            logger.info(f"[{job_id}] Report.md cache hit (findings={len(findings)})")
            return _report_cache[cache_key]

    # 生成报告（在锁外执行，避免长时间持锁）
    md = _generate_markdown(job, findings, total_pages)

    # 写入缓存，清理超出的项
    async with _report_cache_lock:
        _report_cache[cache_key] = md
        if len(_report_cache) > _REPORT_CACHE_MAX:
            # 简单 FIFO 淘汰：删除最早插入的 key
            oldest = next(iter(_report_cache))
            del _report_cache[oldest]

    logger.info(
        f"[{job_id}] Report.md generated and cached: {len(findings)} findings, {len(md)} chars"
    )
    return md


@router.get("/jobs/{job_id}/report.md", response_class=PlainTextResponse)
async def download_report_md(job_id: str, request: Request = None):
    """Generate and return Markdown report (cached)."""
    # P2-1: GET 读端点守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    md = await _generate_report_md_cached(job_id)
    await _audit_report_export(job_id, "md", len(md))
    return PlainTextResponse(md, media_type="text/markdown")


@router.get("/jobs/{job_id}/report.json")
async def download_report_json(job_id: str, request: Request = None):
    """Return structured JSON report with job metadata + findings.

    Phase 3 fix: include job field (filename/status/total_pages/stages) so
    downstream consumers (e.g. E2E test, external integrations) can identify
    the job without a separate API call.
    """
    # P2-1: GET 读端点守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, "Job not found")

    cursor = await db.execute(
        "SELECT * FROM findings WHERE job_id = ? ORDER BY page, "
        "CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 WHEN 'info' THEN 2 END, "
        "CASE COALESCE(source, 'rule') WHEN 'rule' THEN 0 "
        "WHEN 'llm_fallback' THEN 1 WHEN 'llm_page' THEN 2 "
        "WHEN 'llm_cross' THEN 3 ELSE 4 END, id",
        (job_id,),
    )
    findings = [dict(r) for r in await cursor.fetchall()]
    logger.info(f"[{job_id}] Report.json generated: {len(findings)} findings")
    await _audit_report_export(job_id, "json", len(findings))
    return {
        "job": {
            "id": job["id"],
            "filename": job["filename"],
            "status": job["status"],
            "total_pages": job["total_pages"],
            "stage1_ms": job["stage1_ms"],
            "stage2_ms": job["stage2_ms"],
            "stage3_ms": job["stage3_ms"],
            "created_at": job["created_at"],
            "finished_at": job["finished_at"],
        },
        "findings": findings,
        "count": len(findings),
    }


def _generate_markdown(job: dict, findings: list[dict], total_pages: int) -> str:
    """Build Markdown report from findings."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    SeverityIcon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
    StatusIcon = {
        "pending": "⏳",
        "confirmed": "✅",
        "rejected": "❌",
        "corrected": "✏️",
    }

    # 对抗审查(cr-7): 报告里的 LLM/OCR 内容是模型生成的可疑文本，可能含
    # HTML/脚本。Markdown 本身浏览器不渲染，但用户常用 Typora/Obsidian 等
    # 自动渲染 HTML 的编辑器打开 → 形成 XSS 执行面。统一 HTML-escape。
    # 对抗审查 P1-B：html.escape 只转义 <>& — LLM 文本里的 `![x](url)` /
    # `[x](...)` 在 Typora/Obsidian 中成为真实图片/链接（远端图片会触发
    # 请求 = 隐私泄露/追踪；路径外链可指向 file://）。对 LLM/OCR 无信任
    # 文本额外转义 Markdown 元字符，使其成为纯文本。
    def esc(text) -> str:
        s = html.escape(str(text), quote=False)
        # 转义 Markdown 图像/链接/强调元字符 — 仅限无信任来源字段
        for ch in ("!", "[", "]", "(", ")"):
            s = s.replace(ch, "&#{};".format(ord(ch)))
        return s

    lines = [
        "# GMP 批生产记录合规检查报告",
        "",
        f"- **文件名**: {esc(job['filename'])}",
        f"- **总页数**: {total_pages}",
        f"- **生成时间**: {now}",
        f"- **Job ID**: {job['id']}",
        f"- **总 Findings**: {len(findings)}",
        "",
        "---",
        "",
        "## Findings 列表",
        "",
    ]

    # Group by severity
    for sev in ["critical", "warning", "info"]:
        sev_findings = [f for f in findings if f["severity"] == sev]
        if not sev_findings:
            continue
        icon = SeverityIcon.get(sev, "")
        lines.append(f"### {icon} {zh_severity(sev)} ({len(sev_findings)})")
        lines.append("")
        for f in sev_findings:
            st_icon = StatusIcon.get(f["status"], "")
            lines.append(f"- **第{f['page']}页** | `{esc(f['type'])}` {st_icon} {zh_finding_status(f['status'])}")
            lines.append(f"  - {esc(f['description'])}")
            if f.get("ocr_text"):
                # P2: 先截原始文本再转义 — 反过来的话实体（如 &#33;）会被
                # 拦腰截断，渲染成字面 "&#33"，且实际展示字符数不足
                lines.append(f"  - OCR原文: `{esc(f['ocr_text'][:100])}`")
            if f.get("corrected_text"):
                lines.append(f"  - 修正为: `{esc(f['corrected_text'][:100])}`")
            if f.get("reviewer_note"):
                lines.append(f"  - 审查员备注: {esc(f['reviewer_note'])}")
            lines.append("")

    # Summary
    pending = len([f for f in findings if f["status"] == "pending"])
    lines.append("---")
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    if pending:
        lines.append(f"⚠️ **{pending} 条 Finding 需人工复核**，请在复核界面确认/拒绝/修正后重新导出。")
    else:
        lines.append("✅ 所有 Findings 已处理完毕。")
    lines.append("")

    return "\n".join(lines)
