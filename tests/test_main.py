from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check_status_code():
    response = client.get("/api/v1/health")
    assert response.status_code == 200


def test_health_check_response_body():
    response = client.get("/api/v1/health")
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "pdfshield"
