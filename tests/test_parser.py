"""
Comprehensive tests for app/services/parser.py.

Coverage areas
--------------
1. Schema correctness  – returned types match Pydantic models.
2. Metadata extraction – all four keys populated with correct values and PDF
                         date format (D:...).
3. Structural data     – page count, text-layer flag, raw-text content.
4. Character blocks    – font name, font size, coordinate fields, bbox
                         consistency, and page-index bounds.
5. Anomaly signals     – Sejda metadata fingerprint, date mismatch, multiple
                         font families, font-mismatch on price digit.
6. Determinism         – two consecutive parses of the same file yield
                         bit-identical results.
7. File-descriptor safety
                       – fitz Document is closed (is_closed == True) after
                         every parse (uses mock spy on fitz.open).
                       – PdfReader exits its context manager (monkeypatches
                         PdfReader.__exit__ to count calls).
                       – pdfplumber PDF is closed via __exit__ (context-
                         manager spy on pdfplumber.open).
8. Error handling      – missing file, corrupt file, exception hierarchy.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch as mock_patch

import fitz
import pdfplumber
import pytest
from pypdf import PdfReader

from app.exceptions import PDFParseError, PDFShieldError
from app.models.schemas import PDFMetadata, PDFStructuralData, TextBlock
from app.services.parser import parse_pdf

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PDF_DATE_PREFIX = "D:"


def _result(fixture_path: Path) -> PDFStructuralData:
    return parse_pdf(fixture_path)


# ============================================================================
# 1. Schema correctness
# ============================================================================

def test_parse_returns_pdfstructuraldata(clean_pdf_path):
    assert isinstance(_result(clean_pdf_path), PDFStructuralData)


def test_metadata_field_is_pdfmetadata(clean_pdf_path):
    assert isinstance(_result(clean_pdf_path).metadata, PDFMetadata)


def test_blocks_are_all_textblock_instances(clean_pdf_path):
    assert all(isinstance(b, TextBlock) for b in _result(clean_pdf_path).blocks)


def test_page_count_is_int(clean_pdf_path):
    assert isinstance(_result(clean_pdf_path).page_count, int)


def test_has_text_layer_is_bool(clean_pdf_path):
    assert isinstance(_result(clean_pdf_path).has_text_layer, bool)


def test_raw_text_is_str(clean_pdf_path):
    assert isinstance(_result(clean_pdf_path).raw_text, str)


# ============================================================================
# 2. Metadata extraction — clean.pdf
# ============================================================================

def test_clean_metadata_creator_exact(clean_pdf_path):
    assert _result(clean_pdf_path).metadata.creator == "Microsoft Word 16.0"


def test_clean_metadata_producer_exact(clean_pdf_path):
    assert _result(clean_pdf_path).metadata.producer == "Microsoft Word 16.0"


def test_clean_metadata_creation_date_present(clean_pdf_path):
    assert _result(clean_pdf_path).metadata.creation_date is not None


def test_clean_metadata_mod_date_present(clean_pdf_path):
    assert _result(clean_pdf_path).metadata.mod_date is not None


def test_clean_metadata_creation_date_pdf_format(clean_pdf_path):
    """PDF dates must start with 'D:'."""
    cd = _result(clean_pdf_path).metadata.creation_date
    assert cd is not None and cd.startswith(PDF_DATE_PREFIX), f"Bad date format: {cd!r}"


def test_clean_metadata_mod_date_pdf_format(clean_pdf_path):
    md = _result(clean_pdf_path).metadata.mod_date
    assert md is not None and md.startswith(PDF_DATE_PREFIX), f"Bad date format: {md!r}"


def test_clean_metadata_creation_equals_mod_date(clean_pdf_path):
    meta = _result(clean_pdf_path).metadata
    assert meta.creation_date == meta.mod_date


# ============================================================================
# 3. Structural data — clean.pdf
# ============================================================================

def test_clean_page_count_is_one(clean_pdf_path):
    assert _result(clean_pdf_path).page_count == 1


def test_clean_has_text_layer_true(clean_pdf_path):
    assert _result(clean_pdf_path).has_text_layer is True


def test_clean_raw_text_contains_header(clean_pdf_path):
    raw = _result(clean_pdf_path).raw_text
    assert "ACME Corp" in raw


def test_clean_raw_text_contains_invoice_keyword(clean_pdf_path):
    raw = _result(clean_pdf_path).raw_text
    assert "Invoice" in raw


def test_clean_raw_text_contains_price(clean_pdf_path):
    raw = _result(clean_pdf_path).raw_text
    assert "550" in raw


# ============================================================================
# 4. Character blocks — font, size, coordinates, bbox
# ============================================================================

def test_clean_blocks_non_empty(clean_pdf_path):
    assert len(_result(clean_pdf_path).blocks) > 0


def test_block_font_name_is_nonempty_string(clean_pdf_path):
    for b in _result(clean_pdf_path).blocks:
        assert isinstance(b.font_name, str) and b.font_name, (
            f"Empty font_name on block: {b!r}"
        )


def test_block_font_size_is_positive(clean_pdf_path):
    for b in _result(clean_pdf_path).blocks:
        assert b.font_size > 0, f"Non-positive font_size on block: {b!r}"


def test_block_x_equals_bbox_x0(clean_pdf_path):
    for b in _result(clean_pdf_path).blocks:
        assert b.x == b.bbox[0], f"x ({b.x}) != bbox[0] ({b.bbox[0]})"


def test_block_y_equals_bbox_top(clean_pdf_path):
    for b in _result(clean_pdf_path).blocks:
        assert b.y == b.bbox[1], f"y ({b.y}) != bbox[1] ({b.bbox[1]})"


def test_block_bbox_has_four_elements(clean_pdf_path):
    for b in _result(clean_pdf_path).blocks:
        assert len(b.bbox) == 4


def test_block_bbox_x1_ge_x0(clean_pdf_path):
    for b in _result(clean_pdf_path).blocks:
        x0, _, x1, _ = b.bbox
        assert x1 >= x0, f"x1 ({x1}) < x0 ({x0}) in block {b!r}"


def test_block_bbox_bottom_ge_top(clean_pdf_path):
    for b in _result(clean_pdf_path).blocks:
        _, top, _, bottom = b.bbox
        assert bottom >= top, f"bottom ({bottom}) < top ({top}) in block {b!r}"


def test_block_coordinates_nonnegative(clean_pdf_path):
    for b in _result(clean_pdf_path).blocks:
        assert b.x >= 0 and b.y >= 0, f"Negative coordinate in block {b!r}"


def test_block_page_index_within_bounds(clean_pdf_path):
    result = _result(clean_pdf_path)
    for b in result.blocks:
        assert 0 <= b.page_index < result.page_count, (
            f"page_index {b.page_index} out of range [0, {result.page_count})"
        )


def test_clean_blocks_use_helvetica(clean_pdf_path):
    fonts = {b.font_name for b in _result(clean_pdf_path).blocks}
    assert any("Helv" in f or "helv" in f.lower() for f in fonts), (
        f"Helvetica not found in fonts: {fonts}"
    )


def test_clean_blocks_single_page_only(clean_pdf_path):
    page_indices = {b.page_index for b in _result(clean_pdf_path).blocks}
    assert page_indices == {0}


# ============================================================================
# 5. Anomaly signals — anomalous.pdf
# ============================================================================

def test_anomalous_metadata_creator_contains_sejda(anomalous_pdf_path):
    meta = _result(anomalous_pdf_path).metadata
    assert meta.creator is not None and "Sejda" in meta.creator


def test_anomalous_metadata_producer_contains_sejda(anomalous_pdf_path):
    assert "Sejda" in (_result(anomalous_pdf_path).metadata.producer or "")


def test_anomalous_metadata_mod_date_newer_than_creation(anomalous_pdf_path):
    meta = _result(anomalous_pdf_path).metadata
    # Dates are PDF strings "D:YYYYMMDDHHmmss..." — lexicographic comparison works
    assert meta.creation_date != meta.mod_date
    assert meta.mod_date > meta.creation_date  # type: ignore[operator]


def test_anomalous_has_text_layer(anomalous_pdf_path):
    assert _result(anomalous_pdf_path).has_text_layer is True


def test_anomalous_has_multiple_font_families(anomalous_pdf_path):
    fonts = {b.font_name for b in _result(anomalous_pdf_path).blocks}
    assert len(fonts) > 1, f"Expected >1 font families, got: {fonts}"


def test_anomalous_contains_times_roman_font(anomalous_pdf_path):
    fonts = {b.font_name for b in _result(anomalous_pdf_path).blocks}
    assert any("Times" in f or "tiro" in f.lower() for f in fonts), (
        f"Times-Roman not found in fonts: {fonts}"
    )


def test_anomalous_price_digit_has_mismatched_font(anomalous_pdf_path):
    """The anomalous '5' must appear in Times-Roman while the rest uses Helv."""
    blocks = _result(anomalous_pdf_path).blocks
    fives = [b for b in blocks if b.text == "5"]
    assert fives, "No '5' character blocks found"
    fonts_of_fives = {b.font_name for b in fives}
    assert any("Times" in f or "tiro" in f.lower() for f in fonts_of_fives), (
        f"No '5' found in Times-Roman; fonts seen: {fonts_of_fives}"
    )


# ============================================================================
# 6. Determinism — same file parsed twice yields identical results
# ============================================================================

def test_deterministic_metadata(clean_pdf_path):
    assert _result(clean_pdf_path).metadata == _result(clean_pdf_path).metadata


def test_deterministic_page_count(clean_pdf_path):
    assert _result(clean_pdf_path).page_count == _result(clean_pdf_path).page_count


def test_deterministic_block_count(clean_pdf_path):
    assert len(_result(clean_pdf_path).blocks) == len(_result(clean_pdf_path).blocks)


def test_deterministic_full_model_dump(clean_pdf_path):
    r1 = _result(clean_pdf_path).model_dump()
    r2 = _result(clean_pdf_path).model_dump()
    assert r1 == r2


def test_deterministic_anomalous_metadata(anomalous_pdf_path):
    m1 = _result(anomalous_pdf_path).metadata
    m2 = _result(anomalous_pdf_path).metadata
    assert m1 == m2


# ============================================================================
# 7. File-descriptor safety
# ============================================================================

def test_fitz_document_is_closed_after_parse(clean_pdf_path):
    """Every fitz.Document opened during parsing must have is_closed == True."""
    opened_docs: list[fitz.Document] = []
    real_fitz_open = fitz.open

    def spy_open(*args, **kwargs):
        doc = real_fitz_open(*args, **kwargs)
        opened_docs.append(doc)
        return doc

    with mock_patch("app.services.parser.fitz.open", side_effect=spy_open):
        parse_pdf(clean_pdf_path)

    assert opened_docs, "fitz.open was never called — extraction path changed?"
    unclosed = [d for d in opened_docs if not d.is_closed]
    assert not unclosed, f"{len(unclosed)} fitz Document(s) left open after parse"


def test_pypdf_reader_closed_via_context_manager(clean_pdf_path, monkeypatch):
    """PdfReader.__exit__ must be called (i.e. used as 'with PdfReader()')."""
    exit_calls: list[object] = []
    original_exit = PdfReader.__exit__

    def tracking_exit(self, *args):
        exit_calls.append(self)
        return original_exit(self, *args)

    monkeypatch.setattr(PdfReader, "__exit__", tracking_exit)
    parse_pdf(clean_pdf_path)

    assert exit_calls, "PdfReader.__exit__ was never called — reader not closed"


def test_pdfplumber_pdf_closed_via_context_manager(clean_pdf_path, monkeypatch):
    """pdfplumber PDF object must exit its context manager after extraction."""
    exit_calls: list[bool] = []
    real_open = pdfplumber.open

    class _TrackingPDF:
        def __init__(self, *args, **kwargs):
            self._pdf = real_open(*args, **kwargs)

        def __enter__(self):
            return self._pdf.__enter__()

        def __exit__(self, *args):
            exit_calls.append(True)
            return self._pdf.__exit__(*args)

    monkeypatch.setattr(pdfplumber, "open", _TrackingPDF)
    parse_pdf(clean_pdf_path)

    assert exit_calls, "pdfplumber PDF.__exit__ was never called — PDF not closed"


def test_all_handles_closed_anomalous(anomalous_pdf_path):
    """FD-safety test repeated for anomalous.pdf to cover the overlay path."""
    opened_docs: list[fitz.Document] = []
    real_open = fitz.open

    def spy_open(*args, **kwargs):
        doc = real_open(*args, **kwargs)
        opened_docs.append(doc)
        return doc

    with mock_patch("app.services.parser.fitz.open", side_effect=spy_open):
        parse_pdf(anomalous_pdf_path)

    unclosed = [d for d in opened_docs if not d.is_closed]
    assert not unclosed, f"{len(unclosed)} fitz Document(s) left open after parse"


# ============================================================================
# 8. Error handling
# ============================================================================

def test_missing_file_raises_parse_error():
    with pytest.raises(PDFParseError, match="File not found"):
        parse_pdf("/nonexistent/path/file.pdf")


def test_corrupt_bytes_raises_parse_error(tmp_path):
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a pdf at all \x00\x01\x02")
    with pytest.raises(PDFParseError):
        parse_pdf(corrupt)


def test_parse_error_is_pdfshield_error():
    with pytest.raises(PDFShieldError):
        parse_pdf("/nonexistent/path/file.pdf")


def test_parse_error_carries_filename(tmp_path):
    bad = tmp_path / "bad_file.pdf"
    bad.write_bytes(b"%bad")
    with pytest.raises(PDFParseError) as exc_info:
        parse_pdf(bad)
    assert "bad_file.pdf" in str(exc_info.value)


def test_parse_accepts_path_object(clean_pdf_path):
    assert isinstance(clean_pdf_path, Path)
    result = parse_pdf(clean_pdf_path)
    assert result.page_count == 1


def test_parse_accepts_string_path(clean_pdf_path):
    result = parse_pdf(str(clean_pdf_path))
    assert result.page_count == 1
