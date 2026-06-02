"""
PDF visual annotator.

Opens an uploaded PDF and draws translucent red bounding-box rectangles
around every anomalous region identified by the forensic pipeline findings.
Each page that carries at least one annotation is rendered as a
high-resolution PNG and saved under ``app/static/annotated/``.

Coordinate extraction strategy
-------------------------------
Because :class:`~app.models.schemas.Finding` stores anomaly descriptions as
human-readable strings (not structured geometry), this module re-derives page
coordinates using two complementary techniques:

* **Text search** — for ``coordinate_alignment`` and ``font_consistency``
  findings the anomalous span text is extracted from the detail string via
  regex and located on every PDF page with :meth:`fitz.Page.search_for`.

* **Drawing scan** — for ``hidden_overlay_detection`` findings the 1-indexed
  page number is parsed from the detail string and the page's drawing commands
  are re-read with :meth:`fitz.Page.get_drawings` to recover the exact
  masking-fill rectangle.

A generic fallback searches for any single-quoted token found in an unrecognised
detail string so that future check types degrade gracefully rather than silently
producing blank output.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import fitz  # PyMuPDF

from app.models.schemas import Finding

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------

#: PNGs are saved here; path is relative to the ``app/`` package root.
_STATIC_DIR = Path(__file__).resolve().parents[1] / "static" / "annotated"

# ---------------------------------------------------------------------------
# Rendering constants
# ---------------------------------------------------------------------------

#: 2× scale renders a standard 72-DPI PDF page at ~144 DPI.
_RENDER_MATRIX = fitz.Matrix(2.0, 2.0)

_STROKE_COLOR = (0.80, 0.0, 0.0)   # dark red border
_FILL_COLOR   = (1.0,  0.2, 0.2)   # lighter red fill  (translucency via alpha)
_FILL_ALPHA   = 0.25                # 25 % opacity
_STROKE_WIDTH = 1.5                 # pt
_MIN_DRAW_AREA = 4.0                # sq pts — skip sub-pixel noise rectangles

# ---------------------------------------------------------------------------
# White-fill detection (mirrors overlay_detection constants; no cross-import)
# ---------------------------------------------------------------------------

_WHITE_THRESHOLD  = 0.85
_MIN_FILL_OPACITY = 0.85
_FILLED_TYPES     = frozenset({"f", "fs"})
_MIN_OVERLAY_AREA = 100.0           # sq pts

# Minimum character length for a span to be useful as a search query
_MIN_SEARCH_LEN = 2

# ---------------------------------------------------------------------------
# Regex patterns for extracting identifiers from Finding.details strings
# ---------------------------------------------------------------------------

# coordinate_alignment — vertical-shift detail
# "Vertical shift: char 'X' at y=123.456 is 2.3pt above … in span 'ABC'"
_RE_YSHIFT = re.compile(
    r"Vertical shift: char '([^']+)' at y=([\d.]+).*?in span '([^']+)'"
)

# coordinate_alignment — x-spacing detail
# "Irregular spacing: gap of … between 'A' and 'B' in span 'ABCDE' (…)"
_RE_XGAP = re.compile(
    r"Irregular spacing:.*?in span '([^']+)'"
)

# font_consistency — numeric field span text
# "… within numeric field '1,234.56' (expected …)"
_RE_FONT_SPAN = re.compile(r"numeric field '([^']+)'")

# hidden_overlay_detection — 1-indexed page number
# "… covering structural elements on Page 3"
_RE_OVERLAY_PAGE = re.compile(r"on Page (\d+)")

# Generic fallback — any single-quoted token with ≥ 2 characters
_RE_QUOTED = re.compile(r"'([^']{2,})'")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def annotate_pdf_anomalies(file_path: str, findings: list) -> str:
    """
    Draw translucent red rectangles over every anomalous region that can be
    geo-located from *findings*, render each annotated page as a
    high-resolution PNG, and return the relative URL of the first saved image.

    Parameters
    ----------
    file_path:
        Absolute filesystem path to the source PDF (same value stored in
        ``ForensicReport.file_path``).
    findings:
        List of :class:`~app.models.schemas.Finding` instances produced by
        the forensic pipeline.  Findings whose ``status`` is ``"info"`` are
        skipped because they carry no anomaly.

    Returns
    -------
    str
        Relative URL e.g. ``/static/annotated/abc123_p0.png`` for the first
        annotated page, or an empty string when no coordinates can be resolved
        or an error prevents rendering.

    Side-effects
    ------------
    * Creates ``app/static/annotated/`` if it does not already exist.
    * Writes one ``{stem}_p{page_index}.png`` file per annotated page.
    * Does **not** modify the source PDF on disk.
    """
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)

    path = Path(file_path)
    stem = path.stem  # UUID-based name without extension

    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        logger.error("annotator: cannot open '%s' — %s", path.name, exc)
        return ""

    # { page_index → [fitz.Rect, ...] }
    page_rects: dict[int, list[fitz.Rect]] = {}

    try:
        for finding in findings:
            if not isinstance(finding, Finding):
                continue
            if finding.status == "info":
                continue
            _collect_rects(doc, finding, page_rects)
    except Exception as exc:
        logger.error(
            "annotator: coordinate collection failed for '%s' — %s",
            path.name, exc, exc_info=True,
        )
        doc.close()
        return ""

    annotatable = {k: v for k, v in page_rects.items() if v}
    if not annotatable:
        doc.close()
        logger.info(
            "annotator: no locatable coordinates in findings for '%s'", path.name
        )
        return ""

    first_url = ""

    try:
        for page_idx in sorted(annotatable):
            if page_idx >= doc.page_count:
                logger.warning(
                    "annotator: page_idx %d out of range (%d pages in '%s')",
                    page_idx, doc.page_count, path.name,
                )
                continue

            page = doc[page_idx]
            rects = _deduplicate_rects(annotatable[page_idx])

            _draw_rects_on_page(page, rects)

            pix = page.get_pixmap(matrix=_RENDER_MATRIX, alpha=False)
            out_name = f"{stem}_p{page_idx}.png"
            out_path = _STATIC_DIR / out_name
            pix.save(str(out_path))

            url = f"/static/annotated/{out_name}"
            logger.info(
                "annotator: saved '%s' (%d annotation(s))", out_path.name, len(rects)
            )

            if not first_url:
                first_url = url

    except Exception as exc:
        logger.error(
            "annotator: rendering failed for '%s' — %s",
            path.name, exc, exc_info=True,
        )
    finally:
        doc.close()

    return first_url


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _draw_rects_on_page(page: fitz.Page, rects: list[fitz.Rect]) -> None:
    """
    Draw translucent red rectangles on *page* using PyMuPDF's shape API.

    All shapes share the same style (stroke + translucent fill).  The
    drawing lives only in memory — the source PDF is never written to disk.
    """
    shape = page.new_shape()
    drawn = 0

    for rect in rects:
        if rect.is_empty or rect.is_infinite or rect.get_area() < _MIN_DRAW_AREA:
            continue
        shape.draw_rect(rect)
        drawn += 1

    if drawn:
        shape.finish(
            color=_STROKE_COLOR,
            fill=_FILL_COLOR,
            fill_opacity=_FILL_ALPHA,
            stroke_opacity=1.0,
            width=_STROKE_WIDTH,
            even_odd=False,
        )
        shape.commit()


def _deduplicate_rects(rects: list[fitz.Rect]) -> list[fitz.Rect]:
    """
    Return a copy of *rects* with near-identical entries removed.

    Two rectangles are considered duplicates when all four edges are within
    1 pt of each other.
    """
    unique: list[fitz.Rect] = []
    for r in rects:
        if not any(
            abs(r.x0 - u.x0) < 1.0
            and abs(r.y0 - u.y0) < 1.0
            and abs(r.x1 - u.x1) < 1.0
            and abs(r.y1 - u.y1) < 1.0
            for u in unique
        ):
            unique.append(r)
    return unique


# ---------------------------------------------------------------------------
# Coordinate collection dispatcher
# ---------------------------------------------------------------------------

def _collect_rects(
    doc: fitz.Document,
    finding: Finding,
    page_rects: dict[int, list[fitz.Rect]],
) -> None:
    """
    Populate *page_rects* with annotatable bounding boxes for *finding*.

    Dispatches to a per-check-type extractor; falls back to a generic quoted-
    token search for unrecognised check names.
    """
    check = finding.check

    if check == "coordinate_alignment":
        for detail in finding.details:
            _extract_coord_alignment(doc, detail, page_rects)

    elif check == "font_consistency":
        for detail in finding.details:
            _extract_font_consistency(doc, detail, page_rects)

    elif check == "hidden_overlay_detection":
        for detail in finding.details:
            _extract_overlay(doc, detail, page_rects)

    else:
        for detail in finding.details:
            _extract_generic(doc, detail, page_rects)


# ---------------------------------------------------------------------------
# Per-check-type coordinate extractors
# ---------------------------------------------------------------------------

def _extract_coord_alignment(
    doc: fitz.Document,
    detail: str,
    page_rects: dict[int, list[fitz.Rect]],
) -> None:
    """
    Locate anomalous text for a ``coordinate_alignment`` detail.

    The span text embedded in both y-shift and x-gap detail strings is used
    as the :meth:`~fitz.Page.search_for` query because it is longer and more
    specific than a single anomalous character.
    """
    m = _RE_YSHIFT.search(detail)
    if m:
        span_text = m.group(3)
        if len(span_text) >= _MIN_SEARCH_LEN:
            _search_all_pages(doc, span_text, page_rects)
        return

    m = _RE_XGAP.search(detail)
    if m:
        span_text = m.group(1)
        if len(span_text) >= _MIN_SEARCH_LEN:
            _search_all_pages(doc, span_text, page_rects)


def _extract_font_consistency(
    doc: fitz.Document,
    detail: str,
    page_rects: dict[int, list[fitz.Rect]],
) -> None:
    """
    Locate the numeric field for a ``font_consistency`` detail by searching
    for the span text extracted from the detail string.
    """
    m = _RE_FONT_SPAN.search(detail)
    if not m:
        return
    span_text = m.group(1)
    if len(span_text) >= _MIN_SEARCH_LEN:
        _search_all_pages(doc, span_text, page_rects)


def _extract_overlay(
    doc: fitz.Document,
    detail: str,
    page_rects: dict[int, list[fitz.Rect]],
) -> None:
    """
    Recover the masking-fill rectangle(s) for a ``hidden_overlay_detection``
    detail by re-scanning the page's drawing commands.

    The page number (1-indexed) is parsed directly from the detail string.
    """
    m = _RE_OVERLAY_PAGE.search(detail)
    if not m:
        return

    page_idx = int(m.group(1)) - 1  # convert to 0-indexed
    if page_idx < 0 or page_idx >= doc.page_count:
        return

    page = doc[page_idx]
    for drawing in page.get_drawings():
        if not _is_masking_fill(drawing):
            continue

        drect = drawing.get("rect")
        if drect is None:
            continue
        if not isinstance(drect, fitz.Rect):
            try:
                drect = fitz.Rect(drect)
            except Exception:
                continue

        if drect.get_area() >= _MIN_OVERLAY_AREA:
            page_rects.setdefault(page_idx, []).append(drect)


def _extract_generic(
    doc: fitz.Document,
    detail: str,
    page_rects: dict[int, list[fitz.Rect]],
) -> None:
    """
    Fallback extractor for unrecognised check types.

    Searches for the first single-quoted token (≥ 2 chars) found in *detail*
    on every page.  This ensures future check types degrade gracefully rather
    than producing blank annotations.
    """
    for m in _RE_QUOTED.finditer(detail):
        token = m.group(1)
        if len(token) >= _MIN_SEARCH_LEN:
            _search_all_pages(doc, token, page_rects)
            return  # one search per detail is sufficient for the generic case


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _search_all_pages(
    doc: fitz.Document,
    text: str,
    page_rects: dict[int, list[fitz.Rect]],
) -> None:
    """
    Search for *text* on every page and append each hit's bounding box to
    *page_rects[page_index]*.
    """
    for page_idx in range(doc.page_count):
        hits = doc[page_idx].search_for(text)
        if hits:
            page_rects.setdefault(page_idx, []).extend(hits)


def _is_masking_fill(drawing: dict) -> bool:
    """
    Return ``True`` when *drawing* represents a solid, near-white filled shape.

    This predicate mirrors ``overlay_detection._is_masking_fill`` exactly so
    that the annotator requires no import from that service module.

    Rules (all must hold)
    ---------------------
    * ``type`` is ``"f"`` (fill-only) or ``"fs"`` (fill + stroke).
    * ``fill`` is a non-``None`` RGB or RGBA tuple.
    * Every colour channel is ≥ ``_WHITE_THRESHOLD`` (0.85).
    * ``fill_opacity`` is ≥ ``_MIN_FILL_OPACITY`` (0.85).
    """
    if drawing.get("type", "") not in _FILLED_TYPES:
        return False

    fill = drawing.get("fill")
    if fill is None:
        return False

    opacity = drawing.get("fill_opacity")
    if opacity is None:
        opacity = 1.0
    if float(opacity) < _MIN_FILL_OPACITY:
        return False

    if not isinstance(fill, (tuple, list)) or len(fill) < 3:
        return False

    return all(float(c) >= _WHITE_THRESHOLD for c in fill[:3])
