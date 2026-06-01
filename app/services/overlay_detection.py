"""
Vector masking / hidden overlay detection engine.

Uses PyMuPDF (fitz) drawing commands (``page.get_drawings()``) to detect
solid-coloured vector rectangles or paths that cover existing text content,
which is a classic indicator that original text was hidden beneath an
opaque white (or paper-coloured) shape before replacement text was drawn
on top.

Detection pipeline
------------------
1. Open the PDF with ``fitz.open`` and iterate over every page.
2. Extract all non-empty text-span bounding boxes from
   ``page.get_text("dict")``.
3. Extract all drawing commands via ``page.get_drawings()``.
4. For each drawing command:
   a. Verify it carries a **filled** path (``type`` in ``{"f", "fs"}``).
   b. Verify the fill colour is **white or near-white** — all RGB channels
      ≥ ``WHITE_CHANNEL_THRESHOLD`` (catches pure white, ivory, cream).
   c. Verify fill opacity is **solid** (``fill_opacity ≥ MIN_FILL_OPACITY``).
   d. Verify the drawing bounding rectangle meets the minimum area guard
      (``MIN_RECT_AREA`` sq pts) to ignore tiny decorative hairlines.
   e. Check every text span for intersection.  If the intersection covers
      at least ``MIN_OVERLAP_RATIO`` of the span's area, the drawing is
      masking text → emit one ``danger`` detail per page.

Pure-logic helpers (``_is_masking_fill``, ``_check_coverage``) are
intentionally importable so the test suite can exercise them with
simulated drawing-command dicts and synthetic ``fitz.Rect`` objects
without opening a real PDF.
"""
from __future__ import annotations

import logging
from pathlib import Path

import fitz  # PyMuPDF

from app.models.schemas import Finding

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exported constants — referenced by tests
# ---------------------------------------------------------------------------

CHECK_NAME = "hidden_overlay_detection"

#: RGB channel value at or above which a fill is considered white / background.
WHITE_CHANNEL_THRESHOLD: float = 0.85

#: Fill opacity must be this high to be treated as a solid mask.
MIN_FILL_OPACITY: float = 0.85

#: Fraction of a text span's area that must be covered to trigger a finding.
MIN_OVERLAP_RATIO: float = 0.10

#: Minimum bounding-box area (sq pts) of a drawing to be examined.
#: Filters out sub-pixel hairlines and tiny decorative shapes.
MIN_RECT_AREA: float = 100.0

#: Drawing type values that indicate a fill is present.
FILLED_TYPES: frozenset[str] = frozenset({"f", "fs"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_hidden_overlays(pdf_path: Path | str) -> Finding:
    """
    Scan every page of *pdf_path* for solid white/background-coloured vector
    shapes that cover text content.

    Parameters
    ----------
    pdf_path:
        Filesystem path to the PDF file.

    Returns
    -------
    Finding
        ``check="hidden_overlay_detection"``.  Status is ``"danger"`` when at
        least one masking overlay is confirmed, otherwise ``"info"``.  Each
        detail line names the affected page (1-indexed).
    """
    details: list[str] = []
    path = Path(pdf_path)

    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        logger.error("overlay_detection: cannot open %s — %s", path, exc)
        return Finding(check=CHECK_NAME, status="info", details=[])

    try:
        for page_num, page in enumerate(doc):
            _scan_page(page, page_num, details)
    finally:
        doc.close()

    status = "danger" if details else "info"
    if details:
        logger.info("overlay_detection: %d page(s) with masking overlays", len(details))
    return Finding(check=CHECK_NAME, status=status, details=details)


# ---------------------------------------------------------------------------
# Private pipeline steps (exported for unit-testing)
# ---------------------------------------------------------------------------

def _scan_page(
    page: fitz.Page,
    page_num: int,
    details: list[str],
) -> None:
    """
    Examine one fitz page and append a detail string if any masking overlay
    is found.  At most **one** detail is emitted per page to avoid
    flooding the report when a page contains many overlapping shapes.
    """
    text_rects = _extract_text_rects(page)
    if not text_rects:
        return

    for drawing in page.get_drawings():
        if not _is_masking_fill(drawing):
            continue

        drect = drawing.get("rect")
        if drect is None:
            continue
        if not isinstance(drect, fitz.Rect):
            drect = fitz.Rect(drect)
        if drect.get_area() < MIN_RECT_AREA:
            continue

        if _check_coverage(drect, text_rects):
            details.append(
                f"Suspicious solid masking polygon detected covering "
                f"structural elements on Page {page_num + 1}"
            )
            logger.debug(
                "overlay_detection: masking rect %s on page %d",
                drect, page_num + 1,
            )
            return  # one finding per page is enough


def _extract_text_rects(page: fitz.Page) -> list[fitz.Rect]:
    """Return bounding boxes for every non-empty text span on *page*."""
    rects: list[fitz.Rect] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                text = span.get("text", "").strip()
                if text:
                    rects.append(fitz.Rect(span["bbox"]))
    return rects


def _is_masking_fill(drawing: dict) -> bool:
    """
    Return ``True`` when *drawing* carries a solid, opaque, white (or
    near-white / paper-background) fill.

    This function is a pure predicate and accepts a plain ``dict`` so it
    can be called from tests without a real fitz page.

    Parameters
    ----------
    drawing:
        A dict as returned by ``page.get_drawings()``.

    Rules applied (all must hold)
    ------------------------------
    * ``type`` is in ``FILLED_TYPES`` (``"f"`` or ``"fs"``).
    * ``fill`` is a non-``None`` RGB/RGBA tuple.
    * Every colour channel ≥ ``WHITE_CHANNEL_THRESHOLD``.
    * ``fill_opacity`` ≥ ``MIN_FILL_OPACITY``.
    """
    dtype = drawing.get("type", "")
    if dtype not in FILLED_TYPES:
        return False

    fill = drawing.get("fill")
    if fill is None:
        return False

    opacity = drawing.get("fill_opacity")
    if opacity is None:
        opacity = 1.0
    if opacity < MIN_FILL_OPACITY:
        return False

    if not isinstance(fill, (tuple, list)) or len(fill) < 3:
        return False

    return all(float(c) >= WHITE_CHANNEL_THRESHOLD for c in fill[:3])


def _check_coverage(
    drawing_rect: fitz.Rect,
    text_rects: list[fitz.Rect],
) -> bool:
    """
    Return ``True`` if *drawing_rect* covers at least ``MIN_OVERLAP_RATIO``
    of **any** text rect in *text_rects*.

    This function accepts plain ``fitz.Rect`` objects and is importable for
    direct testing.
    """
    for tr in text_rects:
        if not drawing_rect.intersects(tr):
            continue
        span_area = tr.get_area()
        if span_area <= 0:
            continue
        inter_area = (drawing_rect & tr).get_area()
        if inter_area / span_area >= MIN_OVERLAP_RATIO:
            return True
    return False
