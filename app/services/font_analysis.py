"""
Typography consistency checking engine.

Focuses on *numeric spans* — runs of currency symbols, digits, decimal
points, and thousands separators that appear on the same visual baseline.
Within each such span the engine identifies characters whose font family or
size diverges from the consensus of the surrounding characters, which is a
classic indicator of manual content substitution (e.g., a digit pasted from
a different source application).

Detection pipeline
------------------
1. Group character-level blocks by page + visual baseline.
   Baseline is anchored to ``bbox[3]`` (bottom edge) because font-size
   differences shift the *top* edge but leave the *bottom* edge almost
   unchanged.  Two characters are on the same line when their bottom
   edges are within ``BASELINE_TOLERANCE`` points of each other.

2. Within each sorted baseline line, find **numeric spans**: maximal
   consecutive runs of characters drawn from ``NUMERIC_CHARS`` that
   contain at least one digit and are at least ``MIN_SPAN_LENGTH``
   characters long.

3. For each span, compute the *consensus* font family and size by majority
   vote (``collections.Counter.most_common``).

4. Any character whose font family differs from the consensus, or whose
   font size differs by more than ``FONT_SIZE_TOLERANCE`` points, is
   reported as an anomaly.

5. Duplicate detail lines within the *same span* are suppressed; identical
   issues across *different spans* are kept (they represent distinct fields).
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from app.models.schemas import Finding, PDFStructuralData, Severity, TextBlock

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuneable constants (exported so tests can reference them)
# ---------------------------------------------------------------------------

CHECK_NAME = "font_consistency"

#: Bottom-edge proximity used to assign characters to the same baseline.
BASELINE_TOLERANCE: float = 2.0

#: Font-size difference larger than this (exclusive) triggers an anomaly.
FONT_SIZE_TOLERANCE: float = 1.5

#: Numeric runs shorter than this are ignored.
MIN_SPAN_LENGTH: int = 2

#: Characters that may appear inside a numeric value.
NUMERIC_CHARS: frozenset[str] = frozenset("0123456789$.,€£%")


# ---------------------------------------------------------------------------
# Internal data structure
# ---------------------------------------------------------------------------

@dataclass
class _Span:
    """A maximal run of numeric characters on one visual baseline."""

    blocks: list[TextBlock] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks)

    @property
    def consensus_font(self) -> str:
        return Counter(b.font_name for b in self.blocks).most_common(1)[0][0]

    @property
    def consensus_size(self) -> float:
        return Counter(b.font_size for b in self.blocks).most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_font_consistency(structural_data: PDFStructuralData) -> Finding:
    """
    Scan *structural_data* for font-consistency anomalies in numeric spans.

    Parameters
    ----------
    structural_data:
        Populated :class:`~app.models.schemas.PDFStructuralData` from
        :func:`~app.services.parser.parse_pdf`.

    Returns
    -------
    Finding
        ``check="font_consistency"``.  Status is ``"danger"`` when at least
        one mismatch is detected, otherwise ``"info"``.  Each element of
        ``details`` describes one unique anomaly.
    """
    details: list[str] = []

    lines = _group_into_lines(structural_data.blocks)
    for line in lines:
        for span in _find_numeric_spans(line):
            details.extend(_check_span(span))

    status: Severity = "danger" if details else "info"
    if details:
        logger.info(
            "font_consistency: %d anomaly(ies) found across %d line(s)",
            len(details), len(lines),
        )
    return Finding(check=CHECK_NAME, status=status, details=details)


# ---------------------------------------------------------------------------
# Private pipeline steps
# ---------------------------------------------------------------------------

def _group_into_lines(blocks: list[TextBlock]) -> list[list[TextBlock]]:
    """
    Cluster *blocks* into visual baseline lines.

    Strategy: group by ``page_index`` first, then sort each page's blocks
    by their *bottom* edge (``bbox[3]``) and greedily merge consecutive
    blocks whose bottom edges are within ``BASELINE_TOLERANCE``.  Each
    resulting group is then re-sorted left-to-right by ``x``.
    """
    if not blocks:
        return []

    # Partition by page
    by_page: dict[int, list[TextBlock]] = {}
    for b in blocks:
        by_page.setdefault(b.page_index, []).append(b)

    all_lines: list[list[TextBlock]] = []

    for page_blocks in by_page.values():
        # Anchor sort: bottom edge first, then left-to-right within a line
        sorted_by_bottom = sorted(page_blocks, key=lambda b: (b.bbox[3], b.x))

        current_group: list[TextBlock] = []
        group_bottom: float | None = None

        for blk in sorted_by_bottom:
            bottom = blk.bbox[3]
            if group_bottom is None or abs(bottom - group_bottom) <= BASELINE_TOLERANCE:
                current_group.append(blk)
                if group_bottom is None:
                    group_bottom = bottom
            else:
                all_lines.append(sorted(current_group, key=lambda b: b.x))
                current_group = [blk]
                group_bottom = bottom

        if current_group:
            all_lines.append(sorted(current_group, key=lambda b: b.x))

    return all_lines


def _find_numeric_spans(line: list[TextBlock]) -> list[_Span]:
    """
    Return all maximal numeric spans within a single sorted baseline line.

    A numeric span must satisfy:
    * Every character is in ``NUMERIC_CHARS``.
    * At least one character is a digit (``str.isdigit()``).
    * Length ≥ ``MIN_SPAN_LENGTH``.
    """
    spans: list[_Span] = []
    current: list[TextBlock] = []

    def _flush() -> None:
        if len(current) >= MIN_SPAN_LENGTH and any(b.text.isdigit() for b in current):
            spans.append(_Span(blocks=list(current)))
        current.clear()

    for blk in line:
        if blk.text in NUMERIC_CHARS:
            current.append(blk)
        else:
            _flush()

    _flush()
    return spans


def _check_span(span: _Span) -> list[str]:
    """
    Return detail strings for every font anomaly found in *span*.

    Deduplication: within the same span the same (offending_font,
    expected_font) pair is reported at most once (family check), and the
    same (offending_size, expected_size) pair is reported at most once
    (size check).  Across different spans there is no deduplication so that
    each distinct numeric field is independently reported.
    """
    cons_font = span.consensus_font
    cons_size = span.consensus_size
    span_text = span.text
    details: list[str] = []

    seen_family: set[tuple[str, str]] = set()
    seen_size: set[tuple[float, float]] = set()

    for blk in span.blocks:
        # --- Font-family check -------------------------------------------
        if blk.font_name != cons_font:
            key = (blk.font_name, cons_font)
            if key not in seen_family:
                details.append(
                    f"Mismatched font family {blk.font_name!r} detected within "
                    f"numeric field {span_text!r} (expected {cons_font!r})"
                )
                seen_family.add(key)
                logger.debug(
                    "font_consistency: family mismatch %r vs %r in span %r",
                    blk.font_name, cons_font, span_text,
                )

        # --- Font-size check ---------------------------------------------
        size_delta = abs(blk.font_size - cons_size)
        if size_delta > FONT_SIZE_TOLERANCE:
            key_s = (blk.font_size, cons_size)
            if key_s not in seen_size:
                details.append(
                    f"Font size mismatch ({blk.font_size:.1f}pt vs expected "
                    f"{cons_size:.1f}pt) within numeric field {span_text!r}"
                )
                seen_size.add(key_s)
                logger.debug(
                    "font_consistency: size mismatch %.1fpt vs %.1fpt in span %r",
                    blk.font_size, cons_size, span_text,
                )

    return details
