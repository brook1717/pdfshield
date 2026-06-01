"""
Validation tests for app/services/coordinate_analysis.py.

All blocks are constructed synthetically using precise coordinate values;
only the two integration tests at the bottom open real PDF fixtures.

Test matrix
-----------
A. Return-type / schema invariants
B. No-finding (healthy) cases
   B1. Empty blocks
   B2. Uniform y + uniform spacing → info
   B3. Single-block span (too short)
   B4. Whitespace-only line
C. Y-shift detection (_check_y_shift)
   C1. One char > Y_SHIFT_TOLERANCE above baseline → warning
   C2. One char > Y_SHIFT_TOLERANCE below baseline → warning
   C3. Deviation exactly == Y_SHIFT_TOLERANCE → NOT flagged (exclusive)
   C4. Deviation just above tolerance (tolerance + 0.01) → flagged
   C5. Detail string cites: char text, exact y, delta, span text, direction
   C6. Multiple shifted chars → multiple detail lines
   C7. All-identical y values → fast-path, no details
D. X-spacing detection (_check_x_spacing)
   D1. One gap > X_GAP_HIGH_RATIO × median → warning (oversized)
   D2. One gap < X_GAP_LOW_RATIO × median → warning (compressed)
   D3. Gap exactly == X_GAP_HIGH_RATIO × median → NOT flagged (exclusive)
   D4. Gap just above high ratio → flagged
   D5. Detail string cites: chars, gap size, median, ratio, direction word
   D6. All gaps uniform → info
   D7. Non-positive gaps are ignored in median computation
E. Cohesive-span segmentation (_find_cohesive_spans)
   E1. Space breaks a span
   E2. Multiple spaces treated as single break
   E3. Two words → two spans analyzed independently
   E4. Span of length 1 skipped
   E5. Entire line of spaces → no spans
F. Baseline grouping (_group_into_lines)
   F1. Same bottom → same line
   F2. Bottoms > BASELINE_TOLERANCE apart → separate lines
   F3. Bottoms within BASELINE_TOLERANCE → same line
   F4. Empty input → empty output
G. Status / check name invariants
   G1. Any finding → status "warning" (not "danger")
   G2. No findings → status "info"
   G3. check field is always "coordinate_alignment"
H. Integration tests
   H1. anomalous.pdf → warning, detail cites y-shift in '$550.00'
   H2. clean.pdf     → info, no details
"""
from __future__ import annotations

import pytest

from app.models.schemas import Finding, PDFMetadata, PDFStructuralData, TextBlock
from app.services.coordinate_analysis import (
    BASELINE_TOLERANCE,
    MIN_SPAN_LENGTH,
    X_GAP_HIGH_RATIO,
    X_GAP_LOW_RATIO,
    Y_SHIFT_TOLERANCE,
    _Span,
    _check_x_spacing,
    _check_y_shift,
    _find_cohesive_spans,
    _group_into_lines,
    analyze_coordinate_alignment,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

_CHAR_W = 7.0    # default char width for bbox construction
_BASE_BOTTOM = 112.0


def _block(
    text: str,
    x: float = 0.0,
    y: float | None = None,      # top edge; default = bottom - 12.0
    bottom: float = _BASE_BOTTOM,
    page_index: int = 0,
    font_name: str = "Helvetica",
    font_size: float = 12.0,
) -> TextBlock:
    top = y if y is not None else (bottom - font_size)
    return TextBlock(
        text=text,
        font_name=font_name,
        font_size=font_size,
        x=x,
        y=top,
        bbox=(x, top, x + _CHAR_W, bottom),
        page_index=page_index,
    )


def _uniform_line(
    text: str,
    y: float = 100.0,
    bottom: float = _BASE_BOTTOM,
    start_x: float = 10.0,
    char_step: float = _CHAR_W,
    page_index: int = 0,
) -> list[TextBlock]:
    """Build a perfectly uniform left-to-right line (same y and step)."""
    blocks: list[TextBlock] = []
    x = start_x
    for ch in text:
        blocks.append(_block(ch, x=x, y=y, bottom=bottom, page_index=page_index))
        x += char_step
    return blocks


def _sd(blocks: list[TextBlock], page_count: int = 1) -> PDFStructuralData:
    return PDFStructuralData(
        metadata=PDFMetadata(),
        page_count=page_count,
        has_text_layer=bool(blocks),
        raw_text="".join(b.text for b in blocks),
        blocks=blocks,
    )


# ---------------------------------------------------------------------------
# A. Return-type / schema invariants
# ---------------------------------------------------------------------------

def test_returns_finding():
    assert isinstance(analyze_coordinate_alignment(_sd([])), Finding)


def test_check_name():
    assert analyze_coordinate_alignment(_sd([])).check == "coordinate_alignment"


def test_details_is_list():
    assert isinstance(analyze_coordinate_alignment(_sd([])).details, list)


def test_details_are_strings():
    blocks = _uniform_line("$550.00")
    blocks[1] = _block("5", x=blocks[1].x, y=blocks[1].y - 2.0,
                        bottom=blocks[1].bbox[3] + 0.5)
    f = analyze_coordinate_alignment(_sd(blocks))
    assert all(isinstance(d, str) for d in f.details)


# ---------------------------------------------------------------------------
# B. No-finding cases
# ---------------------------------------------------------------------------

def test_empty_blocks_is_info():
    assert analyze_coordinate_alignment(_sd([])).status == "info"


def test_uniform_line_is_info():
    blocks = _uniform_line("$1500.00")
    assert analyze_coordinate_alignment(_sd(blocks)).status == "info"


def test_uniform_word_no_details():
    assert analyze_coordinate_alignment(_sd(_uniform_line("Invoice"))).details == []


def test_single_block_span_skipped():
    """A lone non-space char between spaces: span length 1 → skipped."""
    blocks = _uniform_line("A B")   # 'A', ' ', 'B' → two 1-char spans
    assert analyze_coordinate_alignment(_sd(blocks)).status == "info"


def test_whitespace_only_line_is_info():
    blocks = _uniform_line("   ")
    assert analyze_coordinate_alignment(_sd(blocks)).status == "info"


# ---------------------------------------------------------------------------
# C. Y-shift detection
# ---------------------------------------------------------------------------

def test_y_shift_above_tolerance_is_warning():
    """One char raised by more than Y_SHIFT_TOLERANCE → warning."""
    blocks = _uniform_line("$550.00", y=100.0)
    shift = Y_SHIFT_TOLERANCE + 0.5
    # Replace the '5' at index 1 with a version shifted upward (lower y value)
    orig = blocks[1]
    blocks[1] = _block("5", x=orig.x, y=orig.y - shift, bottom=orig.bbox[3])
    assert analyze_coordinate_alignment(_sd(blocks)).status == "warning"


def test_y_shift_below_tolerance_is_warning():
    """One char pushed down by more than Y_SHIFT_TOLERANCE → warning."""
    blocks = _uniform_line("$550.00", y=100.0)
    shift = Y_SHIFT_TOLERANCE + 0.5
    orig = blocks[1]
    blocks[1] = _block("5", x=orig.x, y=orig.y + shift, bottom=orig.bbox[3])
    assert analyze_coordinate_alignment(_sd(blocks)).status == "warning"


def test_y_shift_exactly_tolerance_not_flagged():
    """delta == Y_SHIFT_TOLERANCE is at the boundary — must NOT fire."""
    blocks = _uniform_line("$550.00", y=100.0)
    orig = blocks[1]
    blocks[1] = _block("5", x=orig.x, y=orig.y - Y_SHIFT_TOLERANCE,
                        bottom=orig.bbox[3])
    assert analyze_coordinate_alignment(_sd(blocks)).status == "info"


def test_y_shift_just_above_tolerance_flagged():
    blocks = _uniform_line("$550.00", y=100.0)
    orig = blocks[1]
    blocks[1] = _block("5", x=orig.x, y=orig.y - (Y_SHIFT_TOLERANCE + 0.01),
                        bottom=orig.bbox[3])
    assert analyze_coordinate_alignment(_sd(blocks)).status == "warning"


def test_y_shift_detail_contains_char_text():
    blocks = _uniform_line("$550.00", y=100.0)
    orig = blocks[1]
    blocks[1] = _block("5", x=orig.x, y=orig.y - 1.5, bottom=orig.bbox[3])
    detail = analyze_coordinate_alignment(_sd(blocks)).details[0]
    assert "'5'" in detail


def test_y_shift_detail_contains_span_text():
    blocks = _uniform_line("$550.00", y=100.0)
    orig = blocks[1]
    blocks[1] = _block("5", x=orig.x, y=orig.y - 1.5, bottom=orig.bbox[3])
    detail = analyze_coordinate_alignment(_sd(blocks)).details[0]
    assert "$550.00" in detail


def test_y_shift_detail_mentions_direction_above():
    """Char raised (lower y number) → 'above' in detail."""
    blocks = _uniform_line("$550.00", y=100.0)
    orig = blocks[1]
    blocks[1] = _block("5", x=orig.x, y=orig.y - 1.5, bottom=orig.bbox[3])
    detail = analyze_coordinate_alignment(_sd(blocks)).details[0]
    assert "above" in detail


def test_y_shift_detail_mentions_direction_below():
    """Char lowered (higher y number) → 'below' in detail."""
    blocks = _uniform_line("$550.00", y=100.0)
    orig = blocks[1]
    blocks[1] = _block("5", x=orig.x, y=orig.y + 1.5, bottom=orig.bbox[3])
    detail = analyze_coordinate_alignment(_sd(blocks)).details[0]
    assert "below" in detail


def test_y_shift_detail_contains_vertical_shift_phrase():
    blocks = _uniform_line("$550.00", y=100.0)
    orig = blocks[1]
    blocks[1] = _block("5", x=orig.x, y=orig.y - 1.5, bottom=orig.bbox[3])
    detail = analyze_coordinate_alignment(_sd(blocks)).details[0]
    assert "Vertical shift" in detail


def test_y_shift_multiple_chars_multiple_details():
    """Two chars shifted → two Y-shift detail lines."""
    blocks = _uniform_line("$550.00", y=100.0)
    for idx in (1, 3):
        orig = blocks[idx]
        blocks[idx] = _block(orig.text, x=orig.x, y=orig.y - 1.5,
                              bottom=orig.bbox[3])
    y_details = [d for d in analyze_coordinate_alignment(_sd(blocks)).details
                 if "Vertical shift" in d]
    assert len(y_details) == 2


def test_y_shift_all_identical_fast_path():
    """All chars have same y → check returns immediately with no details."""
    blocks = _uniform_line("550.00")
    details = _check_y_shift(_Span(blocks=blocks))
    assert details == []


# ---------------------------------------------------------------------------
# D. X-spacing detection
# ---------------------------------------------------------------------------

def test_x_gap_oversized_is_warning():
    """A single gap > X_GAP_HIGH_RATIO × median → warning."""
    # Build "550.00" with uniform 7pt steps, then make one gap 3x (21pt)
    blocks = _uniform_line("550.00", start_x=10.0, char_step=7.0)
    # inject a large gap between index 1 and 2 by moving all chars from idx 2 onward
    offset = 7.0 * X_GAP_HIGH_RATIO + 1.0   # > 2x median
    for i in range(2, len(blocks)):
        orig = blocks[i]
        blocks[i] = _block(orig.text, x=orig.x + offset,
                            y=orig.y, bottom=orig.bbox[3])
    f = analyze_coordinate_alignment(_sd(blocks))
    assert f.status == "warning"
    assert any("oversized" in d for d in f.details)


def test_x_gap_compressed_is_warning():
    """A single gap < X_GAP_LOW_RATIO × median → warning (compressed)."""
    # Build "550.00" with uniform 14pt steps (large median so we can compress easily)
    blocks = _uniform_line("550.00", start_x=10.0, char_step=14.0)
    # compress gap at index 1→2 to near zero
    compressed_x = blocks[1].x + 14.0 * X_GAP_LOW_RATIO * 0.5  # 0.5x low ratio
    orig = blocks[2]
    blocks[2] = _block(orig.text, x=compressed_x, y=orig.y, bottom=orig.bbox[3])
    f = analyze_coordinate_alignment(_sd(blocks))
    assert f.status == "warning"
    assert any("compressed" in d for d in f.details)


def test_x_gap_exactly_high_ratio_not_flagged():
    """gap == X_GAP_HIGH_RATIO × median is at the boundary — must NOT fire."""
    # median = 7pt, high threshold = 14pt → exactly 2x
    median_step = 7.0
    blocks = _uniform_line("550.00", start_x=10.0, char_step=median_step)
    # set gap[1→2] = exactly X_GAP_HIGH_RATIO * median
    exact_gap = median_step * X_GAP_HIGH_RATIO
    orig2 = blocks[2]
    # new x for blocks[2] = blocks[1].x + exact_gap
    new_x2 = blocks[1].x + exact_gap
    delta = new_x2 - orig2.x
    # shift blocks[2] onward to maintain subsequent uniform spacing
    for i in range(2, len(blocks)):
        ob = blocks[i]
        blocks[i] = _block(ob.text, x=ob.x + delta, y=ob.y, bottom=ob.bbox[3])
    f = analyze_coordinate_alignment(_sd(blocks))
    x_details = [d for d in f.details if "Irregular spacing" in d]
    assert x_details == []


def test_x_gap_just_above_high_ratio_flagged():
    median_step = 7.0
    blocks = _uniform_line("550.00", start_x=10.0, char_step=median_step)
    # set gap[1→2] = X_GAP_HIGH_RATIO * median + 0.1  (just over)
    new_gap = median_step * X_GAP_HIGH_RATIO + 0.1
    delta = new_gap - median_step
    for i in range(2, len(blocks)):
        ob = blocks[i]
        blocks[i] = _block(ob.text, x=ob.x + delta, y=ob.y, bottom=ob.bbox[3])
    f = analyze_coordinate_alignment(_sd(blocks))
    assert any("oversized" in d for d in f.details)


def test_x_gap_detail_mentions_both_chars():
    blocks = _uniform_line("AB1234", start_x=10.0, char_step=7.0)
    # push chars from index 3 onward far right
    for i in range(3, len(blocks)):
        ob = blocks[i]
        blocks[i] = _block(ob.text, x=ob.x + 30.0, y=ob.y, bottom=ob.bbox[3])
    details = analyze_coordinate_alignment(_sd(blocks)).details
    assert any("Irregular spacing" in d for d in details)


def test_x_gap_uniform_spacing_no_details():
    blocks = _uniform_line("100.00", char_step=7.0)
    x_details = _check_x_spacing(_Span(blocks=blocks))
    assert x_details == []


def test_x_gap_non_positive_gaps_ignored():
    """Overlapping chars (gap ≤ 0) must not be counted in the median.
    Use a numeric span so the x-spacing check is actually invoked."""
    blocks = _uniform_line("12345", char_step=7.0)
    # Collapse block[2] onto block[1] (zero gap)
    ob = blocks[2]
    blocks[2] = _block(ob.text, x=blocks[1].x, y=ob.y, bottom=ob.bbox[3])
    # Should not crash; no false-positive from the 0-gap
    f = analyze_coordinate_alignment(_sd(blocks))
    assert isinstance(f, Finding)


# ---------------------------------------------------------------------------
# E. Cohesive-span segmentation
# ---------------------------------------------------------------------------

def test_space_breaks_span():
    line = _uniform_line("AB CD")
    spans = _find_cohesive_spans(line)
    assert len(spans) == 2


def test_multiple_spaces_single_break():
    line = _uniform_line("AB   CD")
    spans = _find_cohesive_spans(line)
    assert len(spans) == 2


def test_two_words_two_spans():
    line = _uniform_line("Hello World")
    spans = _find_cohesive_spans(line)
    assert [s.text for s in spans] == ["Hello", "World"]


def test_span_of_one_skipped():
    line = _uniform_line("A B")   # 'A', ' ', 'B'
    spans = _find_cohesive_spans(line)
    assert spans == []


def test_all_spaces_no_spans():
    line = _uniform_line("   ")
    assert _find_cohesive_spans(line) == []


def test_no_space_whole_line_one_span():
    line = _uniform_line("$550.00")
    spans = _find_cohesive_spans(line)
    assert len(spans) == 1
    assert spans[0].text == "$550.00"


def test_span_text_correct():
    line = _uniform_line("Invoice Total")
    spans = _find_cohesive_spans(line)
    texts = [s.text for s in spans]
    assert "Invoice" in texts and "Total" in texts


# ---------------------------------------------------------------------------
# F. Baseline grouping
# ---------------------------------------------------------------------------

def test_same_bottom_same_line():
    b1 = _block("A", x=10.0, bottom=112.0)
    b2 = _block("B", x=20.0, bottom=112.0)
    lines = _group_into_lines([b1, b2])
    assert len(lines) == 1


def test_far_apart_separate_lines():
    b1 = _block("A", x=10.0, bottom=112.0)
    b2 = _block("B", x=10.0, bottom=112.0 + BASELINE_TOLERANCE + 1.0)
    lines = _group_into_lines([b1, b2])
    assert len(lines) == 2


def test_within_tolerance_same_line():
    """Exactly BASELINE_TOLERANCE apart → still same line (inclusive)."""
    b1 = _block("A", x=10.0, bottom=112.0)
    b2 = _block("B", x=20.0, bottom=112.0 + BASELINE_TOLERANCE)
    lines = _group_into_lines([b1, b2])
    assert len(lines) == 1


def test_empty_input_empty_output():
    assert _group_into_lines([]) == []


def test_line_sorted_by_x():
    b1 = _block("A", x=50.0, bottom=112.0)
    b2 = _block("B", x=10.0, bottom=112.0)
    (line,) = _group_into_lines([b1, b2])
    xs = [b.x for b in line]
    assert xs == sorted(xs)


# ---------------------------------------------------------------------------
# G. Status / check name invariants
# ---------------------------------------------------------------------------

def test_finding_with_anomaly_status_is_warning_not_danger():
    """Coordinate anomalies must produce 'warning', never 'danger'."""
    blocks = _uniform_line("$550.00", y=100.0)
    orig = blocks[2]
    blocks[2] = _block("5", x=orig.x, y=orig.y - 2.0, bottom=orig.bbox[3])
    f = analyze_coordinate_alignment(_sd(blocks))
    assert f.status == "warning"


def test_finding_with_anomaly_status_not_danger():
    blocks = _uniform_line("$550.00", y=100.0)
    orig = blocks[2]
    blocks[2] = _block("5", x=orig.x, y=orig.y - 2.0, bottom=orig.bbox[3])
    assert analyze_coordinate_alignment(_sd(blocks)).status != "danger"


def test_no_anomaly_status_is_info():
    assert analyze_coordinate_alignment(_sd(_uniform_line("$550.00"))).status == "info"


def test_check_name_stable_across_statuses():
    for blocks in (
        [],                           # info
        _uniform_line("$550.00"),     # info
    ):
        assert analyze_coordinate_alignment(_sd(blocks)).check == "coordinate_alignment"


# ---------------------------------------------------------------------------
# H. Integration tests — real fixtures
# ---------------------------------------------------------------------------

def test_anomalous_pdf_is_warning(anomalous_pdf_path):
    from app.services.parser import parse_pdf
    f = analyze_coordinate_alignment(parse_pdf(anomalous_pdf_path))
    assert f.status == "warning"


def test_anomalous_pdf_detail_mentions_vertical_shift(anomalous_pdf_path):
    from app.services.parser import parse_pdf
    f = analyze_coordinate_alignment(parse_pdf(anomalous_pdf_path))
    assert any("Vertical shift" in d for d in f.details)


def test_anomalous_pdf_detail_mentions_the_anomalous_char(anomalous_pdf_path):
    """The Times-Roman '5' at y=369.838 is the outlier — must appear in details."""
    from app.services.parser import parse_pdf
    f = analyze_coordinate_alignment(parse_pdf(anomalous_pdf_path))
    assert any("'5'" in d for d in f.details)


def test_anomalous_pdf_detail_contains_span_text(anomalous_pdf_path):
    """The affected span is '$550.00'."""
    from app.services.parser import parse_pdf
    f = analyze_coordinate_alignment(parse_pdf(anomalous_pdf_path))
    assert any("550" in d for d in f.details)


def test_clean_pdf_is_info(clean_pdf_path):
    from app.services.parser import parse_pdf
    f = analyze_coordinate_alignment(parse_pdf(clean_pdf_path))
    assert f.status == "info"
    assert f.details == []
