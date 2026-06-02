"""
Unified endpoint and page routing layer.

Routers
-------
``page_router``  (no prefix, included directly in app)
    GET /                          — Upload workspace (index.html).

``router``       (mounted at /api/v1 by main.py)
    GET  /health                   — Service liveness check.
    POST /upload                   — Accept PDF, write PENDING job record,
                                     dispatch forensic pipeline as a
                                     BackgroundTask, return 202 + job_id.
    GET  /status/{job_id}          — Current job state and result metadata.
    GET  /export/{job_id}          — Download completed ForensicReport as JSON.
"""
import json
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db.jobs import COMPLETED, create_job, get_job
from app.middleware.rate_limit import limiter
from app.models.schemas import ForensicReport
from app.services.analysis_task import run_analysis_task
from app.utils.secure_filename import secure_filename

logger = logging.getLogger(__name__)

BASE_DIR      = Path(__file__).resolve().parents[1]   # app/
UPLOADS_DIR   = Path(__file__).resolve().parents[2] / "uploads"
MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 MB
ALLOWED_MIME  = {"application/pdf"}

templates = Jinja2Templates(directory=BASE_DIR / "templates")

CHECK_LABELS: dict[str, str] = {
    "metadata_analysis":       "Metadata Analysis",
    "text_layer_analysis":     "Text Layer",
    "font_consistency":        "Font Consistency",
    "coordinate_alignment":    "Coordinate Alignment",
    "hidden_overlay_detection": "Overlay Detection",
}
CHECK_ICONS: dict[str, str] = {
    "metadata_analysis":       "\U0001f3f7",
    "text_layer_analysis":     "\U0001f4c4",
    "font_consistency":        "\U0001f524",
    "coordinate_alignment":    "\U0001f4d0",
    "hidden_overlay_detection": "\U0001f50d",
}



# ---------------------------------------------------------------------------
# Page router (no /api/v1 prefix)
# ---------------------------------------------------------------------------

page_router = APIRouter()


@page_router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index_page(request: Request):
    return templates.TemplateResponse(request, "index.html")


@page_router.get("/processing/{job_id}", response_class=HTMLResponse, include_in_schema=False)
async def processing_page(request: Request, job_id: str):
    """
    Render the processing-state page for *job_id*.

    The page polls ``GET /api/v1/status/{job_id}`` and auto-redirects to
    ``/report/{job_id}`` once the job completes.
    """
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return templates.TemplateResponse(
        request,
        "processing.html",
        {"job_id": job_id, "filename": job["filename"], "status": job["status"]},
    )


@page_router.get("/report/{job_id}", response_class=HTMLResponse, include_in_schema=False)
async def report_page(request: Request, job_id: str):
    """
    Render the completed forensic report for *job_id*.

    Redirects back to ``/processing/{job_id}`` if the job has not yet
    finished so the user can watch the polling animation.
    """
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if job["status"] != COMPLETED:
        return RedirectResponse(url=f"/processing/{job_id}")
    report = ForensicReport.model_validate_json(job["results_json"])
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "report":        report,
            "job_id":        job_id,
            "filename":      job["filename"],
            "annotated_url": job.get("annotated_url"),
            "check_labels":  dict(CHECK_LABELS),
            "check_icons":   dict(CHECK_ICONS),
        },
    )


# ---------------------------------------------------------------------------
# API router  (mounted at /api/v1 by main.py)
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/health", tags=["health"], summary="Health check")
async def health_check():
    return {"status": "ok", "service": "pdfshield"}


@router.post(
    "/upload",
    status_code=202,
    tags=["analysis"],
    summary="Submit a PDF for asynchronous forensic analysis",
)
@limiter.limit("5/minute")
async def upload_and_analyze(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """
    Accept a PDF upload, write a ``PENDING`` job record, and immediately
    dispatch the forensic pipeline as a background task.

    Returns ``202 Accepted`` with a ``job_id`` that can be polled via
    ``GET /api/v1/status/{job_id}`` and used to download the report via
    ``GET /api/v1/export/{job_id}`` once the analysis completes.

    Validation errors (wrong MIME type / oversized file) still raise
    :class:`~fastapi.HTTPException` ``400`` so callers get a machine-readable
    error before any disk I/O occurs.
    """
    is_pdf_mime = file.content_type in ALLOWED_MIME
    is_pdf_ext  = (file.filename or "").lower().endswith(".pdf")

    if not (is_pdf_mime or is_pdf_ext):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{file.content_type}'. Only PDF files are accepted.",
        )

    contents = await file.read()

    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File size {len(contents)} bytes exceeds the 10 MB limit.",
        )

    filename = secure_filename(file.filename or "document.pdf")
    job_id   = str(uuid.uuid4())
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS_DIR / f"{job_id}.pdf"
    dest.write_bytes(contents)
    try:
        create_job(job_id, filename)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    background_tasks.add_task(run_analysis_task, job_id, str(dest), filename)

    logger.info("upload: queued job_id=%s filename=%s", job_id, filename)
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "status": "PENDING", "filename": filename},
    )


@router.get(
    "/status/{job_id}",
    tags=["analysis"],
    summary="Get the current state of an analysis job",
)
async def get_job_status(job_id: str):
    """
    Return the current status and result metadata for *job_id*.

    The full report payload is **not** included; use
    ``GET /api/v1/export/{job_id}`` to download the complete JSON report.

    Raises ``404`` when *job_id* is not recognised.
    """
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return {
        "job_id":        job["job_id"],
        "filename":      job["filename"],
        "status":        job["status"],
        "risk_level":    job.get("risk_level"),
        "annotated_url": job.get("annotated_url"),
        "created_at":    job["created_at"],
        "updated_at":    job["updated_at"],
    }


@router.get(
    "/export/{job_id}",
    tags=["export"],
    summary="Download the completed ForensicReport as JSON",
    response_class=JSONResponse,
)
async def export_report(job_id: str):
    """
    Return the :class:`~app.models.schemas.ForensicReport` for *job_id* as a
    downloadable JSON attachment for audit logs or external processing.

    Raises ``404`` when *job_id* is not found or the analysis has not yet
    completed successfully.
    """
    job = get_job(job_id)
    if job is None or job["status"] != COMPLETED or not job.get("results_json"):
        raise HTTPException(
            status_code=404,
            detail=f"Report '{job_id}' not found or analysis not yet complete.",
        )
    return JSONResponse(
        content=json.loads(job["results_json"]),
        headers={
            "Content-Disposition": (
                f'attachment; filename="pdfshield-{job_id[:8]}.json"'
            )
        },
    )
