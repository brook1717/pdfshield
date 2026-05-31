"""
PDF parsing service.

Extraction strategy:
    pypdf      → metadata dict (Creator, Producer, CreationDate, ModDate)
                 and page count.
    PyMuPDF    → has-text-layer flag and full raw text (fast getText).
    pdfplumber → character-level structural blocks with font name, font size,
                 and precise bounding-box coordinates.
"""
from __future__ import annotations

import logging
from pathlib import Path

import fitz  # PyMuPDF
import pdfplumber
from pypdf import PdfReader

from app.exceptions import PDFParseError
from app.models.schemas import PDFMetadata, PDFStructuralData, TextBlock

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_pdf(file_path: str | Path) -> PDFStructuralData:
    """
    Extract structural data from a PDF at *file_path*.

    Returns a fully populated :class:`PDFStructuralData` instance.

    Raises:
        PDFParseError: if the file is missing or any extraction step fails.
    """
    path = Path(file_path).resolve()

    if not path.is_file():
        raise PDFParseError(f"File not found: {path}")

    logger.info("Parsing PDF: %s", path.name)

    try:
        metadata, page_count = _pypdf_extract(path)
        has_text_layer, raw_text = _fitz_extract(path)
        blocks = _pdfplumber_extract(path)
    except PDFParseError:
        raise
    except Exception as exc:
        logger.exception("Unexpected error while parsing '%s'", path.name)
        raise PDFParseError(f"Failed to parse '{path.name}': {exc}") from exc

    logger.info(
        "Parsed '%s': %d page(s), %d block(s), text_layer=%s",
        path.name, page_count, len(blocks), has_text_layer,
    )

    return PDFStructuralData(
        metadata=metadata,
        page_count=page_count,
        has_text_layer=has_text_layer,
        raw_text=raw_text,
        blocks=blocks,
    )


# ---------------------------------------------------------------------------
# Private extraction helpers
# ---------------------------------------------------------------------------

def _pypdf_extract(path: Path) -> tuple[PDFMetadata, int]:
    """Use pypdf to read metadata and page count."""
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise PDFParseError(f"pypdf could not open '{path.name}': {exc}") from exc

    meta = reader.metadata or {}
    page_count = len(reader.pages)

    metadata = PDFMetadata(
        creator=_safe_str(meta.get("/Creator")),
        producer=_safe_str(meta.get("/Producer")),
        creation_date=_safe_str(meta.get("/CreationDate")),
        mod_date=_safe_str(meta.get("/ModDate")),
    )

    return metadata, page_count


def _fitz_extract(path: Path) -> tuple[bool, str]:
    """
    Use PyMuPDF to determine text-layer presence and extract raw text.

    A page is considered to have a text layer if it contains at least one
    non-whitespace character in the embedded text stream.
    """
    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        raise PDFParseError(f"PyMuPDF could not open '{path.name}': {exc}") from exc

    has_text = False
    page_texts: list[str] = []

    try:
        for page in doc:
            text = page.get_text("text")
            page_texts.append(text)
            if not has_text and text.strip():
                has_text = True
    finally:
        doc.close()

    return has_text, "\f".join(page_texts)  # form-feed as page separator


def _pdfplumber_extract(path: Path) -> list[TextBlock]:
    """
    Use pdfplumber to extract character-level structural blocks.

    Each character in the PDF becomes one :class:`TextBlock` carrying its
    font name, font size, position (x0, top), full bounding box, and page index.
    """
    blocks: list[TextBlock] = []

    try:
        with pdfplumber.open(str(path)) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                for char in page.chars or []:
                    try:
                        block = TextBlock(
                            text=char.get("text", ""),
                            font_name=char.get("fontname", ""),
                            font_size=float(char.get("size") or 0.0),
                            x=float(char.get("x0") or 0.0),
                            y=float(char.get("top") or 0.0),
                            bbox=(
                                float(char.get("x0") or 0.0),
                                float(char.get("top") or 0.0),
                                float(char.get("x1") or 0.0),
                                float(char.get("bottom") or 0.0),
                            ),
                            page_index=page_idx,
                        )
                        blocks.append(block)
                    except Exception:
                        logger.debug(
                            "Skipping malformed char on page %d: %r",
                            page_idx, char,
                        )
    except Exception as exc:
        raise PDFParseError(
            f"pdfplumber could not extract blocks from '{path.name}': {exc}"
        ) from exc

    return blocks


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _safe_str(value: object) -> str | None:
    """Return a stripped string or None for empty/None values."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None
