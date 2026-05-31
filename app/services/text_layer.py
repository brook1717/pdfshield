"""
Text-layer presence validation service.

Evaluates whether a PDF contains a real, selectable text layer or has been
flattened into a purely image-based or vector-rasterised representation.

Three-tier detection
--------------------
1. Primary (danger) — the document has pages but its text layer is absent or
   effectively empty:
     • PyMuPDF found no text stream (``has_text_layer = False``).
     • Total stripped raw text is shorter than ``MIN_TEXT_CHARS`` characters.
     • pdfplumber found zero character-level rendering elements.
   Any one of these failing triggers *danger* with the canonical detail string
   "No selectable text layer detected: Flattened or Image-only PDF" plus a
   parenthesised breakdown of exactly which sub-checks failed.

2. Sparsity (warning) — the document passes the primary check (some text
   exists) but the character density per page falls below
   ``MIN_BLOCKS_PER_PAGE``.  This can indicate a *partially* flattened PDF
   where only a narrow header or watermark survived conversion.

3. Empty document (info) — a PDF with zero pages carries no finding.
"""
from __future__ import annotations

import logging

from app.models.schemas import Finding, PDFStructuralData, Severity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuneable thresholds (exported so tests can reference them)
# ---------------------------------------------------------------------------

CHECK_NAME = "text_layer_analysis"

#: Stripped raw-text shorter than this is treated as "effectively no text".
MIN_TEXT_CHARS: int = 10

#: Character-block density below this per page triggers a sparsity warning.
MIN_BLOCKS_PER_PAGE: float = 5.0

_SEVERITY_RANK: dict[str, int] = {"info": 0, "warning": 1, "danger": 2}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_text_layer(structural_data: PDFStructuralData) -> Finding:
    """
    Check whether *structural_data* contains a genuine selectable text layer.

    Parameters
    ----------
    structural_data:
        Populated :class:`~app.models.schemas.PDFStructuralData` from
        :func:`~app.services.parser.parse_pdf`.

    Returns
    -------
    Finding
        Always returns a :class:`~app.models.schemas.Finding` with
        ``check="text_layer_analysis"``.  Status is ``"info"`` when the text
        layer is healthy, ``"warning"`` when sparse, and ``"danger"`` when
        absent or effectively empty.
    """
    details: list[str] = []
    worst: Severity = "info"

    page_count = structural_data.page_count
    has_tl = structural_data.has_text_layer
    raw_len = len(structural_data.raw_text.strip())
    block_count = len(structural_data.blocks)

    # No pages — nothing to evaluate.
    if page_count == 0:
        logger.debug("text_layer: page_count=0, skipping checks")
        return Finding(check=CHECK_NAME, status="info", details=[])

    # ------------------------------------------------------------------
    # Primary check: is there a meaningful text layer at all?
    # ------------------------------------------------------------------
    has_sufficient = has_tl and raw_len >= MIN_TEXT_CHARS and block_count > 0

    if not has_sufficient:
        reasons: list[str] = []
        if not has_tl:
            reasons.append("no embedded text stream")
        elif raw_len < MIN_TEXT_CHARS:
            reasons.append(
                f"only {raw_len} extractable character(s) "
                f"(threshold: {MIN_TEXT_CHARS})"
            )
        if block_count == 0:
            reasons.append("no character-level rendering elements found")

        suffix = f" ({'; '.join(reasons)})" if reasons else ""
        details.append(
            "No selectable text layer detected: Flattened or Image-only PDF" + suffix
        )
        worst = _escalate(worst, "danger")
        logger.info(
            "text_layer: danger — %s | page_count=%d raw_len=%d blocks=%d",
            ", ".join(reasons), page_count, raw_len, block_count,
        )
        return Finding(check=CHECK_NAME, status=worst, details=details)

    # ------------------------------------------------------------------
    # Secondary check: density (only reached when primary passes)
    # ------------------------------------------------------------------
    density = block_count / page_count
    if density < MIN_BLOCKS_PER_PAGE:
        details.append(
            f"Sparse text layer: {block_count} char block(s) across "
            f"{page_count} page(s) ({density:.1f} blocks/page) — "
            "possible partial flattening"
        )
        worst = _escalate(worst, "warning")
        logger.info(
            "text_layer: warning — sparse %.1f blocks/page (threshold %.1f)",
            density, MIN_BLOCKS_PER_PAGE,
        )

    return Finding(check=CHECK_NAME, status=worst, details=details)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _escalate(current: Severity, candidate: Severity) -> Severity:
    """Return whichever severity has the higher rank."""
    if _SEVERITY_RANK.get(candidate, 0) > _SEVERITY_RANK.get(current, 0):
        return candidate
    return current
