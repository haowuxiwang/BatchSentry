"""Job management API — upload PDF, check status."""
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from db.client import get_db
from core.pipeline import run_pipeline

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("")
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """Upload a PDF and start OCR + analysis pipeline."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files accepted")

    job_id = str(uuid.uuid4())[:12]
    job_dir = Path("output") / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = job_dir / file.filename
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    db = await get_db()
    await db.execute(
        "INSERT INTO jobs (id, filename, status, pdf_path) VALUES (?, ?, 'pending', ?)",
        (job_id, file.filename, str(pdf_path)),
    )
    await db.commit()

    # Launch async pipeline
    background_tasks.add_task(run_pipeline, job_id, str(pdf_path))

    return {"job_id": job_id, "filename": file.filename, "status": "pending"}


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
        "pages_ocr_done": pages_ocr,
        "pages_analyzed": pages_analyzed,
        "total_findings": total_findings,
        "review_findings": review_findings,
        "created_at": job["created_at"],
        "finished_at": job["finished_at"],
        "error_message": job["error_message"],
    }
