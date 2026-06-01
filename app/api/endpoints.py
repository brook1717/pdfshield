"""
Unified endpoint and page routing layer.

Routers
-------
``page_router``  (no prefix, included directly in app)
    GET /                      — Upload workspace (index.html).

``router``       (mounted at /api/v1 by main.py)
    GET  /health               — Service liveness check.
    POST /upload               — Accept PDF, run full forensic pipeline,
                                 render ``report.html`` directly.
    GET  /export/{file_id}     — Return stored ForensicReport as JSON.
"""
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.models.schemas import ForensicReport
from app.services.risk_engine import run_forensic_pipeline

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

# In-memory report store keyed by file_id
_reports:   dict[str, ForensicReport] = {}
_filenames: dict[str, str]            = {}


# ---------------------------------------------------------------------------
# Page router (no /api/v1 prefix)
# ---------------------------------------------------------------------------

page_router = APIRouter()


@page_router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index_page(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ---------------------------------------------------------------------------
# API router  (mounted at /api/v1 by main.py)
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/health", tags=["health"], summary="Health check")
async def health_check():
    return {"status": "ok", "service": "pdfshield"}


@router.post(
    "/upload",
    response_class=HTMLResponse,
    tags=["analysis"],
    summary="Upload a PDF and receive the forensic report page",
)
async def upload_and_analyze(request: Request, file: UploadFile = File(...)):
    """
    Accept a PDF upload, run the full forensic pipeline, and render the
    ``report.html`` dashboard directly.  No raw JSON is returned to the
    browser.

    Validation errors (wrong type / oversized) still raise
    :class:`~fastapi.HTTPException` so programmatic callers get a
    machine-readable 400 response.
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

    file_id = str(uuid.uuid4())
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS_DIR / f"{file_id}.pdf"
    dest.write_bytes(contents)

    try:
        report = run_forensic_pipeline(str(dest))
    except Exception as exc:
        logger.error("pipeline failed for %s: %s", file_id, exc)
        raise HTTPException(status_code=500, detail="PDF processing failed.")

    filename = file.filename or "document.pdf"
    _reports[file_id]   = report
    _filenames[file_id] = filename

    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "report":       report,
            "file_id":      file_id,
            "filename":     filename,
            "check_labels": CHECK_LABELS,
            "check_icons":  CHECK_ICONS,
        },
    )


@router.get(
    "/export/{file_id}",
    tags=["export"],
    summary="Download the raw JSON forensic report",
    response_class=JSONResponse,
)
async def export_report(file_id: str):
    """
    Return the stored :class:`~app.models.schemas.ForensicReport` as a
    downloadable JSON file for audit logs or external processing.
    """
    report = _reports.get(file_id)
    if not report:
        raise HTTPException(
            status_code=404, detail=f"Report '{file_id}' not found."
        )
    return JSONResponse(
        content=report.model_dump(),
        headers={
            "Content-Disposition": (
                f'attachment; filename="pdfshield-{file_id[:8]}.json"'
            )
        },
    )
