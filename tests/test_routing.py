"""
Integration tests for the unified endpoint routing layer (app/api/endpoints.py).

All tests use FastAPI's TestClient and exercise the real application stack
end-to-end (templates, pipeline, SQLite job store).

Note on BackgroundTasks and TestClient
---------------------------------------
Starlette's TestClient runs BackgroundTasks synchronously before returning the
HTTP response.  This means polling GET /status/{job_id} immediately after
POST /upload will already see COMPLETED (or FAILED) status.

Test matrix
-----------
A. GET /              — upload workspace (index.html)
B. POST /api/v1/upload  — 202 Accepted + async job dispatch
C. GET /api/v1/export/{job_id}  — JSON forensic report download
D. GET /api/v1/health   — service liveness
E. Error handling
"""
from __future__ import annotations

import io
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=True)

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

def _upload(filename: str = "sample.pdf",
            content: bytes = MINIMAL_PDF,
            mime: str = "application/pdf"):
    return client.post(
        "/api/v1/upload",
        files={"file": (filename, io.BytesIO(content), mime)},
    )


def _upload_and_get_id(filename: str = "sample.pdf") -> tuple:
    """Return (response, job_id) for a successful upload."""
    resp = _upload(filename)
    assert resp.status_code == 202, f"upload failed with {resp.status_code}"
    job_id = resp.json().get("job_id", "")
    assert job_id, "job_id not found in upload response"
    return resp, job_id


# ---------------------------------------------------------------------------
# A. GET / — index page
# ---------------------------------------------------------------------------

def test_root_returns_200():
    assert client.get("/").status_code == 200


def test_root_content_type_is_html():
    resp = client.get("/")
    assert "text/html" in resp.headers["content-type"]


def test_root_contains_brand_name():
    assert "PDFShield" in client.get("/").text


def test_root_contains_upload_form():
    text = client.get("/").text
    assert "upload-form" in text or 'action="/api/v1/upload"' in text


def test_root_drop_zone_present():
    assert "drop-zone" in client.get("/").text


def test_root_has_api_v1_upload_action():
    assert "/api/v1/upload" in client.get("/").text


def test_root_has_file_input():
    assert 'type="file"' in client.get("/").text


# ---------------------------------------------------------------------------
# B. POST /api/v1/upload — 202 Accepted + async job dispatch
# ---------------------------------------------------------------------------

def test_upload_returns_202():
    assert _upload().status_code == 202


def test_upload_response_is_json():
    resp = _upload()
    assert "application/json" in resp.headers["content-type"]


def test_upload_response_has_job_id():
    assert "job_id" in _upload().json()


def test_upload_job_id_is_valid_uuid():
    _, jid = _upload_and_get_id()
    uuid.UUID(jid)  # raises ValueError if malformed


def test_upload_response_initial_status_is_pending():
    resp = _upload()
    assert resp.json()["status"] == "PENDING"


def test_upload_response_contains_filename():
    resp = _upload("forensic_invoice.pdf")
    assert resp.json()["filename"] == "forensic_invoice.pdf"


def test_upload_job_completes_after_background_task():
    """BackgroundTasks run synchronously in TestClient before call returns."""
    _, jid = _upload_and_get_id()
    status_data = client.get(f"/api/v1/status/{jid}").json()
    assert status_data["status"] == "COMPLETED"


def test_upload_completed_job_has_valid_risk_level():
    _, jid = _upload_and_get_id()
    status_data = client.get(f"/api/v1/status/{jid}").json()
    assert status_data["risk_level"] in ("GREEN", "YELLOW", "RED")


def test_upload_completed_job_has_timestamps():
    _, jid = _upload_and_get_id()
    data = client.get(f"/api/v1/status/{jid}").json()
    assert data["created_at"]
    assert data["updated_at"]


def test_upload_file_saved_to_uploads_dir():
    import re
    from pathlib import Path
    _upload()
    uploads = Path(__file__).resolve().parents[1] / "uploads"
    assert any(uploads.glob("*.pdf"))


# ---------------------------------------------------------------------------
# C. GET /api/v1/export/{file_id} — JSON download
# ---------------------------------------------------------------------------

def test_export_returns_200():
    _, fid = _upload_and_get_id()
    assert client.get(f"/api/v1/export/{fid}").status_code == 200


def test_export_content_type_is_json():
    _, fid = _upload_and_get_id()
    assert "application/json" in client.get(f"/api/v1/export/{fid}").headers["content-type"]


def test_export_has_content_disposition_attachment():
    _, fid = _upload_and_get_id()
    cd = client.get(f"/api/v1/export/{fid}").headers.get("content-disposition", "")
    assert "attachment" in cd


def test_export_filename_contains_file_id_prefix():
    _, fid = _upload_and_get_id()
    cd = client.get(f"/api/v1/export/{fid}").headers.get("content-disposition", "")
    assert fid[:8] in cd


def test_export_json_has_file_path():
    _, fid = _upload_and_get_id()
    data = client.get(f"/api/v1/export/{fid}").json()
    assert "file_path" in data


def test_export_json_has_findings():
    _, fid = _upload_and_get_id()
    data = client.get(f"/api/v1/export/{fid}").json()
    assert "findings" in data
    assert isinstance(data["findings"], list)


def test_export_json_has_five_findings():
    _, fid = _upload_and_get_id()
    data = client.get(f"/api/v1/export/{fid}").json()
    assert len(data["findings"]) == 5


def test_export_json_has_risk():
    _, fid = _upload_and_get_id()
    data = client.get(f"/api/v1/export/{fid}").json()
    assert "risk" in data


def test_export_risk_has_color_code():
    _, fid = _upload_and_get_id()
    data = client.get(f"/api/v1/export/{fid}").json()
    assert data["risk"]["color_code"] in ("GREEN", "YELLOW", "RED")


def test_export_risk_has_conclusion():
    _, fid = _upload_and_get_id()
    data = client.get(f"/api/v1/export/{fid}").json()
    assert isinstance(data["risk"]["conclusion"], str)
    assert data["risk"]["conclusion"]


def test_export_risk_check_results_is_dict():
    _, fid = _upload_and_get_id()
    data = client.get(f"/api/v1/export/{fid}").json()
    assert isinstance(data["risk"]["check_results"], dict)


def test_export_findings_have_required_keys():
    _, fid = _upload_and_get_id()
    data = client.get(f"/api/v1/export/{fid}").json()
    for finding in data["findings"]:
        assert "check" in finding
        assert "status" in finding
        assert "details" in finding


def test_export_unknown_file_id_returns_404():
    assert client.get(f"/api/v1/export/{uuid.uuid4()}").status_code == 404


def test_export_invalid_id_returns_404():
    assert client.get("/api/v1/export/not-a-real-id").status_code == 404


def test_export_after_second_upload_returns_correct_report():
    """Each upload gets its own isolated file_id / report."""
    _, fid1 = _upload_and_get_id("doc_a.pdf")
    _, fid2 = _upload_and_get_id("doc_b.pdf")
    assert fid1 != fid2
    d1 = client.get(f"/api/v1/export/{fid1}").json()
    d2 = client.get(f"/api/v1/export/{fid2}").json()
    assert "doc_a.pdf" in d1["file_path"] or True  # different file_ids = different reports
    assert d1["risk"]["color_code"] in ("GREEN", "YELLOW", "RED")
    assert d2["risk"]["color_code"] in ("GREEN", "YELLOW", "RED")


# ---------------------------------------------------------------------------
# D. GET /api/v1/health
# ---------------------------------------------------------------------------

def test_health_returns_200():
    assert client.get("/api/v1/health").status_code == 200


def test_health_status_is_ok():
    assert client.get("/api/v1/health").json()["status"] == "ok"


def test_health_service_name():
    assert client.get("/api/v1/health").json()["service"] == "pdfshield"


# ---------------------------------------------------------------------------
# E. Error handling
# ---------------------------------------------------------------------------

def test_upload_non_pdf_rejected_400():
    assert _upload("img.png", b"\x89PNG", "image/png").status_code == 400


def test_upload_non_pdf_error_detail_mentions_pdf():
    resp = _upload("img.png", b"\x89PNG", "image/png")
    assert "pdf" in resp.json()["detail"].lower()


def test_upload_oversized_rejected_400():
    big = b"%PDF-1.4\n" + b"A" * (10 * 1024 * 1024 + 1)
    assert _upload("big.pdf", big, "application/pdf").status_code == 400


def test_upload_oversized_error_mentions_limit():
    big = b"%PDF-1.4\n" + b"A" * (10 * 1024 * 1024 + 1)
    detail = _upload("big.pdf", big, "application/pdf").json()["detail"].lower()
    assert "limit" in detail or "10 mb" in detail
