import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from app.models.schemas import UploadResponse

router = APIRouter()

UPLOADS_DIR = Path(__file__).resolve().parents[2] / "uploads"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_MIME = {"application/pdf"}


@router.get("/health", tags=["health"], summary="Health check")
async def health_check():
    return {"status": "ok", "service": "pdfshield"}


@router.post("/upload", response_model=UploadResponse, tags=["upload"], summary="Upload a PDF file")
async def upload_pdf(file: UploadFile) -> UploadResponse:
    is_pdf_mime = file.content_type in ALLOWED_MIME
    is_pdf_ext = (file.filename or "").lower().endswith(".pdf")

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
    dest = UPLOADS_DIR / f"{file_id}.pdf"
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(contents)

    return UploadResponse(
        file_id=file_id,
        original_filename=file.filename or "",
        storage_path=str(dest),
    )
