"""
Layout misalignment detection engine.

Analyses character-level spatial coordinates to detect localised manual
edits where a digit or character was pasted over existing content with a
slightly different vertical position or non-uniform horizontal spacing.

Detection pipeline
------------------
1. **Baseline grouping** — identical to ``font_analysis``: blocks are
   sorted by ``bbox[3]`` (bottom edge) and merged within
   ``BASELINE_TOLERANCE`` points, then re-sorted left-to-right by ``x``.

2. **Cohesive-span detection** — within each sorted baseline line, runs of
   consecutive *non-whitespace* characters are collected into spans.  A
   whitespace block breaks the span.  Spans shorter than
   ``MIN_SPAN_LENGTH`` are ignored.

3. **Y-shift check** — for each span the consensus top-y is the *median*
   of all ``block.y`` values.  Any character whose ``y`` deviates from the
   median by more than ``Y_SHIFT_TOLERANCE`` points is flagged.

4. **X-spacing check** — consecutive x-increments (``blocks[i+1].x -
   blocks[i].x``) are collected for each span.  The *median* increment is
   the expected character advance.  A gap that exceeds
   ``X_GAP_HIGH_RATIO × median`` or falls below ``X_GAP_LOW_RATIO ×
   median`` (compressed characters) is flagged.

Both checks emit ``status="warning"`` findings — they indicate probable
tampering rather than certain fraud, so they are intentionally softer than
font-mismatch ``danger`` findings.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

from app.models.schemas import Finding, PDFStructuralData, TextBlock

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exported constants — referenced by tests
# ---------------------------------------------------------------------------

CHECK_NAME = "coordinate_alignment"

#: bbox-bottom proximity to assign chars to the same visual baseline.
BASELINE_TOLERANCE: float = 2.0

#: Spans shorter than this are skipped.
MIN_SPAN_LENGTH: int = 2

#: Top-y deviation from the span median beyond which a char is flagged.
Y_SHIFT_TOLERANCE: float = 1.0

#: A gap > this multiple of the median inter-char gap is flagged.
X_GAP_HIGH_RATIO: float = 2.0

#: A gap < this fraction of the median inter-char gap is flagged.
X_GAP_LOW_RATIO: float = 0.35


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _median(values: list[float]) -> float:
    return statistics.median(values)


@dataclass
class _Span:
    blocks: list[TextBlock] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks)


def _group_into_lines(blocks: list[TextBlock]) -> list[list[TextBlock]]:
    """Cluster blocks into visual baseline lines (same algorithm as font_analysis)."""
    if not blocks:
        return []

    by_page: dict[int, list[TextBlock]] = {}
    for b in blocks:
        by_page.setdefault(b.page_index, []).append(b)

    all_lines: list[list[TextBlock]] = []

    for page_blocks in by_page.values():
        sorted_blocks = sorted(page_blocks, key=lambda b: (b.bbox[3], b.x))
        current: list[TextBlock] = []
        anchor: float | None = None

        for blk in sorted_blocks:
            bottom = blk.bbox[3]
            if anchor is None or abs(bottom - anchor) <= BASELINE_TOLERANCE:
                current.append(blk)
                if anchor is None:
                    anchor = bottom
            else:
                all_lines.append(sorted(current, key=lambda b: b.x))
                current = [blk]
                anchor = bottom

        if current:
            all_lines.append(sorted(current, key=lambda b: b.x))

    return all_lines


def _find_cohesive_spans(line: list[TextBlock]) -> list[_Span]:
    """
    Split a sorted line into cohesive spans by breaking on whitespace blocks.
    Each span must have at least ``MIN_SPAN_LENGTH`` characters.
    """
    spans: list[_Span] = []
    current: list[TextBlock] = []

    def _flush() -> None:
        if len(current) >= MIN_SPAN_LENGTH:
            spans.append(_Span(blocks=list(current)))
        current.clear()

    for blk in line:
        if blk.text.strip() == "":
            _flush()
        else:
            current.append(blk)

    _flush()
    return spans


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_coordinate_alignment(structural_data: PDFStructuralData) -> Finding:
    """
    Scan *structural_data* for spatial anomalies within cohesive text spans.

    Returns
    -------
    Finding
        ``check="coordinate_alignment"``.  Status is ``"warning"`` when at
        least one anomaly is detected, otherwise ``"info"``.
    """
    details: list[str] = []

    lines = _group_into_lines(structural_data.blocks)
    for line in lines:
        for span in _find_cohesive_spans(line):
            details.extend(_check_y_shift(span))
            # X-spacing is only reliable inside numeric contexts where
            # character advance-widths are uniform (digits, currency, decimals).
            # Proportional-font word spans naturally vary in char width and
            # would produce false positives if checked here.
            if any(b.text.isdigit() for b in span.blocks):
                details.extend(_check_x_spacing(span))

    status = "warning" if details else "info"
    if details:
        logger.info(
            "coordinate_alignment: %d anomaly(ies) across %d line(s)",
            len(details), len(lines),
        )
    return Finding(check=CHECK_NAME, status=status, details=details)


# ---------------------------------------------------------------------------
# Per-span checks
# ---------------------------------------------------------------------------

def _check_y_shift(span: _Span) -> list[str]:
    """
    Flag any character whose top-y deviates from the span's median top-y
    by more than ``Y_SHIFT_TOLERANCE`` points.
    """
    ys = [b.y for b in span.blocks]
    if len(set(ys)) == 1:
        return []   # fast path — all identical

    consensus_y = _median(ys)
    details: list[str] = []

    for blk in span.blocks:
        delta = abs(blk.y - consensus_y)
        if delta > Y_SHIFT_TOLERANCE:
            direction = "above" if blk.y < consensus_y else "below"
            details.append(
                f"Vertical shift: char {blk.text!r} at y={blk.y:.3f} is "
                f"{delta:.3f}pt {direction} baseline y={consensus_y:.3f} "
                f"in span {span.text!r}"
            )
            logger.debug(
                "coord: y-shift %.3fpt for %r in span %r", delta, blk.text, span.text
            )

    return details


def _check_x_spacing(span: _Span) -> list[str]:
    """
    Flag inter-character x-gaps that are suspiciously large or small
    relative to the span's median gap.

    Gaps smaller than ``X_GAP_LOW_RATIO × median`` are indicative of
    compressed / overlapping pasted content; gaps larger than
    ``X_GAP_HIGH_RATIO × median`` suggest a character was inserted with
    an unexpected offset.
    """
    if len(span.blocks) < 2:
        return []

    gaps: list[float] = [
        span.blocks[i + 1].x - span.blocks[i].x
        for i in range(len(span.blocks) - 1)
    ]

    positive_gaps = [g for g in gaps if g > 0]
    if not positive_gaps:
        return []

    median_gap = _median(positive_gaps)
    if median_gap <= 0:
        return []

    details: list[str] = []

    for i, gap in enumerate(gaps):
        if gap <= 0:
            continue
        ratio = gap / median_gap

        if ratio > X_GAP_HIGH_RATIO:
            left = span.blocks[i].text
            right = span.blocks[i + 1].text
            details.append(
                f"Irregular spacing: gap of {gap:.2f}pt between {left!r} and "
                f"{right!r} in span {span.text!r} "
                f"(median {median_gap:.2f}pt, ratio {ratio:.1f}x — oversized)"
            )
            logger.debug(
                "coord: x-gap %.2f / median %.2f = %.1fx (high) in span %r",
                gap, median_gap, ratio, span.text,
            )

        elif ratio < X_GAP_LOW_RATIO:
            left = span.blocks[i].text
            right = span.blocks[i + 1].text
            details.append(
                f"Irregular spacing: gap of {gap:.2f}pt between {left!r} and "
                f"{right!r} in span {span.text!r} "
                f"(median {median_gap:.2f}pt, ratio {ratio:.1f}x — compressed)"
            )
            logger.debug(
                "coord: x-gap %.2f / median %.2f = %.1fx (low) in span %r",
                gap, median_gap, ratio, span.text,
            )

    return details
