"""Pharma Batch Checker — FastAPI entry point."""
import logging
from contextlib import asynccontextmanager

import re
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, FileResponse

from config import config
from db.client import get_db, close_db, init_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from api.jobs import router as jobs_router
from api.review import router as review_router
from api.report import router as report_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Pharma Batch Checker...")
    db = await get_db()
    await init_schema(db)
    logger.info("Database initialized.")
    yield
    await close_db()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="Pharma Batch Checker",
    description="GMP 批生产记录半自动合规检查系统",
    version="0.1.0",
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Templates
templates = Jinja2Templates(directory="templates")

app.include_router(jobs_router)
app.include_router(review_router)
app.include_router(report_router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Upload page placeholder."""
    return templates.TemplateResponse(request, "upload.html", {})


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
async def review_page(job_id: str, request: Request, page: int = 1):
    """Review UI page: left PDF viewer + right OCR text + findings."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, "Job not found")

    total_pages = job["total_pages"] or 0

    # Get OCR text for requested page
    cursor = await db.execute(
        "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = ?",
        (job_id, page),
    )
    row = await cursor.fetchone()
    raw_html = row["raw_html"] if row else ""
    # Strip HTML tags for display
    ocr_text = re.sub(r"<[^>]+>", " ", raw_html) if raw_html else ""
    ocr_text = re.sub(r"\s+", " ", ocr_text).strip()

    # Get findings for requested page
    cursor = await db.execute(
        "SELECT * FROM findings WHERE job_id = ? AND page = ? ORDER BY severity, id",
        (job_id, page),
    )
    findings = [dict(r) for r in await cursor.fetchall()]

    return templates.TemplateResponse(request, "review.html", {
        "job_id": job_id,
        "filename": job["filename"],
        "status": job["status"],
        "page": page,
        "total_pages": total_pages,
        "ocr_text": ocr_text[:5000],
        "findings": findings,
        "pdf_url": f"/api/jobs/{job_id}/pdf",
    })


@app.get("/api/jobs/{job_id}/pdf")
async def serve_pdf(job_id: str):
    """Serve the original PDF for in-browser preview."""
    db = await get_db()
    cursor = await db.execute("SELECT pdf_path FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row or not row["pdf_path"]:
        raise HTTPException(404, "PDF not found")
    pdf_path = Path(row["pdf_path"])
    if not pdf_path.exists():
        raise HTTPException(404, "PDF file missing")
    return FileResponse(pdf_path, media_type="application/pdf", filename=pdf_path.name)


@app.get("/jobs", response_class=HTMLResponse)
async def job_list(request: Request):
    """List all jobs."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs ORDER BY created_at DESC")
    jobs = [dict(r) for r in await cursor.fetchall()]
    return templates.TemplateResponse(request, "upload.html", {
        "request": request,
        "jobs": jobs,
    })
