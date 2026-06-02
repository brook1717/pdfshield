import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type /Catalog /Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type /Pages /Kids [3 0 R] /Count 1>>endobj\n"
    b"3 0 obj<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n"
    b"trailer<</Size 4 /Root 1 0 R>>\n"
    b"startxref\n0\n%%EOF\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upload(filename: str, content: bytes, content_type: str):
    return client.post(
        "/api/v1/upload",
        files={"file": (filename, io.BytesIO(content), content_type)},
    )


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------

def test_upload_valid_pdf_returns_202():
    response = _upload("sample.pdf", MINIMAL_PDF, "application/pdf")
    assert response.status_code == 202


def test_upload_valid_pdf_returns_json():
    response = _upload("sample.pdf", MINIMAL_PDF, "application/pdf")
    assert "application/json" in response.headers["content-type"]


def test_upload_response_has_job_id():
    response = _upload("sample.pdf", MINIMAL_PDF, "application/pdf")
    assert "job_id" in response.json()


def test_upload_response_status_is_pending():
    response = _upload("sample.pdf", MINIMAL_PDF, "application/pdf")
    assert response.json()["status"] == "PENDING"


def test_upload_preserves_original_filename():
    response = _upload("my_document.pdf", MINIMAL_PDF, "application/pdf")
    assert response.json()["filename"] == "my_document.pdf"


def test_upload_file_is_saved_on_disk():
    _upload("sample.pdf", MINIMAL_PDF, "application/pdf")
    uploads = Path(__file__).resolve().parents[1] / "uploads"
    assert any(uploads.glob("*.pdf"))


def test_upload_pdf_extension_accepted_with_octet_stream_mime():
    """A .pdf file sent with application/octet-stream must still be accepted."""
    response = _upload("report.pdf", MINIMAL_PDF, "application/octet-stream")
    assert response.status_code == 202


def test_upload_job_id_is_valid_uuid():
    import uuid
    response = _upload("sample.pdf", MINIMAL_PDF, "application/pdf")
    job_id = response.json().get("job_id", "")
    uuid.UUID(job_id)  # raises ValueError if not a valid UUID


# ---------------------------------------------------------------------------
# Rejection: wrong type
# ---------------------------------------------------------------------------

def test_upload_non_pdf_mime_rejected():
    response = _upload("image.png", b"\x89PNG\r\n", "image/png")
    assert response.status_code == 400


def test_upload_non_pdf_mime_error_message():
    response = _upload("image.png", b"\x89PNG\r\n", "image/png")
    assert "pdf" in response.json()["detail"].lower()


def test_upload_text_file_rejected():
    response = _upload("notes.txt", b"hello world", "text/plain")
    assert response.status_code == 400


def test_upload_docx_rejected():
    response = _upload("doc.docx", b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert response.status_code == 400


def test_upload_wrong_extension_with_pdf_mime_accepted():
    """Content-type wins: application/pdf should still be accepted even with a mismatched extension."""
    response = _upload("file.bin", MINIMAL_PDF, "application/pdf")
    assert response.status_code == 202


# ---------------------------------------------------------------------------
# Rejection: oversized file
# ---------------------------------------------------------------------------

def test_upload_oversized_file_rejected():
    oversized = b"%PDF-1.4\n" + b"A" * (10 * 1024 * 1024 + 1)
    response = _upload("big.pdf", oversized, "application/pdf")
    assert response.status_code == 400


def test_upload_oversized_error_message():
    oversized = b"%PDF-1.4\n" + b"A" * (10 * 1024 * 1024 + 1)
    response = _upload("big.pdf", oversized, "application/pdf")
    assert "10 mb" in response.json()["detail"].lower() or "limit" in response.json()["detail"].lower()


def test_upload_exactly_at_limit_not_rejected_for_size():
    """A file exactly at the 10 MB boundary must not be rejected by the size guard."""
    at_limit = b"%PDF-1.4\n" + b"A" * (10 * 1024 * 1024 - 9)
    response = _upload("exact.pdf", at_limit, "application/pdf")
    assert response.status_code != 400  # size guard did not fire
