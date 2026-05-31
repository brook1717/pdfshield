"""
Metadata evaluation engine.

Checks the four PDF metadata fields (creator, producer, creation_date,
mod_date) for two classes of forensic signals:

  1. Suspicious tool fingerprints — known web-based editors or flattening
     tools (e.g. Sejda, Smallpdf, iLovePDF) that are commonly used to
     alter invoice or contract PDFs.

  2. Date anomalies — modification date precedes creation date (impossible
     without tampering) or is significantly later (edited document).

Returns a single :class:`~app.models.schemas.Finding` that reflects the
worst severity found, together with a list of human-readable detail strings.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Literal, NamedTuple

from app.models.schemas import Finding, PDFMetadata, Severity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHECK_NAME = "metadata_analysis"

_SEVERITY_RANK: dict[str, int] = {"info": 0, "warning": 1, "danger": 2}

# Threshold: modifications beyond this many days post-creation are flagged
# as an "extreme" mismatch in addition to the base modification warning.
_EXTREME_DELTA_DAYS = 30


class _ToolRule(NamedTuple):
    substring: str          # matched case-insensitively against field value
    severity: Severity
    display_name: str       # friendly name used in the detail message


#: Deterministic, ordered list of suspicious tool fingerprints.
TOOL_RULES: list[_ToolRule] = [
    # ---- Online / web-based editors (high risk) ----------------------------
    _ToolRule("sejda",       "danger",  "Sejda"),
    _ToolRule("smallpdf",    "danger",  "Smallpdf"),
    _ToolRule("ilovepdf",    "danger",  "iLovePDF"),
    _ToolRule("pdfescape",   "danger",  "PDFescape"),
    _ToolRule("pdfzen",      "danger",  "PDFzen"),
    _ToolRule("pdf2doc",     "danger",  "PDF2Doc"),
    # ---- Desktop editors that can batch-edit content (medium risk) ---------
    _ToolRule("nitro",       "warning", "Nitro PDF"),
    _ToolRule("foxit",       "warning", "Foxit"),
    _ToolRule("pdf-xchange", "warning", "PDF-XChange"),
    _ToolRule("pdfxchange",  "warning", "PDF-XChange"),
    _ToolRule("pdf24",       "warning", "PDF24"),
    _ToolRule("pdfcreator",  "warning", "PDFCreator"),
]

# Regex that captures YYYYMMDDHHMMSS from a PDF date string.
_PDF_DATE_RE = re.compile(r"D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_metadata(metadata: PDFMetadata) -> Finding:
    """
    Evaluate *metadata* for suspicious tool fingerprints and date anomalies.

    Parameters
    ----------
    metadata:
        A :class:`PDFMetadata` instance, typically obtained from
        :func:`~app.services.parser.parse_pdf`.

    Returns
    -------
    Finding
        Always returns a :class:`Finding` with ``check="metadata_analysis"``.
        ``status`` reflects the worst severity detected; ``details`` contains
        one entry per individual finding.  Both lists are empty and status is
        ``"info"`` when no anomalies are found.
    """
    details: list[str] = []
    worst: Severity = "info"

    # --- 1. Tool fingerprint checks -----------------------------------------
    for field_label, field_value in (
        ("Creator", metadata.creator),
        ("Producer", metadata.producer),
    ):
        if not field_value:
            continue
        rule = _match_tool(field_value)
        if rule:
            details.append(
                f"Suspicious {field_label} detected: {field_value!r} [{rule.display_name}]"
            )
            worst = _escalate(worst, rule.severity)
            logger.debug("Tool match — %s: %r → %s", field_label, field_value, rule.severity)

    # --- 2. Date anomaly checks ---------------------------------------------
    date_detail, date_severity = _check_dates(
        metadata.creation_date, metadata.mod_date
    )
    if date_detail:
        details.append(date_detail)
        worst = _escalate(worst, date_severity)
        logger.debug("Date anomaly: %s → %s", date_detail, date_severity)

    return Finding(check=CHECK_NAME, status=worst, details=details)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _match_tool(value: str) -> _ToolRule | None:
    """Return the first matching tool rule (case-insensitive), or None."""
    lower = value.lower()
    for rule in TOOL_RULES:
        if rule.substring in lower:
            return rule
    return None


def _escalate(current: Severity, candidate: Severity) -> Severity:
    """Return whichever severity has higher rank."""
    if _SEVERITY_RANK.get(candidate, 0) > _SEVERITY_RANK.get(current, 0):
        return candidate
    return current


def _parse_pdf_date(date_str: str | None) -> datetime | None:
    """
    Parse a PDF date string (``D:YYYYMMDDHHmmss...``) into a UTC datetime.
    Returns None if the string is absent or does not match the expected pattern.
    """
    if not date_str:
        return None
    m = _PDF_DATE_RE.search(date_str)
    if not m:
        return None
    year, month, day, hour, minute, second = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


def _check_dates(
    creation_date: str | None,
    mod_date: str | None,
) -> tuple[str | None, Severity]:
    """
    Examine creation vs modification dates for forensic anomalies.

    Returns
    -------
    (detail_message, severity)
        detail_message is None when no anomaly is detected.
    """
    creation = _parse_pdf_date(creation_date)
    mod = _parse_pdf_date(mod_date)

    if creation is None or mod is None:
        return None, "info"

    delta = mod - creation

    if mod < creation:
        return (
            f"Modification date ({mod_date!r}) precedes creation date "
            f"({creation_date!r}) — chronologically impossible without tampering",
            "danger",
        )

    total_days = delta.days

    if total_days > _EXTREME_DELTA_DAYS:
        return (
            f"Modification date is {total_days} day(s) after creation date — "
            "extreme mismatch indicating post-creation editing",
            "warning",
        )

    if total_days > 0 or delta.seconds > 0:
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        return (
            f"Document was modified after creation "
            f"(delta: {total_days}d {hours}h {minutes}m)",
            "warning",
        )

    return None, "info"
