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


@pytest.fixture(scope="session")
def clean_pdf_path(pdf_fixtures: dict[str, Path]) -> Path:
    return pdf_fixtures["clean"]


@pytest.fixture(scope="session")
def anomalous_pdf_path(pdf_fixtures: dict[str, Path]) -> Path:
    return pdf_fixtures["anomalous"]
