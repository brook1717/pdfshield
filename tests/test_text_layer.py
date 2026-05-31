"""
Unit tests for app/services/text_layer.py — analyze_text_layer().

All PDFStructuralData objects are constructed in-process from simulated maps;
no real PDF files are opened except in the two end-to-end integration tests.

Test matrix
-----------
A. Return-type / schema guarantees
B. Zero-page document              → info, no details
C. Primary danger checks (any one of the three sub-conditions)
   C1. has_text_layer = False
   C2. raw_text stripped length < MIN_TEXT_CHARS
   C3. block_count = 0  (with valid has_text_layer and sufficient raw_text)
   C4. Multiple sub-conditions fire simultaneously → single detail line
   C5. Detail string contains canonical phrase
   C6. Detail string names the failing sub-conditions
D. Boundary values around MIN_TEXT_CHARS
   D1. raw_len == MIN_TEXT_CHARS - 1  → danger
   D2. raw_len == MIN_TEXT_CHARS      → NOT triggered by text-length check
   D3. raw_text all-whitespace        → stripped length = 0 → danger
E. Sparsity warning (primary passes, density < MIN_BLOCKS_PER_PAGE)
   E1. 1 block / 1 page              → warning
   E2. 4 blocks / 1 page             → warning  (< 5.0)
   E3. 5 blocks / 1 page             → info     (== threshold)
   E4. 4 blocks / 2 pages (2.0/p)    → warning
   E5. 10 blocks / 2 pages (5.0/p)   → info
   E6. 9 blocks / 2 pages (4.5/p)    → warning
   E7. Detail string mentions blocks-per-page figures
F. Healthy document → info, no details
   F1. Single page with many blocks
   F2. Multi-page with many blocks
   F3. Integration: clean.pdf
   F4. Integration: anomalous.pdf  (real text, so should be info too)
G. Severity escalation / check name invariants
"""
from __future__ import annotations

import pytest

from app.models.schemas import Finding, PDFMetadata, PDFStructuralData, TextBlock
from app.services.text_layer import (
    MIN_BLOCKS_PER_PAGE,
    MIN_TEXT_CHARS,
    analyze_text_layer,
)

# ---------------------------------------------------------------------------
# Shared fixtures / factories
# ---------------------------------------------------------------------------

_EMPTY_META = PDFMetadata()
_LONG_TEXT = "Hello, world! " * 50  # 700 chars — well above MIN_TEXT_CHARS


def _block(text: str = "A", page_index: int = 0) -> TextBlock:
    return TextBlock(
        text=text,
        font_name="Helvetica",
        font_size=12.0,
        x=10.0,
        y=10.0,
        bbox=(10.0, 10.0, 20.0, 22.0),
        page_index=page_index,
    )


def _blocks(n: int, pages: int = 1) -> list[TextBlock]:
    """Return *n* TextBlocks spread evenly across *pages* pages."""
    return [_block(page_index=i % pages) for i in range(n)]


def _sd(
    *,
    page_count: int = 1,
    has_text_layer: bool = True,
    raw_text: str = _LONG_TEXT,
    blocks: list[TextBlock] | None = None,
) -> PDFStructuralData:
    """Build a simulated PDFStructuralData without touching any real PDF."""
    return PDFStructuralData(
        metadata=_EMPTY_META,
        page_count=page_count,
        has_text_layer=has_text_layer,
        raw_text=raw_text,
        blocks=blocks if blocks is not None else _blocks(50),
    )


def _healthy(page_count: int = 1) -> PDFStructuralData:
    """Healthy document: many blocks, long text, text layer present."""
    n = int(MIN_BLOCKS_PER_PAGE * page_count) + 10
    return _sd(page_count=page_count, blocks=_blocks(n, pages=page_count))


# ---------------------------------------------------------------------------
# A. Return-type / schema guarantees
# ---------------------------------------------------------------------------

def test_returns_finding():
    assert isinstance(analyze_text_layer(_healthy()), Finding)


def test_check_name_is_text_layer_analysis():
    assert analyze_text_layer(_healthy()).check == "text_layer_analysis"


def test_status_is_valid_severity():
    f = analyze_text_layer(_healthy())
    assert f.status in ("info", "warning", "danger")


def test_details_is_list_of_strings():
    f = analyze_text_layer(_sd(has_text_layer=False, raw_text="", blocks=[]))
    assert isinstance(f.details, list)
    assert all(isinstance(d, str) for d in f.details)


# ---------------------------------------------------------------------------
# B. Zero-page document
# ---------------------------------------------------------------------------

def test_zero_pages_returns_info():
    assert analyze_text_layer(_sd(page_count=0)).status == "info"


def test_zero_pages_no_details():
    assert analyze_text_layer(_sd(page_count=0)).details == []


# ---------------------------------------------------------------------------
# C1. Primary danger: has_text_layer = False
# ---------------------------------------------------------------------------

def test_no_text_layer_flag_is_danger():
    f = analyze_text_layer(_sd(has_text_layer=False, raw_text="", blocks=[]))
    assert f.status == "danger"


def test_no_text_layer_flag_with_pages_is_danger():
    f = analyze_text_layer(_sd(page_count=3, has_text_layer=False, raw_text="", blocks=[]))
    assert f.status == "danger"


def test_no_text_layer_detail_mentions_no_stream():
    f = analyze_text_layer(_sd(has_text_layer=False, raw_text="", blocks=[]))
    assert any("no embedded text stream" in d for d in f.details)


# ---------------------------------------------------------------------------
# C2. Primary danger: raw_text stripped length < MIN_TEXT_CHARS
# ---------------------------------------------------------------------------

def test_short_raw_text_is_danger():
    short = "A" * (MIN_TEXT_CHARS - 1)   # 9 chars
    f = analyze_text_layer(_sd(raw_text=short, blocks=_blocks(10)))
    assert f.status == "danger"


def test_short_raw_text_detail_mentions_character_count():
    short = "Hi"   # 2 chars
    f = analyze_text_layer(_sd(raw_text=short, blocks=_blocks(10)))
    detail_text = " ".join(f.details)
    assert "2" in detail_text or "extractable" in detail_text


# ---------------------------------------------------------------------------
# C3. Primary danger: blocks = [] (with has_text_layer=True, long raw_text)
# ---------------------------------------------------------------------------

def test_no_blocks_with_valid_text_layer_is_danger():
    f = analyze_text_layer(_sd(has_text_layer=True, raw_text=_LONG_TEXT, blocks=[]))
    assert f.status == "danger"


def test_no_blocks_detail_mentions_rendering_elements():
    f = analyze_text_layer(_sd(has_text_layer=True, raw_text=_LONG_TEXT, blocks=[]))
    assert any("rendering" in d or "character" in d.lower() for d in f.details)


# ---------------------------------------------------------------------------
# C4. Multiple sub-conditions fire simultaneously
# ---------------------------------------------------------------------------

def test_all_three_sub_conditions_fail_one_danger_detail():
    f = analyze_text_layer(_sd(has_text_layer=False, raw_text="", blocks=[]))
    assert len(f.details) == 1   # aggregated into one line


def test_no_text_layer_and_no_blocks_both_mentioned():
    f = analyze_text_layer(_sd(has_text_layer=False, raw_text="", blocks=[]))
    detail = f.details[0]
    assert "no embedded text stream" in detail
    assert "rendering" in detail or "character" in detail.lower()


# ---------------------------------------------------------------------------
# C5. Canonical phrase in detail string
# ---------------------------------------------------------------------------

def test_danger_detail_contains_canonical_phrase():
    f = analyze_text_layer(_sd(has_text_layer=False, raw_text="", blocks=[]))
    assert any(
        "No selectable text layer detected" in d and
        ("Flattened" in d or "Image-only" in d)
        for d in f.details
    )


@pytest.mark.parametrize("has_tl,raw,blks", [
    (False, "", []),
    (True, "tiny", []),
    (True, _LONG_TEXT, []),
])
def test_canonical_phrase_in_all_primary_failure_modes(has_tl, raw, blks):
    f = analyze_text_layer(_sd(has_text_layer=has_tl, raw_text=raw, blocks=blks))
    assert f.status == "danger"
    assert any("No selectable text layer" in d for d in f.details)


# ---------------------------------------------------------------------------
# C6. Detail string references the failing sub-condition(s)
# ---------------------------------------------------------------------------

def test_detail_references_stream_when_no_text_layer():
    f = analyze_text_layer(_sd(has_text_layer=False, raw_text="", blocks=[]))
    assert "no embedded text stream" in f.details[0]


def test_detail_references_char_count_when_raw_text_short():
    raw = "Abc"  # 3 chars, has_text_layer=True
    f = analyze_text_layer(_sd(
        has_text_layer=True,
        raw_text=raw,
        blocks=_blocks(20),    # enough blocks — only raw_text check fails
    ))
    detail = f.details[0]
    assert "3" in detail or "extractable" in detail


# ---------------------------------------------------------------------------
# D. Boundary values around MIN_TEXT_CHARS
# ---------------------------------------------------------------------------

def test_one_below_min_chars_triggers_danger():
    raw = "X" * (MIN_TEXT_CHARS - 1)
    f = analyze_text_layer(_sd(raw_text=raw, blocks=_blocks(20)))
    assert f.status == "danger"


def test_exactly_min_chars_does_not_trigger_text_length_danger():
    """raw_len == MIN_TEXT_CHARS must NOT fail the text-length sub-check."""
    raw = "X" * MIN_TEXT_CHARS
    f = analyze_text_layer(_sd(raw_text=raw, blocks=_blocks(20)))
    # text-length sub-check passes; the result depends only on sparsity
    assert "only" not in " ".join(f.details)


def test_all_whitespace_raw_text_triggers_danger():
    """Whitespace-only raw_text strips to 0 chars → danger."""
    raw = " " * 100
    f = analyze_text_layer(_sd(raw_text=raw, blocks=_blocks(20)))
    assert f.status == "danger"


def test_unicode_text_counted_correctly():
    """Unicode chars must be counted after strip(), not truncated."""
    raw = "日本語テスト！！！！！"  # 11 Unicode chars
    f = analyze_text_layer(_sd(raw_text=raw, blocks=_blocks(20)))
    assert "only" not in " ".join(f.details)   # passes text-length check


# ---------------------------------------------------------------------------
# E. Sparsity warning
# ---------------------------------------------------------------------------

def test_one_block_one_page_is_warning():
    f = analyze_text_layer(_sd(blocks=_blocks(1, pages=1)))
    assert f.status == "warning"


def test_four_blocks_one_page_is_warning():
    f = analyze_text_layer(_sd(blocks=_blocks(4, pages=1)))
    assert f.status == "warning"


def test_five_blocks_one_page_is_info():
    """density == MIN_BLOCKS_PER_PAGE is NOT sparse."""
    n = int(MIN_BLOCKS_PER_PAGE)
    f = analyze_text_layer(_sd(blocks=_blocks(n, pages=1)))
    assert f.status == "info"


def test_six_blocks_one_page_is_info():
    f = analyze_text_layer(_sd(blocks=_blocks(6, pages=1)))
    assert f.status == "info"


def test_four_blocks_two_pages_is_warning():
    """4 blocks / 2 pages = 2.0/page < 5 → warning."""
    f = analyze_text_layer(_sd(page_count=2, blocks=_blocks(4, pages=2)))
    assert f.status == "warning"


def test_ten_blocks_two_pages_is_info():
    """10 blocks / 2 pages = 5.0/page ≥ 5 → info."""
    f = analyze_text_layer(_sd(page_count=2, blocks=_blocks(10, pages=2)))
    assert f.status == "info"


def test_nine_blocks_two_pages_is_warning():
    """9 blocks / 2 pages = 4.5/page < 5 → warning."""
    f = analyze_text_layer(_sd(page_count=2, blocks=_blocks(9, pages=2)))
    assert f.status == "warning"


def test_sparsity_detail_mentions_block_count_and_pages():
    f = analyze_text_layer(_sd(page_count=2, blocks=_blocks(4, pages=2)))
    detail = f.details[0]
    assert "4" in detail
    assert "2" in detail


def test_sparsity_detail_mentions_partial_flattening():
    f = analyze_text_layer(_sd(blocks=_blocks(1)))
    detail = f.details[0]
    assert "flatten" in detail.lower() or "sparse" in detail.lower()


def test_no_detail_when_healthy_density():
    f = analyze_text_layer(_healthy())
    assert f.details == []


# ---------------------------------------------------------------------------
# F. Healthy document → info, no details
# ---------------------------------------------------------------------------

def test_healthy_single_page_is_info():
    assert analyze_text_layer(_healthy(1)).status == "info"


def test_healthy_multi_page_is_info():
    assert analyze_text_layer(_healthy(5)).status == "info"


def test_healthy_has_no_details():
    assert analyze_text_layer(_healthy()).details == []


def test_healthy_check_name_unchanged():
    assert analyze_text_layer(_healthy()).check == "text_layer_analysis"


def test_integration_clean_pdf_is_info(clean_pdf_path):
    """clean.pdf has a rich text layer — must produce no warning/danger."""
    from app.services.parser import parse_pdf
    f = analyze_text_layer(parse_pdf(clean_pdf_path))
    assert f.status == "info"
    assert f.details == []


def test_integration_anomalous_pdf_is_info(anomalous_pdf_path):
    """anomalous.pdf has real text (just a font mismatch) — text layer is healthy."""
    from app.services.parser import parse_pdf
    f = analyze_text_layer(parse_pdf(anomalous_pdf_path))
    assert f.status == "info"


# ---------------------------------------------------------------------------
# G. Severity escalation / invariants
# ---------------------------------------------------------------------------

def test_danger_not_downgraded_to_warning():
    """A primary-check failure must never produce a status below danger."""
    f = analyze_text_layer(_sd(has_text_layer=False, raw_text="", blocks=[]))
    assert f.status == "danger"


def test_single_primary_detail_line():
    """Primary failure must produce exactly one consolidated detail line."""
    f = analyze_text_layer(_sd(has_text_layer=False, raw_text="", blocks=[]))
    assert len(f.details) == 1


def test_single_sparsity_detail_line():
    f = analyze_text_layer(_sd(blocks=_blocks(1)))
    assert len(f.details) == 1


def test_check_name_stable_across_all_statuses():
    for sd in (
        _sd(has_text_layer=False, raw_text="", blocks=[]),   # danger
        _sd(blocks=_blocks(1)),                              # warning
        _healthy(),                                          # info
        _sd(page_count=0),                                   # zero-page
    ):
        assert analyze_text_layer(sd).check == "text_layer_analysis"
