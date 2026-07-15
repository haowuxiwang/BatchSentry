"""Review API — list/update findings."""
from typing import Optional
from fastapi import APIRouter, HTTPException

from db.client import get_db

router = APIRouter(prefix="/api", tags=["review"])


@router.get("/jobs/{job_id}/findings")
async def list_findings(job_id: str, status: Optional[str] = None):
    """List findings for a job, optionally filtered by status."""
    db = await get_db()
    if status:
        cursor = await db.execute(
            "SELECT * FROM findings WHERE job_id = ? AND status = ? ORDER BY page, id",
            (job_id, status),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM findings WHERE job_id = ? ORDER BY page, id", (job_id,)
        )
    rows = await cursor.fetchall()
    findings = [dict(r) for r in rows]
    return {"findings": findings, "count": len(findings)}


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
    status: Optional[str] = None,
    reviewer_note: Optional[str] = None,
    corrected_text: Optional[str] = None,
):
    """Update a finding (confirm/reject/correct)."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT id FROM findings WHERE id = ? AND job_id = ?", (finding_id, job_id)
    )
    if not await cursor.fetchone():
        raise HTTPException(404, "Finding not found")

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

    if not sets:
        raise HTTPException(400, "No fields to update")

    params.extend([finding_id, job_id])
    await db.execute(
        f"UPDATE findings SET {', '.join(sets)} WHERE id = ? AND job_id = ?",
        params,
    )
    await db.commit()
    return {"ok": True}


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
    import json
    return {
        "job_id": job_id,
        "page": page,
        "raw_html": row["raw_html"],
        "structured": json.loads(row["structured_json"]) if row["structured_json"] else None,
    }
