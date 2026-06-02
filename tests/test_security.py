"""
Security hardening regression tests.

Test matrix
-----------
A. Secure filename — path-traversal, sanitisation, edge cases
B. Security response headers — X-Content-Type-Options, X-Frame-Options, CSP
C. Rate limiting — POST /api/v1/upload: max 5/min per IP, 6th → 429
D. Upload file cleanup on task failure
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=False)

MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
    b"xref\n0 4\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000058 00000 n \n"
    b"0000000115 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\n"
    b"startxref\n190\n%%EOF\n"
)


def _upload(fname: str = "test.pdf", content: bytes = MINIMAL_PDF) -> object:
    return client.post(
        "/api/v1/upload",
        files={"file": (fname, io.BytesIO(content), "application/pdf")},
    )


# ---------------------------------------------------------------------------
# A. Secure filename
# ---------------------------------------------------------------------------

from app.utils.secure_filename import secure_filename  # noqa: E402


def test_secure_filename_normal_pdf():
    assert secure_filename("invoice_2024.pdf") == "invoice_2024.pdf"


def test_secure_filename_spaces_become_underscores():
    assert secure_filename("My Invoice Jan 2024.pdf") == "My_Invoice_Jan_2024.pdf"


def test_secure_filename_unix_path_traversal():
    result = secure_filename("../../etc/passwd")
    assert ".." not in result
    assert "/" not in result


def test_secure_filename_absolute_unix_path():
    result = secure_filename("/etc/passwd")
    assert result == "passwd"


def test_secure_filename_windows_backslash_traversal():
    result = secure_filename("..\\..\\Windows\\System32\\cmd.exe")
    assert ".." not in result
    assert "\\" not in result


def test_secure_filename_windows_absolute_path():
    result = secure_filename("C:\\Windows\\System32\\cmd.exe")
    assert result == "cmd.exe"


def test_secure_filename_leading_dots_stripped():
    result = secure_filename("...hidden_file.pdf")
    assert not result.startswith(".")


def test_secure_filename_consecutive_dots_collapsed():
    result = secure_filename("some....name.pdf")
    assert ".." not in result


def test_secure_filename_non_ascii_normalised():
    result = secure_filename("rëport_ñoño.pdf")
    assert all(ord(c) < 128 for c in result)


def test_secure_filename_empty_returns_default():
    assert secure_filename("") == "document.pdf"


def test_secure_filename_whitespace_only_returns_default():
    assert secure_filename("   ") == "document.pdf"


def test_secure_filename_long_name_truncated():
    long_name = "a" * 300 + ".pdf"
    result = secure_filename(long_name)
    assert len(result) <= 255
    assert result.endswith(".pdf")


def test_secure_filename_null_byte_stripped():
    result = secure_filename("evil\x00file.pdf")
    assert "\x00" not in result


def test_secure_filename_mixed_traversal_and_extension():
    result = secure_filename("../../report.pdf")
    assert ".." not in result
    assert result.endswith(".pdf")


# ---------------------------------------------------------------------------
# B. Security response headers
# ---------------------------------------------------------------------------


def test_security_header_x_content_type_options_on_root():
    resp = client.get("/")
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_security_header_x_frame_options_on_root():
    resp = client.get("/")
    assert resp.headers.get("x-frame-options") == "DENY"


def test_security_header_csp_present_on_root():
    resp = client.get("/")
    assert "content-security-policy" in resp.headers


def test_security_header_csp_contains_default_self():
    resp = client.get("/")
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp


def test_security_header_csp_blocks_objects():
    resp = client.get("/")
    csp = resp.headers.get("content-security-policy", "")
    assert "object-src 'none'" in csp


def test_security_header_csp_denies_framing():
    resp = client.get("/")
    csp = resp.headers.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in csp


def test_security_header_referrer_policy_on_root():
    resp = client.get("/")
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_security_header_xss_protection_disabled():
    resp = client.get("/")
    assert resp.headers.get("x-xss-protection") == "0"


def test_security_headers_present_on_upload_202():
    resp = _upload()
    assert resp.status_code == 202
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert "content-security-policy" in resp.headers


def test_security_headers_present_on_404():
    resp = client.get("/api/v1/status/nonexistent-id")
    assert resp.status_code == 404
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert "content-security-policy" in resp.headers


def test_security_headers_present_on_health():
    resp = client.get("/api/v1/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"


# ---------------------------------------------------------------------------
# C. Rate limiting — POST /api/v1/upload (5/minute per IP)
# ---------------------------------------------------------------------------


def _reset_limiter_storage() -> None:
    """Best-effort clear of the in-memory rate-limit storage."""
    from app.middleware.rate_limit import limiter

    try:
        limiter._limiter.storage.reset()
    except AttributeError:
        pass


def test_rate_limit_first_five_uploads_succeed():
    from app.middleware.rate_limit import limiter

    _reset_limiter_storage()
    limiter.enabled = True
    try:
        for _ in range(5):
            resp = _upload()
            assert resp.status_code == 202, f"Expected 202, got {resp.status_code}"
    finally:
        limiter.enabled = False
        _reset_limiter_storage()


def test_rate_limit_sixth_upload_returns_429():
    from app.middleware.rate_limit import limiter

    _reset_limiter_storage()
    limiter.enabled = True
    try:
        for _ in range(5):
            _upload()
        resp = _upload()
        assert resp.status_code == 429
    finally:
        limiter.enabled = False
        _reset_limiter_storage()


def test_rate_limit_response_body_on_429():
    from app.middleware.rate_limit import limiter

    _reset_limiter_storage()
    limiter.enabled = True
    try:
        for _ in range(5):
            _upload()
        resp = _upload()
        assert resp.status_code == 429
        body = resp.text.lower()
        assert "rate" in body or "limit" in body or "too many" in body
    finally:
        limiter.enabled = False
        _reset_limiter_storage()


def test_rate_limit_not_applied_to_status_endpoint():
    """GET /status is not rate-limited; many requests must all succeed."""
    from app.middleware.rate_limit import limiter

    _reset_limiter_storage()
    limiter.enabled = True
    try:
        resp = _upload()
        job_id = resp.json()["job_id"]
        for _ in range(10):
            r = client.get(f"/api/v1/status/{job_id}")
            assert r.status_code == 200
    finally:
        limiter.enabled = False
        _reset_limiter_storage()


def test_rate_limit_reset_allows_new_uploads():
    """After storage reset the counter restarts from zero."""
    from app.middleware.rate_limit import limiter

    _reset_limiter_storage()
    limiter.enabled = True
    try:
        for _ in range(5):
            _upload()
        _reset_limiter_storage()
        resp = _upload()
        assert resp.status_code == 202
    finally:
        limiter.enabled = False
        _reset_limiter_storage()


# ---------------------------------------------------------------------------
# D. Upload file cleanup on task failure
# ---------------------------------------------------------------------------


def test_failed_task_removes_upload_file(monkeypatch: pytest.MonkeyPatch):
    """When the forensic pipeline raises, the uploaded PDF must be deleted."""
    import app.services.analysis_task as at

    captured_path: list[str] = []

    def _boom(path: str) -> None:
        captured_path.append(path)
        raise RuntimeError("simulated pipeline failure")

    monkeypatch.setattr(at, "run_forensic_pipeline", _boom)

    resp = _upload()
    assert resp.status_code == 202
    assert len(captured_path) == 1, "pipeline should have been called once"

    upload_file = Path(captured_path[0])
    assert not upload_file.exists(), (
        f"Failed-task upload file was NOT removed: {upload_file}"
    )


def test_successful_task_keeps_upload_file():
    """On a successful analysis the uploaded PDF must remain on disk."""
    resp = _upload()
    assert resp.status_code == 202

    from app.db.jobs import get_job

    job = get_job(resp.json()["job_id"])
    assert job is not None
    assert job["status"] == "COMPLETED"

    upload_dir = Path(__file__).resolve().parents[1] / "uploads"
    assert any(upload_dir.glob("*.pdf")), "At least one upload should persist after success"
