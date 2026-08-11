"""Review API — list/update findings with audit logging."""
import json
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Form

from db.client import get_db
from core.pipeline import db_lock

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["review"])


@router.get("/jobs/{job_id}/findings")
async def list_findings(
    job_id: str,
    status: Optional[str] = None,
    page: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List findings for a job, optionally filtered by status and/or page.

    统一端点：支持 status 和 page 过滤（AJAX 翻页用 page 参数）。
    分页：limit（默认 50，max 200）+ offset，防止 100+ findings 一次返回卡顿。
    """
    # 钳制 limit 防止滥用
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

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
    # 按页过滤时，仅按 severity+source 排序（不需要 page）
    if page:
        order_clause = f"ORDER BY {severity_order}, {source_order}, id"
        cursor = await db.execute(
            f"SELECT * FROM findings WHERE job_id = ? AND page = ? {order_clause} "
            f"LIMIT ? OFFSET ?",
            (job_id, page, limit, offset),
        )
    elif status:
        order_clause = f"ORDER BY page, {severity_order}, {source_order}, id"
        cursor = await db.execute(
            f"SELECT * FROM findings WHERE job_id = ? AND status = ? {order_clause} "
            f"LIMIT ? OFFSET ?",
            (job_id, status, limit, offset),
        )
    else:
        order_clause = f"ORDER BY page, {severity_order}, {source_order}, id"
        cursor = await db.execute(
            f"SELECT * FROM findings WHERE job_id = ? {order_clause} LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        )
    rows = await cursor.fetchall()
    findings = [dict(r) for r in rows]

    # 总数（用于前端显示 "x/y"）
    count_cursor = await db.execute(
        "SELECT COUNT(*) FROM findings WHERE job_id = ?", (job_id,)
    )
    total = (await count_cursor.fetchone())[0]

    return {
        "findings": findings,
        "count": len(findings),
        "total": total,
        "page": page,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total,
    }


@router.get("/jobs/{job_id}/findings/{finding_id}")
async def get_finding(job_id: str, finding_id: int):
    """Get a single finding detail."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM findings WHERE id = ? AND job_id = ?", (finding_id, job_id)
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Finding not found")
    return dict(row)


@router.post("/jobs/{job_id}/findings/{finding_id}")
async def update_finding(
    job_id: str,
    finding_id: int,
    status: Optional[str] = Form(default=None),
    reviewer_note: Optional[str] = Form(default=None),
    corrected_text: Optional[str] = Form(default=None),
):
    """Update a finding (confirm/reject/correct).

    Phase 3 fix: parameters use Form() because the review.html frontend posts
    application/x-www-form-urlencoded. Without Form(), FastAPI treats them as
    query params and returns 400 'No fields to update'.
    """
    db = await get_db()

    # Validate status value
    valid_statuses = {"confirmed", "rejected", "corrected", "pending"}
    if status and status not in valid_statuses:
        logger.warning(f"[{job_id}] Invalid status update: finding={finding_id} status={status}")
        raise HTTPException(400, f"Invalid status: {status}. Must be one of {valid_statuses}")

    # Check finding exists
    cursor = await db.execute(
        "SELECT id, status FROM findings WHERE id = ? AND job_id = ?", (finding_id, job_id)
    )
    row = await cursor.fetchone()
    if not row:
        logger.warning(f"[{job_id}] Finding not found: {finding_id}")
        raise HTTPException(404, "Finding not found")

    old_status = row["status"]
    logger.info(
        f"[{job_id}] Finding update: id={finding_id} "
        f"{old_status}→{status or '(unchanged)'}"
        + (f" note={reviewer_note[:30]!r}" if reviewer_note else "")
        + (f" corrected={corrected_text[:30]!r}" if corrected_text else "")
    )

    sets = []
    params = []
    if status:
        sets.append("status = ?")
        params.append(status)
    if reviewer_note is not None:
        sets.append("reviewer_note = ?")
        params.append(reviewer_note)
    if corrected_text is not None:
        sets.append("corrected_text = ?")
        params.append(corrected_text)
    if status in ("confirmed", "rejected", "corrected"):
        sets.append("reviewed_at = CURRENT_TIMESTAMP")
    elif status == "pending":
        sets.append("reviewed_at = NULL")

    if not sets:
        raise HTTPException(400, "No fields to update")

    params.extend([finding_id, job_id])

    # P-ADV3 修复：UPDATE findings + INSERT audit_log + commit 必须在 db_lock
    # 内原子执行，与 pipeline 的并发写入共享同一锁，防止 aiosqlite 单连接
    # 上的事务边界被穿插。
    action_parts = []
    if status:
        action_parts.append(f"status: {old_status} → {status}")
    if reviewer_note is not None:
        action_parts.append(f"note: '{reviewer_note[:50]}'")
    if corrected_text is not None:
        action_parts.append(f"corrected: '{corrected_text[:50]}'")

    async with db_lock:
        await db.execute(
            f"UPDATE findings SET {', '.join(sets)} WHERE id = ? AND job_id = ?",
            params,
        )
        await db.execute(
            "INSERT INTO audit_log (job_id, finding_id, action, detail) VALUES (?, ?, ?, ?)",
            (job_id, finding_id, "finding_update", "; ".join(action_parts)),
        )
        await db.commit()
    logger.info(f"[{job_id}] Finding {finding_id} updated: {old_status} → {status or old_status}")
    return {"ok": True}


@router.get("/jobs/{job_id}/audit")
async def get_audit_log(job_id: str, limit: int = 50):
    """Get audit log entries for a job."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM audit_log WHERE job_id = ? ORDER BY id DESC LIMIT ?",
        (job_id, limit),
    )
    rows = await cursor.fetchall()
    return {"entries": [dict(r) for r in rows], "count": len(rows)}


@router.get("/jobs/{job_id}/llm_audit")
async def get_llm_audit_log(job_id: str, limit: int = 100):
    """Get LLM call audit log for a job (Phase 7 GMP traceability).

    Returns every LLM call made during this job's pipeline run, including
    provider / model / prompt_version / token usage / latency / success.
    Used to answer "which model version produced this finding?".
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM llm_call_audit WHERE job_id = ? ORDER BY id ASC LIMIT ?",
        (job_id, limit),
    )
    rows = await cursor.fetchall()
    return {"entries": [dict(r) for r in rows], "count": len(rows)}


@router.get("/jobs/{job_id}/pages/{page}")
async def get_page(job_id: str, page: int):
    """Get raw OCR HTML and structured data for a page."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT raw_html, structured_json FROM page_cache WHERE job_id = ? AND page = ?",
        (job_id, page),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Page not found")
    # 对抗审查(cr-6): structured_json 可能非 JSON（旧版本/手动编辑），
    # 直接 json.loads 会让 review 页 500；与 jobs.get_page_data 一致降级。
    try:
        structured = json.loads(row["structured_json"]) if row["structured_json"] else None
    except json.JSONDecodeError:
        structured = None
    return {
        "job_id": job_id,
        "page": page,
        "raw_html": row["raw_html"],
        "structured": structured,
    }


@router.get("/jobs/{job_id}/pages/{page}/measurements")
async def get_page_measurements(job_id: str, page: int):
    """Return measurement matrix for rendering on review page (Phase 3).

    Extracts all step[].measurements[] from the page's structured_json so the
    review template can render a time × column table with in_spec cell colors.
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT structured_json FROM page_cache WHERE job_id = ? AND page = ?",
        (job_id, page),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Page not found")
    # 对抗审查(cr-6): 同 get_page_data — 非 JSON 的 structured_json 降级为空。
    try:
        data = json.loads(row["structured_json"]) if row["structured_json"] else {}
    except json.JSONDecodeError:
        data = {}
    measurements = []
    column_set: dict[str, None] = {}  # ordered set of column names
    for step in data.get("steps", []) or []:
        for m in step.get("measurements", []) or []:
            values = m.get("values") or {}
            for col in values.keys():
                column_set.setdefault(col, None)
            measurements.append({
                "step_no": step.get("step_no"),
                "time": m.get("time"),
                "values": values,
            })
    return {
        "page": page,
        "columns": list(column_set.keys()),
        "measurements": measurements,
        "count": len(measurements),
    }
