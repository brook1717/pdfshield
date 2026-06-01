"""
Unit and integration tests for app/services/overlay_detection.py.

Simulated drawing command models are plain Python dicts with the exact
structure returned by ``fitz.page.get_drawings()``.  Real ``fitz.Rect``
objects are used for bounding boxes (fitz is already a project dependency).

Test matrix
-----------
A. Return-type / schema invariants
B. _is_masking_fill — drawing predicate
   B1.  Pure white fill, 'fs' type, opaque → True
   B2.  Pure white fill, 'f' type only → True
   B3.  No fill (fill=None) → False
   B4.  Stroke-only type 's' → False
   B5.  Type '' (empty) → False
   B6.  Coloured fill (red) → False
   B7.  Near-white fill all channels == WHITE_CHANNEL_THRESHOLD → True
   B8.  Near-white fill one channel just below threshold → False
   B9.  fill_opacity == MIN_FILL_OPACITY → True (inclusive boundary)
   B10. fill_opacity just below MIN_FILL_OPACITY → False
   B11. fill_opacity=None treated as 1.0 → True
   B12. fill_opacity=0.0 (transparent) → False
   B13. fill as list (not tuple) accepted
   B14. fill with only 1 element → False (needs ≥3)
   B15. RGBA fill (4 elements) only first 3 channels checked
C. _check_coverage — spatial overlap logic
   C1.  Drawing fully contains text rect → True
   C2.  Drawing and text rect do not intersect → False
   C3.  Intersection covers exactly MIN_OVERLAP_RATIO → True (inclusive)
   C4.  Intersection covers just below MIN_OVERLAP_RATIO → False
   C5.  Multiple text rects, only one overlapped → True
   C6.  Empty text_rects list → False
   C7.  Zero-area text rect skipped (no division by zero)
   C8.  Drawing rect is a point (zero area) but text rect has area → False
D. detect_hidden_overlays — full-pipeline unit tests with synthetic pages
   D1.  No drawings on page → info
   D2.  Drawing with non-white fill → info
   D3.  White drawing not covering any text → info
   D4.  White drawing covering text → danger
   D5.  White drawing below MIN_RECT_AREA → info
   D6.  Stroke-only drawing over text → info
   D7.  Semi-transparent fill (opacity below MIN_FILL_OPACITY) → info
   D8.  Multiple pages: only page with overlay flagged, one detail per page
E. Finding content / shape
   E1.  Detail string contains "Suspicious solid masking polygon"
   E2.  Detail string contains "Page 1" (1-indexed)
   E3.  status == "danger" when overlay found
   E4.  check == "hidden_overlay_detection"
   E5.  details is list[str]
F. Integration tests — real fixtures
   F1.  anomalous.pdf → danger, detail cites Page 1
   F2.  clean.pdf     → info, no details
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import fitz
import pytest

from app.models.schemas import Finding
from app.services.overlay_detection import (
    FILLED_TYPES,
    MIN_FILL_OPACITY,
    MIN_OVERLAP_RATIO,
    MIN_RECT_AREA,
    WHITE_CHANNEL_THRESHOLD,
    _check_coverage,
    _is_masking_fill,
    detect_hidden_overlays,
)


# ---------------------------------------------------------------------------
# Drawing-dict factory
# ---------------------------------------------------------------------------

def _drawing(
    dtype: str = "fs",
    fill: tuple | None = (1.0, 1.0, 1.0),
    fill_opacity: float | None = 1.0,
    color: tuple | None = (1.0, 1.0, 1.0),
    rect: tuple = (0.0, 0.0, 100.0, 20.0),
) -> dict:
    """Construct a dict matching the structure of fitz page.get_drawings()."""
    return {
        "type": dtype,
        "fill": fill,
        "fill_opacity": fill_opacity,
        "color": color,
        "rect": fitz.Rect(rect),
        "stroke_opacity": 1.0,
        "width": 1.0,
        "items": [],
        "even_odd": False,
        "seqno": 0,
        "layer": "",
        "lineCap": (0, 0, 0),
        "lineJoin": 0.0,
        "closePath": False,
        "dashes": "[] 0",
    }


def _rect(x0: float, y0: float, x1: float, y1: float) -> fitz.Rect:
    return fitz.Rect(x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# A. Return-type / schema invariants
# ---------------------------------------------------------------------------

def test_returns_finding(anomalous_pdf_path):
    assert isinstance(detect_hidden_overlays(anomalous_pdf_path), Finding)


def test_check_name(clean_pdf_path):
    assert detect_hidden_overlays(clean_pdf_path).check == "hidden_overlay_detection"


def test_details_is_list(clean_pdf_path):
    assert isinstance(detect_hidden_overlays(clean_pdf_path).details, list)


def test_status_valid_literal(clean_pdf_path):
    assert detect_hidden_overlays(clean_pdf_path).status in ("info", "warning", "danger")


# ---------------------------------------------------------------------------
# B. _is_masking_fill predicate
# ---------------------------------------------------------------------------

def test_white_fs_is_masking():
    assert _is_masking_fill(_drawing(dtype="fs", fill=(1.0, 1.0, 1.0), fill_opacity=1.0))


def test_white_f_only_is_masking():
    assert _is_masking_fill(_drawing(dtype="f", fill=(1.0, 1.0, 1.0), fill_opacity=1.0))


def test_no_fill_not_masking():
    assert not _is_masking_fill(_drawing(fill=None))


def test_stroke_only_not_masking():
    assert not _is_masking_fill(_drawing(dtype="s", fill=(1.0, 1.0, 1.0)))


def test_empty_type_not_masking():
    assert not _is_masking_fill(_drawing(dtype="", fill=(1.0, 1.0, 1.0)))


def test_coloured_fill_not_masking():
    assert not _is_masking_fill(_drawing(fill=(1.0, 0.0, 0.0)))  # red


def test_near_white_at_threshold_is_masking():
    t = WHITE_CHANNEL_THRESHOLD
    assert _is_masking_fill(_drawing(fill=(t, t, t)))


def test_one_channel_below_threshold_not_masking():
    t = WHITE_CHANNEL_THRESHOLD - 0.01
    assert not _is_masking_fill(_drawing(fill=(1.0, 1.0, t)))


def test_opacity_at_min_is_masking():
    assert _is_masking_fill(_drawing(fill_opacity=MIN_FILL_OPACITY))


def test_opacity_just_below_min_not_masking():
    assert not _is_masking_fill(_drawing(fill_opacity=MIN_FILL_OPACITY - 0.01))


def test_opacity_none_treated_as_one():
    assert _is_masking_fill(_drawing(fill_opacity=None))


def test_opacity_zero_not_masking():
    assert not _is_masking_fill(_drawing(fill_opacity=0.0))


def test_fill_as_list_accepted():
    assert _is_masking_fill(_drawing(fill=[1.0, 1.0, 1.0]))


def test_fill_single_element_not_masking():
    assert not _is_masking_fill(_drawing(fill=(1.0,)))


def test_rgba_fill_only_first_three_channels_checked():
    """RGBA with opaque white first 3 channels: must be masking."""
    assert _is_masking_fill(_drawing(fill=(1.0, 1.0, 1.0, 0.0)))


def test_dark_fill_not_masking():
    assert not _is_masking_fill(_drawing(fill=(0.0, 0.0, 0.0)))  # black


def test_gray_below_threshold_not_masking():
    g = WHITE_CHANNEL_THRESHOLD - 0.1
    assert not _is_masking_fill(_drawing(fill=(g, g, g)))


# ---------------------------------------------------------------------------
# C. _check_coverage spatial logic
# ---------------------------------------------------------------------------

def test_drawing_contains_text_fully():
    drect = _rect(0, 0, 300, 20)
    trect = _rect(10, 5, 200, 15)   # fully inside
    assert _check_coverage(drect, [trect])


def test_no_intersection():
    drect = _rect(0, 0, 50, 10)
    trect = _rect(100, 0, 200, 10)  # no overlap
    assert not _check_coverage(drect, [trect])


def test_intersection_exactly_min_ratio():
    """Intersection == MIN_OVERLAP_RATIO of span area → True (inclusive)."""
    # span: 100×10 = 1000 sq pts
    # we need overlap = MIN_OVERLAP_RATIO * 1000 = 100 sq pts
    # drawing: 100 × 10 strip starting at span's left edge
    span_w, span_h = 100.0, 10.0
    overlap_w = span_w * MIN_OVERLAP_RATIO   # 10.0 pts
    drect = _rect(0, 0, overlap_w, span_h)
    trect = _rect(0, 0, span_w, span_h)
    assert _check_coverage(drect, [trect])


def test_intersection_just_below_min_ratio():
    span_w, span_h = 100.0, 10.0
    overlap_w = span_w * MIN_OVERLAP_RATIO - 0.01
    drect = _rect(0, 0, overlap_w, span_h)
    trect = _rect(0, 0, span_w, span_h)
    assert not _check_coverage(drect, [trect])


def test_multiple_rects_only_one_covered():
    drect = _rect(50, 0, 150, 20)
    t_away = _rect(200, 0, 300, 20)    # no overlap
    t_hit = _rect(60, 0, 140, 20)      # covered
    assert _check_coverage(drect, [t_away, t_hit])


def test_empty_text_rects_false():
    assert not _check_coverage(_rect(0, 0, 100, 100), [])


def test_zero_area_text_rect_skipped():
    """A degenerate (zero-area) text rect must not raise ZeroDivisionError."""
    drect = _rect(0, 0, 100, 100)
    trect = _rect(10, 10, 10, 10)   # zero area (point)
    result = _check_coverage(drect, [trect])
    assert isinstance(result, bool)


def test_zero_area_drawing_no_coverage():
    drect = _rect(10, 10, 10, 10)   # zero-area drawing
    trect = _rect(0, 0, 100, 100)
    assert not _check_coverage(drect, [trect])


# ---------------------------------------------------------------------------
# D. detect_hidden_overlays — pipeline tests via page-level mocking
# ---------------------------------------------------------------------------

def _make_mock_page(drawings: list[dict], spans: list[tuple[str, tuple]]) -> MagicMock:
    """Build a mock fitz.Page with controlled drawings and text spans."""
    page = MagicMock()
    page.get_drawings.return_value = drawings

    blocks = []
    for text, bbox in spans:
        span_dict = {"text": text, "bbox": bbox}
        line_dict = {"spans": [span_dict]}
        block_dict = {"type": 0, "lines": [line_dict]}
        blocks.append(block_dict)

    page.get_text.return_value = {"blocks": blocks}
    return page


def _run_single_page(drawings: list[dict], spans: list[tuple[str, tuple]]) -> Finding:
    """Exercise detect_hidden_overlays against one synthetic page."""
    mock_page = _make_mock_page(drawings, spans)
    details: list[str] = []
    with patch("fitz.open") as mock_open:
        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))
        mock_doc.__enter__ = MagicMock(return_value=mock_doc)
        mock_doc.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = mock_doc
        return detect_hidden_overlays("fake.pdf")


def test_no_drawings_is_info():
    f = _run_single_page([], [("Hello world", (72, 100, 300, 120))])
    assert f.status == "info"


def test_non_white_drawing_is_info():
    d = _drawing(fill=(0.0, 0.5, 1.0))
    f = _run_single_page([d], [("Hello world", (0, 0, 300, 20))])
    assert f.status == "info"


def test_white_drawing_no_text_overlap_is_info():
    d = _drawing(rect=(500, 500, 600, 520))
    text_span = ("Hello", (10, 10, 200, 30))
    f = _run_single_page([d], [text_span])
    assert f.status == "info"


def test_white_drawing_covering_text_is_danger():
    d = _drawing(rect=(0, 0, 300, 20))
    f = _run_single_page([d], [("Widget A line", (10, 2, 290, 18))])
    assert f.status == "danger"


def test_white_drawing_below_min_area_is_info():
    """A 9×9 = 81 sq pt rectangle is below MIN_RECT_AREA=100 → skipped."""
    small = _drawing(rect=(0, 0, 9, 9))
    f = _run_single_page([small], [("Hello", (0, 0, 300, 20))])
    assert f.status == "info"


def test_stroke_only_drawing_over_text_is_info():
    d = _drawing(dtype="s", fill=(1.0, 1.0, 1.0))
    f = _run_single_page([d], [("Hello", (0, 0, 300, 20))])
    assert f.status == "info"


def test_semi_transparent_fill_is_info():
    d = _drawing(fill_opacity=MIN_FILL_OPACITY - 0.1)
    f = _run_single_page([d], [("Hello", (0, 0, 300, 20))])
    assert f.status == "info"


def test_at_most_one_detail_per_page():
    """Two masking drawings on the same page → still one detail line."""
    d1 = _drawing(rect=(0, 0, 300, 20))
    d2 = _drawing(rect=(0, 0, 300, 20))
    f = _run_single_page([d1, d2], [("Hello", (10, 2, 290, 18))])
    assert len(f.details) == 1


# ---------------------------------------------------------------------------
# E. Finding content / shape
# ---------------------------------------------------------------------------

def test_detail_phrase_present():
    d = _drawing(rect=(0, 0, 300, 20))
    f = _run_single_page([d], [("Widget A", (10, 2, 290, 18))])
    assert any("Suspicious solid masking polygon" in det for det in f.details)


def test_detail_mentions_page_number():
    d = _drawing(rect=(0, 0, 300, 20))
    f = _run_single_page([d], [("Widget A", (10, 2, 290, 18))])
    assert any("Page 1" in det for det in f.details)


def test_status_danger_when_overlay():
    d = _drawing(rect=(0, 0, 300, 20))
    f = _run_single_page([d], [("Widget A", (10, 2, 290, 18))])
    assert f.status == "danger"


def test_check_name_correct():
    f = _run_single_page([], [])
    assert f.check == "hidden_overlay_detection"


def test_details_strings():
    d = _drawing(rect=(0, 0, 300, 20))
    f = _run_single_page([d], [("Widget A", (10, 2, 290, 18))])
    assert all(isinstance(s, str) for s in f.details)


# ---------------------------------------------------------------------------
# F. Integration tests — real fixtures
# ---------------------------------------------------------------------------

def test_anomalous_pdf_is_danger(anomalous_pdf_path):
    f = detect_hidden_overlays(anomalous_pdf_path)
    assert f.status == "danger"


def test_anomalous_pdf_detail_phrase(anomalous_pdf_path):
    f = detect_hidden_overlays(anomalous_pdf_path)
    assert any("Suspicious solid masking polygon" in d for d in f.details)


def test_anomalous_pdf_detail_mentions_page_1(anomalous_pdf_path):
    f = detect_hidden_overlays(anomalous_pdf_path)
    assert any("Page 1" in d for d in f.details)


def test_anomalous_pdf_detail_mentions_structural_elements(anomalous_pdf_path):
    f = detect_hidden_overlays(anomalous_pdf_path)
    assert any("structural elements" in d for d in f.details)


def test_clean_pdf_is_info(clean_pdf_path):
    f = detect_hidden_overlays(clean_pdf_path)
    assert f.status == "info"


def test_clean_pdf_no_details(clean_pdf_path):
    f = detect_hidden_overlays(clean_pdf_path)
    assert f.details == []
