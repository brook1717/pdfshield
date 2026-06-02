"""
Session-scoped pytest fixtures for PDF fixture files.

The PDFs are generated once per test session into tests/fixtures/.
Individual tests receive Path objects they can open with any PDF library.
"""
from pathlib import Path

import pytest

from app.utils.generate_test_pdfs import FIXTURES_DIR, generate_all


@pytest.fixture(scope="session", autouse=True)
def pdf_fixtures() -> dict[str, Path]:
    """Generate clean.pdf and anomalous.pdf before the session runs."""
    return generate_all(FIXTURES_DIR)


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    """
    Disable the slowapi rate limiter for every test.

    Tests in ``test_security.py`` that specifically exercise rate-limiting
    behaviour re-enable the limiter locally inside the test function and reset
    the storage after they finish.
    """
    from app.middleware.rate_limit import limiter

    original = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = original


@pytest.fixture(scope="session")
def clean_pdf_path(pdf_fixtures: dict[str, Path]) -> Path:
    return pdf_fixtures["clean"]


@pytest.fixture(scope="session")
def anomalous_pdf_path(pdf_fixtures: dict[str, Path]) -> Path:
    return pdf_fixtures["anomalous"]
