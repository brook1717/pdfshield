"""Tests for app/services/parser.py."""
import pytest
from pathlib import Path

from app.exceptions import PDFParseError
from app.models.schemas import PDFStructuralData, PDFMetadata, TextBlock
from app.services.parser import parse_pdf


# ---------------------------------------------------------------------------
# Return type and schema
# ---------------------------------------------------------------------------

def test_parse_returns_structural_data(clean_pdf_path):
    result = parse_pdf(clean_pdf_path)
    assert isinstance(result, PDFStructuralData)


def test_metadata_is_pdf_metadata(clean_pdf_path):
    result = parse_pdf(clean_pdf_path)
    assert isinstance(result.metadata, PDFMetadata)


def test_blocks_are_text_blocks(clean_pdf_path):
    result = parse_pdf(clean_pdf_path)
    assert all(isinstance(b, TextBlock) for b in result.blocks)


# ---------------------------------------------------------------------------
# clean.pdf — expected values
# ---------------------------------------------------------------------------

def test_clean_page_count(clean_pdf_path):
    assert parse_pdf(clean_pdf_path).page_count == 1


def test_clean_has_text_layer(clean_pdf_path):
    assert parse_pdf(clean_pdf_path).has_text_layer is True


def test_clean_raw_text_contains_expected_content(clean_pdf_path):
    raw = parse_pdf(clean_pdf_path).raw_text
    assert "ACME Corp" in raw
    assert "Invoice" in raw


def test_clean_metadata_creator(clean_pdf_path):
    meta = parse_pdf(clean_pdf_path).metadata
    assert meta.creator == "Microsoft Word 16.0"


def test_clean_metadata_producer(clean_pdf_path):
    meta = parse_pdf(clean_pdf_path).metadata
    assert meta.producer == "Microsoft Word 16.0"


def test_clean_metadata_dates_present(clean_pdf_path):
    meta = parse_pdf(clean_pdf_path).metadata
    assert meta.creation_date is not None
    assert meta.mod_date is not None


def test_clean_blocks_non_empty(clean_pdf_path):
    assert len(parse_pdf(clean_pdf_path).blocks) > 0


def test_clean_blocks_have_helv_font(clean_pdf_path):
    fonts = {b.font_name for b in parse_pdf(clean_pdf_path).blocks}
    assert any("Helv" in f or "helv" in f.lower() for f in fonts)


def test_clean_block_fields_populated(clean_pdf_path):
    block = parse_pdf(clean_pdf_path).blocks[0]
    assert isinstance(block.text, str)
    assert isinstance(block.font_name, str)
    assert block.font_size > 0
    assert len(block.bbox) == 4
    assert block.page_index == 0


def test_clean_block_bbox_coordinates_valid(clean_pdf_path):
    for block in parse_pdf(clean_pdf_path).blocks:
        x0, top, x1, bottom = block.bbox
        assert x1 >= x0, f"x1 ({x1}) < x0 ({x0})"
        assert bottom >= top, f"bottom ({bottom}) < top ({top})"


# ---------------------------------------------------------------------------
# anomalous.pdf — forensic signal checks
# ---------------------------------------------------------------------------

def test_anomalous_metadata_creator_is_sejda(anomalous_pdf_path):
    meta = parse_pdf(anomalous_pdf_path).metadata
    assert meta.creator is not None
    assert "Sejda" in meta.creator


def test_anomalous_metadata_producer_is_sejda(anomalous_pdf_path):
    meta = parse_pdf(anomalous_pdf_path).metadata
    assert "Sejda" in (meta.producer or "")


def test_anomalous_metadata_mod_date_differs_from_creation(anomalous_pdf_path):
    meta = parse_pdf(anomalous_pdf_path).metadata
    assert meta.creation_date != meta.mod_date


def test_anomalous_has_text_layer(anomalous_pdf_path):
    assert parse_pdf(anomalous_pdf_path).has_text_layer is True


def test_anomalous_has_multiple_font_families(anomalous_pdf_path):
    fonts = {b.font_name for b in parse_pdf(anomalous_pdf_path).blocks}
    assert len(fonts) > 1, f"Expected >1 font families, got: {fonts}"


def test_anomalous_contains_alt_font(anomalous_pdf_path):
    fonts = {b.font_name for b in parse_pdf(anomalous_pdf_path).blocks}
    assert any("Times" in f or "tiro" in f.lower() for f in fonts)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_missing_file_raises_parse_error():
    with pytest.raises(PDFParseError, match="File not found"):
        parse_pdf("/nonexistent/path/file.pdf")


def test_corrupt_file_raises_parse_error(tmp_path):
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a pdf at all \x00\x01\x02")
    with pytest.raises(PDFParseError):
        parse_pdf(corrupt)


def test_parse_error_is_pdf_shield_error():
    from app.exceptions import PDFShieldError
    with pytest.raises(PDFShieldError):
        parse_pdf("/nonexistent/path/file.pdf")
