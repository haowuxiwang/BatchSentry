"""Report API — generate and download Markdown + JSON reports."""
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from db.client import get_db
from models.schemas import FindingStatus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["report"])


@router.get("/jobs/{job_id}/report.md", response_class=PlainTextResponse)
async def download_report_md(job_id: str):
    """Generate and return Markdown report."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        logger.warning(f"Report.md requested for missing job: {job_id}")
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

    md = _generate_markdown(job, findings, total_pages)
    logger.info(f"[{job_id}] Report.md generated: {len(findings)} findings, {len(md)} chars")
    return PlainTextResponse(md, media_type="text/markdown")


@router.get("/jobs/{job_id}/report.json")
async def download_report_json(job_id: str):
    """Return structured JSON report with job metadata + findings.

    Phase 3 fix: include job field (filename/status/total_pages/stages) so
    downstream consumers (e.g. E2E test, external integrations) can identify
    the job without a separate API call.
    """
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

    lines = [
        f"# GMP 批生产记录合规检查报告",
        f"",
        f"- **文件名**: {job['filename']}",
        f"- **总页数**: {total_pages}",
        f"- **生成时间**: {now}",
        f"- **Job ID**: {job['id']}",
        f"- **总 Findings**: {len(findings)}",
        f"",
        f"---",
        f"",
        f"## Findings 列表",
        f"",
    ]

    # Group by severity
    for sev in ["critical", "warning", "info"]:
        sev_findings = [f for f in findings if f["severity"] == sev]
        if not sev_findings:
            continue
        icon = SeverityIcon.get(sev, "")
        lines.append(f"### {icon} {sev.upper()} ({len(sev_findings)})")
        lines.append("")
        for f in sev_findings:
            st_icon = StatusIcon.get(f["status"], "")
            lines.append(f"- **第{f['page']}页** | `{f['type']}` {st_icon} {f['status']}")
            lines.append(f"  - {f['description']}")
            if f.get("ocr_text"):
                lines.append(f"  - OCR原文: `{f['ocr_text'][:100]}`")
            if f.get("corrected_text"):
                lines.append(f"  - 修正为: `{f['corrected_text'][:100]}`")
            if f.get("reviewer_note"):
                lines.append(f"  - 审查员备注: {f['reviewer_note']}")
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
