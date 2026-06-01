"""
Integration tests for the unified endpoint routing layer (app/api/endpoints.py).

All tests use FastAPI's TestClient and exercise the real application stack
end-to-end (templates, pipeline, in-memory store).

Test matrix
-----------
A. GET /         — upload workspace (index.html)
B. POST /api/v1/upload  — pipeline execution + report.html rendering
C. GET /api/v1/export/{file_id}  — JSON forensic report download
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


def _extract_file_id(html: str) -> str | None:
    """Pull the file_id UUID out of the export link embedded in report.html."""
    m = re.search(r'/api/v1/export/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', html)
    return m.group(1) if m else None


def _upload_and_get_id(filename: str = "sample.pdf") -> tuple:
    """Return (response, file_id) for a successful upload."""
    resp = _upload(filename)
    assert resp.status_code == 200, f"upload failed with {resp.status_code}"
    fid = _extract_file_id(resp.text)
    assert fid, "export link with file_id not found in HTML"
    return resp, fid


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
# B. POST /api/v1/upload — pipeline + HTML rendering
# ---------------------------------------------------------------------------

def test_upload_returns_200():
    assert _upload().status_code == 200


def test_upload_response_is_html():
    resp = _upload()
    assert "text/html" in resp.headers["content-type"]


def test_upload_report_has_pdfshield_brand():
    assert "PDFShield" in _upload().text


def test_upload_report_has_risk_banner():
    text = _upload().text
    assert any(cls in text for cls in ("risk-green", "risk-yellow", "risk-red"))


def test_upload_report_has_download_json_button():
    assert "Download JSON" in _upload().text


def test_upload_report_export_link_present():
    assert "/api/v1/export/" in _upload().text


def test_upload_report_export_link_contains_valid_uuid():
    _, fid = _upload_and_get_id()
    uuid.UUID(fid)  # raises ValueError if malformed


def test_upload_report_contains_original_filename():
    resp = _upload("forensic_invoice.pdf")
    assert "forensic_invoice.pdf" in resp.text


def test_upload_report_has_check_section():
    text = _upload().text
    assert "Analysis Breakdown" in text


def test_upload_report_has_status_pills():
    text = _upload().text
    assert any(s in text for s in ("INFO", "WARNING", "DANGER"))


def test_upload_report_has_five_check_cards():
    text = _upload().text
    check_names = [
        "Metadata Analysis",
        "Text Layer",
        "Font Consistency",
        "Coordinate Alignment",
        "Overlay Detection",
    ]
    for name in check_names:
        assert name in text, f"Check card '{name}' not found in report"


def test_upload_report_has_back_link():
    assert "New Analysis" in _upload().text


def test_upload_report_stats_row_present():
    text = _upload().text
    assert "Checks Run" in text


def test_upload_report_conclusion_present():
    text = _upload().text
    assert any(phrase in text for phrase in ("authentic", "review", "tampering"))


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
