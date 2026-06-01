"""
Composite risk assessment engine and master forensic pipeline controller.

Color-code mapping
------------------
GREEN
    All five check results are ``"info"``; zero suspicious indicators.

YELLOW
    Exactly one finding carries status ``"warning"`` and zero findings carry
    ``"danger"``; a single, isolated minor anomaly.

RED
    Any of the following:

    * At least one finding has status ``"danger"`` (font switch, white-mask
      overlay, or impossible metadata date — all hard forensic indicators).
    * Two or more findings carry a non-``"info"`` status regardless of
      individual severity (multiple simultaneous anomalies).

The ``conclusion`` field in :class:`~app.models.schemas.RiskAssessment` is a
short, human-readable sentence generated from the color code and counts.

Pipeline
--------
``run_forensic_pipeline`` wires all five analysis modules in the correct
dependency order:

1. ``parser.parse_pdf``           — structural extraction (shared input)
2. ``metadata.analyze_metadata``  — tool fingerprint + date anomaly
3. ``text_layer.analyze_text_layer`` — selectable text presence check
4. ``font_analysis.analyze_font_consistency`` — numeric font consistency
5. ``coordinate_analysis.analyze_coordinate_alignment`` — spatial alignment
6. ``overlay_detection.detect_hidden_overlays`` — white-mask vector paths
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.models.schemas import Finding, ForensicReport, RiskAssessment
from app.services.coordinate_analysis import analyze_coordinate_alignment
from app.services.font_analysis import analyze_font_consistency
from app.services.metadata import analyze_metadata
from app.services.overlay_detection import detect_hidden_overlays
from app.services.parser import parse_pdf
from app.services.text_layer import analyze_text_layer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conclusion templates (keyed by color code)
# ---------------------------------------------------------------------------

_CONCLUSIONS: dict[str, str] = {
    "GREEN": (
        "No suspicious indicators detected. "
        "The document appears authentic."
    ),
    "YELLOW": (
        "A minor structural anomaly was detected. "
        "Manual review is recommended."
    ),
    "RED": (
        "Forensic indicators of document tampering were detected. "
        "Detailed review required."
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_overall_risk(findings: list[Finding]) -> RiskAssessment:
    """
    Map a list of :class:`~app.models.schemas.Finding` objects to a
    :class:`~app.models.schemas.RiskAssessment`.

    Parameters
    ----------
    findings:
        Results produced by the individual forensic checks.  May be empty.

    Returns
    -------
    RiskAssessment
        Populated with ``color_code``, counts, a per-check status map, and a
        generated conclusion sentence.

    Color-code rules (applied in order, first match wins)
    ------------------------------------------------------
    * ``"GREEN"``  — ``suspicious_count == 0``
    * ``"YELLOW"`` — ``suspicious_count == 1`` and ``danger_count == 0``
    * ``"RED"``    — any ``"danger"`` finding, *or* two or more non-info findings
    """
    danger_count = sum(1 for f in findings if f.status == "danger")
    warning_count = sum(1 for f in findings if f.status == "warning")
    suspicious_count = danger_count + warning_count

    if suspicious_count == 0:
        color_code: str = "GREEN"
    elif suspicious_count == 1 and danger_count == 0:
        color_code = "YELLOW"
    else:
        color_code = "RED"

    conclusion = _CONCLUSIONS[color_code]
    check_results: dict[str, str] = {f.check: f.status for f in findings}

    logger.info(
        "risk_engine: color=%s  danger=%d  warning=%d  total=%d",
        color_code, danger_count, warning_count, len(findings),
    )

    return RiskAssessment(
        color_code=color_code,
        total_findings=len(findings),
        suspicious_count=suspicious_count,
        check_results=check_results,
        conclusion=conclusion,
    )


def run_forensic_pipeline(file_path: str) -> ForensicReport:
    """
    Execute all forensic analysis modules against *file_path* and return a
    unified :class:`~app.models.schemas.ForensicReport`.

    Parameters
    ----------
    file_path:
        Absolute or relative path to the PDF file to examine.

    Returns
    -------
    ForensicReport
        Contains the original file path, the list of individual
        :class:`~app.models.schemas.Finding` objects, and the composite
        :class:`~app.models.schemas.RiskAssessment`.

    Notes
    -----
    The PDF is parsed once (``parse_pdf``); the resulting
    :class:`~app.models.schemas.PDFStructuralData` object is reused by all
    character-level checks.  ``detect_hidden_overlays`` reopens the file
    via fitz because it operates on the raw drawing-command stream rather
    than the pre-parsed text blocks.
    """
    path = Path(file_path)
    logger.info("forensic_pipeline: starting analysis of %s", path.name)

    structural_data = parse_pdf(path)

    findings: list[Finding] = [
        analyze_metadata(structural_data.metadata),
        analyze_text_layer(structural_data),
        analyze_font_consistency(structural_data),
        analyze_coordinate_alignment(structural_data),
        detect_hidden_overlays(path),
    ]

    risk = calculate_overall_risk(findings)

    logger.info(
        "forensic_pipeline: finished %s → %s", path.name, risk.color_code
    )

    return ForensicReport(
        file_path=str(path),
        findings=findings,
        risk=risk,
    )
