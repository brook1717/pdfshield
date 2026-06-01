"""
Server-side page routes (no API prefix).

Routes
------
GET  /                         → index.html  (upload form)
POST /analyze                  → run pipeline, store report, redirect
GET  /report/{file_id}         → report.html (analysis dashboard)
GET  /report/{file_id}/download → ForensicReport as downloadable JSON
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.models.schemas import ForensicReport
from app.services.risk_engine import run_forensic_pipeline

BASE_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=BASE_DIR / "templates")

pages_router = APIRouter()

UPLOADS_DIR = Path(__file__).resolve().parents[2] / "uploads"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

_reports: dict[str, ForensicReport] = {}
_filenames: dict[str, str] = {}

CHECK_LABELS: dict[str, str] = {
    "metadata_analysis":      "Metadata Analysis",
    "text_layer_analysis":    "Text Layer",
    "font_consistency":       "Font Consistency",
    "coordinate_alignment":   "Coordinate Alignment",
    "hidden_overlay_detection": "Overlay Detection",
}

CHECK_ICONS: dict[str, str] = {
    "metadata_analysis":      "🏷",
    "text_layer_analysis":    "📄",
    "font_consistency":       "🔤",
    "coordinate_alignment":   "📐",
    "hidden_overlay_detection": "🔍",
}


@pages_router.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@pages_router.post("/analyze")
async def analyze_pdf(request: Request, file: UploadFile = File(...)):
    is_pdf = (file.content_type == "application/pdf") or (
        (file.filename or "").lower().endswith(".pdf")
    )
    if not is_pdf:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Only PDF files are accepted."},
            status_code=422,
        )

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "File exceeds the 10 MB limit."},
            status_code=422,
        )

    file_id = str(uuid.uuid4())
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS_DIR / f"{file_id}.pdf"
    dest.write_bytes(contents)

    report = run_forensic_pipeline(str(dest))
    _reports[file_id] = report
    _filenames[file_id] = file.filename or "document.pdf"

    return RedirectResponse(url=f"/report/{file_id}", status_code=303)


@pages_router.get("/report/{file_id}", response_class=HTMLResponse)
async def report_page(request: Request, file_id: str):
    report = _reports.get(file_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return templates.TemplateResponse(
        "report.html",
        {
            "request":      request,
            "report":       report,
            "file_id":      file_id,
            "filename":     _filenames.get(file_id, "document.pdf"),
            "check_labels": CHECK_LABELS,
            "check_icons":  CHECK_ICONS,
        },
    )


@pages_router.get("/report/{file_id}/download")
async def download_json(file_id: str):
    report = _reports.get(file_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return JSONResponse(
        content=report.model_dump(),
        headers={
            "Content-Disposition": (
                f'attachment; filename="pdfshield-{file_id[:8]}.json"'
            )
        },
    )
