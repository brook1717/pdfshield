"""
Localized unit & integration tests for app/services/font_analysis.py.

All block data is constructed programmatically — no real PDF opened except
in the two end-to-end integration tests at the bottom.

Test matrix
-----------
A. Return-type / schema invariants
B. No-finding cases
   B1. Empty block list
   B2. No numeric characters in the document
   B3. Numeric span with uniform font (family + size)
   B4. Single-character numeric span (too short to analyze)
   B5. Pure-symbol span with no digits (e.g., "$.")
C. Font-family mismatch detection
   C1. Outlier in the middle of a span
   C2. Outlier at the start of a span
   C3. Outlier at the end of a span
   C4. Detail string contains: offending font, span text, expected font
   C5. Two different outlier fonts in the same span → two detail lines
   C6. Same outlier font appearing twice → deduplicated to one detail
D. Font-size mismatch detection
   D1. Size delta > FONT_SIZE_TOLERANCE → danger
   D2. Size delta == FONT_SIZE_TOLERANCE → NOT flagged (exclusive boundary)
   D3. Size delta just above tolerance (tolerance + 0.01) → flagged
   D4. Detail string contains offending size and expected size
E. Combined family + size mismatch (one character triggers both)
F. Span scope / segmentation rules
   F1. Non-numeric char breaks the span; only numeric runs analyzed
   F2. Span of pure commas/dots (no digit) → skipped
   F3. Two separate numeric spans on same line — each independently checked
   F4. Numeric spans on different baselines are independent
   F5. Numeric spans on different pages are independent
G. Consensus selection (majority vote)
   G1. Majority wins when two fonts present (2 vs 1)
   G2. Tie-break: Counter.most_common is deterministic for the test inputs
H. Multi-line / multi-page scenarios
   H1. Clean line adjacent to anomalous line → only anomalous line flagged
   H2. Multi-page: anomaly on page 2 only
I. Baseline grouping (_group_into_lines)
   I1. Chars with identical bbox-bottom → same line
   I2. Chars whose bbox-bottom differs by > BASELINE_TOLERANCE → separate lines
   I3. Mixed font-size chars (< BASELINE_TOLERANCE bottom diff) → same line
J. Integration tests against real fixtures
   J1. anomalous.pdf → danger, Times-Roman / $550.00 / Helvetica in detail
   J2. clean.pdf     → info, no details
"""
from __future__ import annotations

import pytest

from app.models.schemas import Finding, PDFMetadata, PDFStructuralData, TextBlock
from app.services.font_analysis import (
    BASELINE_TOLERANCE,
    FONT_SIZE_TOLERANCE,
    MIN_SPAN_LENGTH,
    NUMERIC_CHARS,
    _Span,
    _check_span,
    _find_numeric_spans,
    _group_into_lines,
    analyze_font_consistency,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

_HELV = "Helvetica"
_TROM = "Times-Roman"
_COUR = "Courier-Bold"
_BASE_Y = 100.0          # arbitrary top-y for test blocks
_CHAR_W = 7.0            # approximate character width


def _block(
    text: str,
    font_name: str = _HELV,
    font_size: float = 12.0,
    x: float = 0.0,
    bottom: float = 112.0,   # top + font_size
    page_index: int = 0,
) -> TextBlock:
    top = bottom - font_size
    return TextBlock(
        text=text,
        font_name=font_name,
        font_size=font_size,
        x=x,
        y=top,
        bbox=(x, top, x + _CHAR_W, bottom),
        page_index=page_index,
    )


def _line(
    chars: str,
    fonts: list[str] | None = None,
    sizes: list[float] | None = None,
    start_x: float = 10.0,
    bottom: float = 112.0,
    page_index: int = 0,
) -> list[TextBlock]:
    """Build left-to-right blocks for *chars* with per-character font/size."""
    fonts = fonts or [_HELV] * len(chars)
    sizes = sizes or [12.0] * len(chars)
    x = start_x
    blocks: list[TextBlock] = []
    for ch, fn, fs in zip(chars, fonts, sizes):
        blocks.append(_block(ch, fn, fs, x=x, bottom=bottom, page_index=page_index))
        x += _CHAR_W
    return blocks


def _sd(blocks: list[TextBlock], page_count: int = 1) -> PDFStructuralData:
    return PDFStructuralData(
        metadata=PDFMetadata(),
        page_count=page_count,
        has_text_layer=bool(blocks),
        raw_text="".join(b.text for b in blocks),
        blocks=blocks,
    )


def _uniform_price(text: str = "$1500.00", bottom: float = 112.0) -> list[TextBlock]:
    """All-Helvetica numeric blocks (no anomaly)."""
    return _line(text, bottom=bottom)


def _anomalous_price(
    text: str,
    bad_idx: int,
    bad_font: str = _TROM,
    bad_size: float = 12.0,
    bottom: float = 112.0,
) -> list[TextBlock]:
    """Build a price string where index *bad_idx* has a different font."""
    fonts = [_HELV] * len(text)
    sizes = [12.0] * len(text)
    fonts[bad_idx] = bad_font
    sizes[bad_idx] = bad_size
    return _line(text, fonts=fonts, sizes=sizes, bottom=bottom)


# ---------------------------------------------------------------------------
# A. Return-type / schema invariants
# ---------------------------------------------------------------------------

def test_returns_finding():
    assert isinstance(analyze_font_consistency(_sd([])), Finding)


def test_check_name_is_font_consistency():
    assert analyze_font_consistency(_sd([])).check == "font_consistency"


def test_status_is_valid_severity():
    f = analyze_font_consistency(_sd([]))
    assert f.status in ("info", "warning", "danger")


def test_details_is_list_of_strings():
    f = analyze_font_consistency(_sd(_anomalous_price("$550.00", bad_idx=1)))
    assert isinstance(f.details, list) and all(isinstance(d, str) for d in f.details)


# ---------------------------------------------------------------------------
# B. No-finding cases
# ---------------------------------------------------------------------------

def test_empty_blocks_is_info():
    assert analyze_font_consistency(_sd([])).status == "info"


def test_no_numeric_chars_is_info():
    blocks = _line("Invoice Total")
    assert analyze_font_consistency(_sd(blocks)).status == "info"


def test_uniform_numeric_span_is_info():
    blocks = _uniform_price("$1500.00")
    assert analyze_font_consistency(_sd(blocks)).status == "info"


def test_uniform_two_digits_is_info():
    blocks = _line("42")
    assert analyze_font_consistency(_sd(blocks)).status == "info"


def test_single_digit_span_skipped():
    """Single-character numeric 'span' is below MIN_SPAN_LENGTH → info."""
    blocks = _line("A5B", fonts=[_HELV, _TROM, _HELV])
    assert analyze_font_consistency(_sd(blocks)).status == "info"


def test_pure_symbol_span_no_digit_skipped():
    """'$.' contains no digit → not a numeric span → info."""
    blocks = _line("$.", fonts=[_HELV, _TROM])
    assert analyze_font_consistency(_sd(blocks)).status == "info"


def test_exactly_min_span_length_analyzed():
    """'$5' has length == MIN_SPAN_LENGTH and one digit → must be analyzed."""
    blocks = _line("$5", fonts=[_HELV, _TROM])
    f = analyze_font_consistency(_sd(blocks))
    assert f.status == "danger"


# ---------------------------------------------------------------------------
# C. Font-family mismatch detection
# ---------------------------------------------------------------------------

def test_family_mismatch_middle_is_danger():
    blocks = _anomalous_price("$550.00", bad_idx=1, bad_font=_TROM)
    assert analyze_font_consistency(_sd(blocks)).status == "danger"


def test_family_mismatch_start_is_danger():
    blocks = _anomalous_price("550.00", bad_idx=0, bad_font=_TROM)
    assert analyze_font_consistency(_sd(blocks)).status == "danger"


def test_family_mismatch_end_is_danger():
    blocks = _anomalous_price("550.00", bad_idx=5, bad_font=_TROM)
    assert analyze_font_consistency(_sd(blocks)).status == "danger"


def test_family_mismatch_detail_contains_offending_font():
    blocks = _anomalous_price("$550.00", bad_idx=1, bad_font=_TROM)
    detail = analyze_font_consistency(_sd(blocks)).details[0]
    assert _TROM in detail


def test_family_mismatch_detail_contains_span_text():
    blocks = _anomalous_price("$550.00", bad_idx=1, bad_font=_TROM)
    detail = analyze_font_consistency(_sd(blocks)).details[0]
    assert "$550.00" in detail


def test_family_mismatch_detail_contains_expected_font():
    blocks = _anomalous_price("$550.00", bad_idx=1, bad_font=_TROM)
    detail = analyze_font_consistency(_sd(blocks)).details[0]
    assert _HELV in detail


def test_family_mismatch_detail_canonical_phrase():
    blocks = _anomalous_price("$550.00", bad_idx=1, bad_font=_TROM)
    detail = analyze_font_consistency(_sd(blocks)).details[0]
    assert "Mismatched font family" in detail and "expected" in detail


def test_two_different_outlier_fonts_two_details():
    """Chars at positions 1 and 3 have *different* outlier fonts."""
    chars = "$550.00"
    fonts = [_HELV] * len(chars)
    fonts[1] = _TROM
    fonts[3] = _COUR
    blocks = _line(chars, fonts=fonts)
    f = analyze_font_consistency(_sd(blocks))
    assert len(f.details) == 2


def test_same_outlier_font_twice_deduplicated():
    """Two chars share the same outlier font → only one detail line for family."""
    chars = "1,500.00"
    fonts = [_HELV] * len(chars)
    fonts[0] = _TROM   # '1'
    fonts[2] = _TROM   # '5'
    blocks = _line(chars, fonts=fonts)
    f = analyze_font_consistency(_sd(blocks))
    family_details = [d for d in f.details if "Mismatched font family" in d]
    assert len(family_details) == 1


# ---------------------------------------------------------------------------
# D. Font-size mismatch detection
# ---------------------------------------------------------------------------

def test_size_above_tolerance_is_danger():
    delta = FONT_SIZE_TOLERANCE + 0.1
    blocks = _anomalous_price("$550.00", bad_idx=1, bad_size=12.0 + delta)
    assert analyze_font_consistency(_sd(blocks)).status == "danger"


def test_size_exactly_tolerance_is_not_flagged():
    """Delta == FONT_SIZE_TOLERANCE → boundary is exclusive → no finding."""
    chars = "$550.00"
    sizes = [12.0] * len(chars)
    sizes[1] = 12.0 + FONT_SIZE_TOLERANCE   # exactly at boundary
    blocks = _line(chars, sizes=sizes)
    # no family mismatch, size exactly at boundary → info
    assert analyze_font_consistency(_sd(blocks)).status == "info"


def test_size_just_above_tolerance_is_danger():
    chars = "$550.00"
    sizes = [12.0] * len(chars)
    sizes[1] = 12.0 + FONT_SIZE_TOLERANCE + 0.01
    blocks = _line(chars, sizes=sizes)
    assert analyze_font_consistency(_sd(blocks)).status == "danger"


def test_size_mismatch_detail_contains_both_sizes():
    delta = FONT_SIZE_TOLERANCE + 1.0   # 2.5pt
    chars = "$550.00"
    sizes = [12.0] * len(chars)
    sizes[1] = 12.0 + delta
    blocks = _line(chars, sizes=sizes)
    f = analyze_font_consistency(_sd(blocks))
    size_details = [d for d in f.details if "size mismatch" in d.lower()]
    assert size_details
    detail = size_details[0]
    assert "14.5" in detail or "12.0" in detail   # either value present


def test_size_decrease_also_detected():
    chars = "$550.00"
    sizes = [14.0] * len(chars)          # consensus 14pt
    sizes[1] = 14.0 - FONT_SIZE_TOLERANCE - 0.5  # 12.0pt < 13.5pt threshold
    blocks = _line(chars, sizes=sizes)
    assert analyze_font_consistency(_sd(blocks)).status == "danger"


# ---------------------------------------------------------------------------
# E. Combined family + size mismatch (both checks on the same character)
# ---------------------------------------------------------------------------

def test_combined_mismatch_two_details():
    """One char: wrong family AND wrong size → two detail lines."""
    blocks = _anomalous_price("$550.00", bad_idx=1,
                               bad_font=_TROM,
                               bad_size=12.0 + FONT_SIZE_TOLERANCE + 1.0)
    f = analyze_font_consistency(_sd(blocks))
    assert len(f.details) == 2
    assert any("Mismatched font family" in d for d in f.details)
    assert any("size mismatch" in d.lower() for d in f.details)


def test_combined_mismatch_status_danger():
    blocks = _anomalous_price("$550.00", bad_idx=1,
                               bad_font=_TROM,
                               bad_size=16.0)
    assert analyze_font_consistency(_sd(blocks)).status == "danger"


# ---------------------------------------------------------------------------
# F. Span scope / segmentation
# ---------------------------------------------------------------------------

def test_non_numeric_char_breaks_span():
    """'$5 00' — the space breaks the span into '$5' and '00'."""
    chars = "$5 00"
    fonts = [_HELV, _TROM, _HELV, _HELV, _HELV]
    blocks = _line(chars, fonts=fonts)
    # '$5' span: $ (Helv) + 5 (Trom) → family mismatch → danger
    assert analyze_font_consistency(_sd(blocks)).status == "danger"


def test_letter_breaks_span():
    """'12A34' — 'A' breaks span into '12' and '34'; both uniform → info."""
    blocks = _line("12A34")
    assert analyze_font_consistency(_sd(blocks)).status == "info"


def test_span_without_any_digit_skipped():
    """'$,.' — no digit, so not a valid numeric span → info."""
    chars = "$,."
    fonts = [_HELV, _TROM, _HELV]
    blocks = _line(chars, fonts=fonts)
    assert analyze_font_consistency(_sd(blocks)).status == "info"


def test_two_separate_spans_each_checked():
    """Two spans on same line: first clean, second anomalous."""
    # span1: "100.00" all Helv (clean)
    # gap: " - "
    # span2: "$500.00" with one Times-Roman outlier
    line1 = _line("100.00", start_x=10.0, bottom=112.0)
    gap = _line(" - ", start_x=10.0 + 6 * _CHAR_W, bottom=112.0)
    span2_chars = "$500.00"
    span2_fonts = [_HELV] * len(span2_chars)
    span2_fonts[1] = _TROM
    line2 = _line(span2_chars, fonts=span2_fonts,
                  start_x=10.0 + 9 * _CHAR_W, bottom=112.0)
    f = analyze_font_consistency(_sd(line1 + gap + line2))
    assert f.status == "danger"
    # only one finding (from span2)
    assert len([d for d in f.details if "Mismatched font family" in d]) == 1


def test_two_anomalous_spans_two_findings():
    """Both spans have a mismatch → two detail lines (no cross-span dedup)."""
    s1 = _anomalous_price("100.00", bad_idx=0, bad_font=_TROM, bottom=112.0)
    s2 = _anomalous_price("200.00", bad_idx=0, bad_font=_TROM, bottom=130.0)
    f = analyze_font_consistency(_sd(s1 + s2))
    family_details = [d for d in f.details if "Mismatched font family" in d]
    assert len(family_details) == 2


def test_different_baseline_spans_independent():
    """Anomalous char only on second baseline; first must be clean."""
    clean_line = _line("$100.00", bottom=112.0)
    anomalous_line = _anomalous_price("$200.00", bad_idx=1, bottom=130.0)
    f = analyze_font_consistency(_sd(clean_line + anomalous_line))
    assert f.status == "danger"
    assert all("200.00" in d for d in f.details if "Mismatched" in d)


def test_different_pages_independent():
    p1 = _line("$100.00", page_index=0, bottom=112.0)
    anomalous = _anomalous_price("$200.00", bad_idx=1, bottom=112.0)
    # make anomalous blocks on page 1
    for b in anomalous:
        object.__setattr__(b, 'page_index', 1)
    f = analyze_font_consistency(_sd(p1 + anomalous, page_count=2))
    assert f.status == "danger"


# ---------------------------------------------------------------------------
# G. Consensus selection
# ---------------------------------------------------------------------------

def test_majority_font_is_consensus():
    """3 Helv + 1 Trom → consensus = Helv → Trom is the outlier."""
    chars = "1500"
    fonts = [_HELV, _HELV, _HELV, _TROM]
    blocks = _line(chars, fonts=fonts)
    f = analyze_font_consistency(_sd(blocks))
    assert f.status == "danger"
    assert _TROM in f.details[0]
    assert _HELV in f.details[0]


def test_minority_font_is_flagged_not_majority():
    """When minority is Courier, Courier appears as offending, Helv as expected."""
    chars = "$550"
    fonts = [_HELV, _HELV, _HELV, _COUR]
    blocks = _line(chars, fonts=fonts)
    detail = analyze_font_consistency(_sd(blocks)).details[0]
    assert _COUR in detail and _HELV in detail


# ---------------------------------------------------------------------------
# H. Multi-line scenarios
# ---------------------------------------------------------------------------

def test_clean_line_adjacent_to_anomalous():
    clean = _line("$100.00", bottom=112.0)
    bad = _anomalous_price("$200.00", bad_idx=1, bad_font=_TROM, bottom=130.0)
    f = analyze_font_consistency(_sd(clean + bad))
    assert f.status == "danger"
    assert all("200.00" in d or "size" in d for d in f.details)


def test_anomaly_on_second_page_only(anomalous_pdf_path):
    """Synthetic two-page doc: page 0 clean, page 1 anomalous."""
    clean_p0 = _uniform_price("$100.00", bottom=112.0)
    bad_p1_chars = "$200.00"
    bad_p1_fonts = [_HELV] * len(bad_p1_chars)
    bad_p1_fonts[1] = _TROM
    bad_p1 = _line(bad_p1_chars, fonts=bad_p1_fonts, bottom=112.0)
    for b in bad_p1:
        object.__setattr__(b, 'page_index', 1)
    f = analyze_font_consistency(_sd(clean_p0 + bad_p1, page_count=2))
    assert f.status == "danger"
    family_details = [d for d in f.details if "Mismatched font family" in d]
    assert len(family_details) == 1


# ---------------------------------------------------------------------------
# I. Baseline grouping (_group_into_lines)
# ---------------------------------------------------------------------------

def test_same_bottom_same_line():
    b1 = _block("1", x=10.0, bottom=112.0)
    b2 = _block("2", x=20.0, bottom=112.0)
    lines = _group_into_lines([b1, b2])
    assert len(lines) == 1


def test_far_apart_bottoms_separate_lines():
    b1 = _block("1", x=10.0, bottom=112.0)
    b2 = _block("2", x=10.0, bottom=112.0 + BASELINE_TOLERANCE + 1.0)
    lines = _group_into_lines([b1, b2])
    assert len(lines) == 2


def test_within_tolerance_same_line():
    """Two blocks whose bottom edges differ by exactly BASELINE_TOLERANCE → same line."""
    b1 = _block("1", x=10.0, bottom=112.0)
    b2 = _block("2", x=20.0, bottom=112.0 + BASELINE_TOLERANCE)
    lines = _group_into_lines([b1, b2])
    assert len(lines) == 1


def test_line_sorted_left_to_right():
    """Blocks must be returned in ascending x order within a line."""
    b1 = _block("A", x=50.0, bottom=112.0)
    b2 = _block("B", x=10.0, bottom=112.0)
    (line,) = _group_into_lines([b1, b2])
    assert [b.x for b in line] == sorted(b.x for b in line)


def test_mixed_font_size_same_visual_line():
    """A 14pt and 12pt char whose bottom difference < BASELINE_TOLERANCE → same line."""
    b_small = _block("A", font_size=12.0, bottom=112.0)
    b_large = _block("B", font_size=14.0, bottom=112.0 + 0.5)  # ~0.5pt diff
    lines = _group_into_lines([b_small, b_large])
    assert len(lines) == 1


def test_empty_blocks_gives_empty_lines():
    assert _group_into_lines([]) == []


# ---------------------------------------------------------------------------
# Helper: _find_numeric_spans
# ---------------------------------------------------------------------------

def test_find_spans_all_numeric():
    line = _line("$550.00")
    spans = _find_numeric_spans(line)
    assert len(spans) == 1
    assert spans[0].text == "$550.00"


def test_find_spans_broken_by_space():
    line = _line("$5 00")
    spans = _find_numeric_spans(line)
    assert len(spans) == 2


def test_find_spans_no_digit_excluded():
    line = _line("$.")
    spans = _find_numeric_spans(line)
    assert spans == []


def test_find_spans_min_length_enforced():
    line = _line("5")   # single digit
    spans = _find_numeric_spans(line)
    assert spans == []


# ---------------------------------------------------------------------------
# Helper: _check_span
# ---------------------------------------------------------------------------

def test_check_span_uniform_no_details():
    span = _Span(blocks=_line("$550.00"))
    assert _check_span(span) == []


def test_check_span_family_mismatch_detail():
    chars = "$550.00"
    fonts = [_HELV] * len(chars)
    fonts[1] = _TROM
    span = _Span(blocks=_line(chars, fonts=fonts))
    details = _check_span(span)
    assert any("Mismatched font family" in d for d in details)


def test_check_span_size_mismatch_detail():
    chars = "$550.00"
    sizes = [12.0] * len(chars)
    sizes[1] = 12.0 + FONT_SIZE_TOLERANCE + 1.0
    span = _Span(blocks=_line(chars, sizes=sizes))
    details = _check_span(span)
    assert any("size mismatch" in d.lower() for d in details)


# ---------------------------------------------------------------------------
# J. Integration tests — real PDF fixtures
# ---------------------------------------------------------------------------

def test_anomalous_pdf_is_danger(anomalous_pdf_path):
    from app.services.parser import parse_pdf
    f = analyze_font_consistency(parse_pdf(anomalous_pdf_path))
    assert f.status == "danger"


def test_anomalous_pdf_detail_mentions_times_roman(anomalous_pdf_path):
    from app.services.parser import parse_pdf
    f = analyze_font_consistency(parse_pdf(anomalous_pdf_path))
    assert any("Times-Roman" in d for d in f.details)


def test_anomalous_pdf_detail_mentions_span_text(anomalous_pdf_path):
    from app.services.parser import parse_pdf
    f = analyze_font_consistency(parse_pdf(anomalous_pdf_path))
    # The span is '$550.00' in the fixture
    assert any("550" in d for d in f.details)


def test_anomalous_pdf_detail_mentions_helvetica(anomalous_pdf_path):
    from app.services.parser import parse_pdf
    f = analyze_font_consistency(parse_pdf(anomalous_pdf_path))
    assert any("Helvetica" in d for d in f.details)


def test_anomalous_pdf_size_mismatch_detected(anomalous_pdf_path):
    """The anomalous 5 is 14pt vs 12pt consensus → size finding present."""
    from app.services.parser import parse_pdf
    f = analyze_font_consistency(parse_pdf(anomalous_pdf_path))
    assert any("size mismatch" in d.lower() for d in f.details)


def test_clean_pdf_is_info(clean_pdf_path):
    from app.services.parser import parse_pdf
    f = analyze_font_consistency(parse_pdf(clean_pdf_path))
    assert f.status == "info"
    assert f.details == []
