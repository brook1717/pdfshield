"""
Tests for the SQLite job state store (app/db/jobs.py) and the
GET /api/v1/status/{job_id} endpoint.

Test matrix
-----------
A. Database layer — schema and CRUD
   A1. init_db is idempotent (safe to call multiple times)
   A2. create_job inserts a PENDING record
   A3. create_job stores the correct filename
   A4. create_job populates created_at and updated_at
   A5. get_job returns None for an unknown job_id
   A6. update_job transitions status correctly
   A7. update_job persists risk_level
   A8. update_job persists annotated_url
   A9. update_job persists results_json
   A10. update_job raises ValueError for an invalid status string
   A11. COALESCE: update_job with None fields leaves existing values unchanged
   A12. Two successive updates each advance updated_at

B. Status constants
   B1. STATUSES frozenset contains all four expected values
   B2. Each status constant matches its string value

C. GET /api/v1/status/{job_id} — HTTP endpoint
   C1. Returns 404 for an unknown job_id
   C2. Returns 200 for a known job_id after upload
   C3. Response JSON contains all expected keys
   C4. status field matches the DB value
   C5. risk_level is None for a PENDING / PROCESSING job (before task runs)
   C6. status is COMPLETED after background task completes
   C7. risk_level is present and valid after completion
   C8. annotated_url is a string or None after completion
   C9. filename in response matches the uploaded filename

D. Full upload → status round-trip (integration)
   D1. Upload then poll status → COMPLETED
   D2. Export is available once COMPLETED
   D3. Two independent uploads produce two independent job records
"""
from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.jobs import (
    COMPLETED,
    FAILED,
    PENDING,
    PROCESSING,
    STATUSES,
    create_job,
    get_job,
    init_db,
    update_job,
)
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


def _upload(filename: str = "sample.pdf") -> dict:
    """Upload MINIMAL_PDF and return the parsed JSON body."""
    resp = client.post(
        "/api/v1/upload",
        files={"file": (filename, io.BytesIO(MINIMAL_PDF), "application/pdf")},
    )
    assert resp.status_code == 202
    return resp.json()


# ---------------------------------------------------------------------------
# A. Database layer
# ---------------------------------------------------------------------------

class TestDbLayer:
    def test_a1_init_db_is_idempotent(self) -> None:
        init_db()
        init_db()  # must not raise

    def test_a2_create_job_inserts_pending_record(self) -> None:
        jid = str(uuid.uuid4())
        create_job(jid, "test.pdf")
        record = get_job(jid)
        assert record is not None
        assert record["status"] == PENDING

    def test_a3_create_job_stores_filename(self) -> None:
        jid = str(uuid.uuid4())
        create_job(jid, "my_invoice.pdf")
        assert get_job(jid)["filename"] == "my_invoice.pdf"

    def test_a4_create_job_has_timestamps(self) -> None:
        jid = str(uuid.uuid4())
        create_job(jid, "ts_test.pdf")
        rec = get_job(jid)
        assert rec["created_at"]
        assert rec["updated_at"]

    def test_a5_get_job_returns_none_for_unknown(self) -> None:
        assert get_job(str(uuid.uuid4())) is None

    def test_a6_update_job_transitions_status(self) -> None:
        jid = str(uuid.uuid4())
        create_job(jid, "flow.pdf")
        update_job(jid, status=PROCESSING)
        assert get_job(jid)["status"] == PROCESSING
        update_job(jid, status=COMPLETED, results_json="{}")
        assert get_job(jid)["status"] == COMPLETED

    def test_a7_update_job_persists_risk_level(self) -> None:
        jid = str(uuid.uuid4())
        create_job(jid, "risk.pdf")
        update_job(jid, status=COMPLETED, risk_level="RED", results_json="{}")
        assert get_job(jid)["risk_level"] == "RED"

    def test_a8_update_job_persists_annotated_url(self) -> None:
        jid = str(uuid.uuid4())
        create_job(jid, "annot.pdf")
        url = "/static/annotated/annot_p0.png"
        update_job(jid, status=COMPLETED, annotated_url=url, results_json="{}")
        assert get_job(jid)["annotated_url"] == url

    def test_a9_update_job_persists_results_json(self) -> None:
        jid = str(uuid.uuid4())
        create_job(jid, "results.pdf")
        payload = '{"file_path": "/tmp/x.pdf"}'
        update_job(jid, status=COMPLETED, results_json=payload)
        assert get_job(jid)["results_json"] == payload

    def test_a10_update_job_raises_on_invalid_status(self) -> None:
        jid = str(uuid.uuid4())
        create_job(jid, "bad_status.pdf")
        with pytest.raises(ValueError, match="Invalid job status"):
            update_job(jid, status="RUNNING")

    def test_a11_coalesce_none_preserves_existing_values(self) -> None:
        jid = str(uuid.uuid4())
        create_job(jid, "coalesce.pdf")
        update_job(jid, status=COMPLETED, risk_level="GREEN", results_json="{}")
        # Second update with risk_level=None must NOT overwrite "GREEN"
        update_job(jid, status=COMPLETED, risk_level=None)
        assert get_job(jid)["risk_level"] == "GREEN"

    def test_a12_successive_updates_advance_updated_at(self) -> None:
        import time
        jid = str(uuid.uuid4())
        create_job(jid, "ts2.pdf")
        t1 = get_job(jid)["updated_at"]
        time.sleep(0.01)
        update_job(jid, status=PROCESSING)
        t2 = get_job(jid)["updated_at"]
        assert t2 >= t1  # updated_at must not go backwards


# ---------------------------------------------------------------------------
# B. Status constants
# ---------------------------------------------------------------------------

class TestStatusConstants:
    def test_b1_statuses_contains_all_four(self) -> None:
        assert STATUSES == {"PENDING", "PROCESSING", "COMPLETED", "FAILED"}

    def test_b2_constant_values_match_strings(self) -> None:
        assert PENDING    == "PENDING"
        assert PROCESSING == "PROCESSING"
        assert COMPLETED  == "COMPLETED"
        assert FAILED     == "FAILED"


# ---------------------------------------------------------------------------
# C. GET /api/v1/status/{job_id}
# ---------------------------------------------------------------------------

class TestStatusEndpoint:
    def test_c1_unknown_job_returns_404(self) -> None:
        resp = client.get(f"/api/v1/status/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_c2_known_job_returns_200(self) -> None:
        body = _upload()
        resp = client.get(f"/api/v1/status/{body['job_id']}")
        assert resp.status_code == 200

    def test_c3_response_has_expected_keys(self) -> None:
        body = _upload()
        data = client.get(f"/api/v1/status/{body['job_id']}").json()
        for key in ("job_id", "filename", "status", "risk_level",
                    "annotated_url", "created_at", "updated_at"):
            assert key in data, f"missing key: {key}"

    def test_c4_status_matches_db(self) -> None:
        body  = _upload()
        jid   = body["job_id"]
        data  = client.get(f"/api/v1/status/{jid}").json()
        db_rec = get_job(jid)
        assert data["status"] == db_rec["status"]

    def test_c5_risk_level_none_before_completion(self) -> None:
        # Insert a bare PENDING record without running the pipeline
        jid = str(uuid.uuid4())
        create_job(jid, "pending_only.pdf")
        data = client.get(f"/api/v1/status/{jid}").json()
        assert data["risk_level"] is None

    def test_c6_status_completed_after_background_task(self) -> None:
        # TestClient runs BackgroundTasks synchronously before returning
        body = _upload()
        data = client.get(f"/api/v1/status/{body['job_id']}").json()
        assert data["status"] == COMPLETED

    def test_c7_risk_level_valid_after_completion(self) -> None:
        body = _upload()
        data = client.get(f"/api/v1/status/{body['job_id']}").json()
        assert data["risk_level"] in ("GREEN", "YELLOW", "RED")

    def test_c8_annotated_url_is_str_or_none(self) -> None:
        body = _upload()
        data = client.get(f"/api/v1/status/{body['job_id']}").json()
        assert data["annotated_url"] is None or isinstance(data["annotated_url"], str)

    def test_c9_filename_matches_upload(self) -> None:
        body = _upload("my_report.pdf")
        data = client.get(f"/api/v1/status/{body['job_id']}").json()
        assert data["filename"] == "my_report.pdf"


# ---------------------------------------------------------------------------
# D. Full upload → status round-trip (integration)
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_d1_upload_then_poll_gives_completed(self) -> None:
        body = _upload()
        status = client.get(f"/api/v1/status/{body['job_id']}").json()["status"]
        assert status == COMPLETED

    def test_d2_export_available_after_completion(self) -> None:
        body = _upload()
        jid  = body["job_id"]
        # Wait for BG task (already done in TestClient)
        exp_resp = client.get(f"/api/v1/export/{jid}")
        assert exp_resp.status_code == 200
        data = exp_resp.json()
        assert "findings" in data
        assert "risk" in data

    def test_d3_two_uploads_have_independent_job_records(self) -> None:
        b1 = _upload("doc1.pdf")
        b2 = _upload("doc2.pdf")
        assert b1["job_id"] != b2["job_id"]
        r1 = client.get(f"/api/v1/status/{b1['job_id']}").json()
        r2 = client.get(f"/api/v1/status/{b2['job_id']}").json()
        assert r1["job_id"] != r2["job_id"]
        assert r1["status"] == COMPLETED
        assert r2["status"] == COMPLETED
