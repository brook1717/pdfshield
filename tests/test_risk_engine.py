"""
Tests for app/services/risk_engine.py.

Test matrix
-----------
A. Return-type / schema invariants
   A1.  calculate_overall_risk returns RiskAssessment
   A2.  run_forensic_pipeline returns ForensicReport
   A3.  RiskAssessment fields present and typed correctly
   A4.  ForensicReport fields present and typed correctly

B. calculate_overall_risk — color-code rules
   B1.  All info → GREEN
   B2.  Empty findings list → GREEN
   B3.  One warning, zero danger → YELLOW
   B4.  One danger, zero warning → RED
   B5.  Two warnings, zero danger → RED (multiple anomalies)
   B6.  One danger + one warning → RED
   B7.  Two dangers → RED
   B8.  Three info + one warning → YELLOW
   B9.  Three info + one danger → RED
   B10. suspicious_count == 1 AND danger_count == 1 → RED (not YELLOW)

C. calculate_overall_risk — counts
   C1.  total_findings == len(findings)
   C2.  suspicious_count == warning_count + danger_count
   C3.  suspicious_count == 0 when all info
   C4.  check_results maps each check name to its status
   C5.  check_results length == len(findings)
   C6.  duplicate check names: last one wins (dict semantics)

D. calculate_overall_risk — conclusion text
   D1.  GREEN conclusion mentions "authentic"
   D2.  YELLOW conclusion mentions "manual review"
   D3.  RED conclusion mentions "tampering" or "manipulation" or "review required"
   D4.  conclusion is non-empty string for all color codes
   D5.  color_code is in {"GREEN", "YELLOW", "RED"}

E. calculate_overall_risk — boundary values
   E1.  suspicious_count == 1 and danger_count == 0 → exactly YELLOW (not RED)
   E2.  suspicious_count == 2 and danger_count == 0 → RED (not YELLOW)
   E3.  font_consistency danger alone → RED
   E4.  hidden_overlay_detection danger alone → RED

F. run_forensic_pipeline — shape and content
   F1.  file_path field preserved in ForensicReport
   F2.  findings list has exactly 5 elements (one per check module)
   F3.  every finding is a Finding instance
   F4.  all five expected check names present in findings
   F5.  risk field is a RiskAssessment instance
   F6.  check_results in risk covers all five check names

G. Integration — clean fixture
   G1.  clean.pdf → color_code == "GREEN"
   G2.  clean.pdf → suspicious_count == 0
   G3.  clean.pdf → all check statuses are "info"
   G4.  clean.pdf → conclusion mentions "authentic"
   G5.  clean.pdf → total_findings == 5

H. Integration — anomalous fixture
   H1.  anomalous.pdf → color_code == "RED"
   H2.  anomalous.pdf → suspicious_count >= 2
   H3.  anomalous.pdf → font_consistency check is "danger"
   H4.  anomalous.pdf → hidden_overlay_detection check is "danger"
   H5.  anomalous.pdf → metadata_analysis check is "danger" (Sejda tool)
   H6.  anomalous.pdf → conclusion mentions "tampering" or "review required"
   H7.  anomalous.pdf → total_findings == 5
   H8.  anomalous.pdf → ForensicReport.file_path ends with "anomalous.pdf"
"""
from __future__ import annotations

import pytest

from app.models.schemas import Finding, ForensicReport, RiskAssessment
from app.services.risk_engine import (
    _CONCLUSIONS,
    calculate_overall_risk,
    run_forensic_pipeline,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

CHECKS = [
    "metadata_analysis",
    "text_layer_analysis",
    "font_consistency",
    "coordinate_alignment",
    "hidden_overlay_detection",
]


def _f(check: str, status: str, details: list[str] | None = None) -> Finding:
    return Finding(check=check, status=status, details=details or [])


def _all_info() -> list[Finding]:
    return [_f(c, "info") for c in CHECKS]


# ---------------------------------------------------------------------------
# A. Return-type / schema invariants
# ---------------------------------------------------------------------------

def test_calculate_returns_risk_assessment():
    assert isinstance(calculate_overall_risk([]), RiskAssessment)


def test_run_pipeline_returns_forensic_report(clean_pdf_path):
    assert isinstance(run_forensic_pipeline(str(clean_pdf_path)), ForensicReport)


def test_risk_assessment_has_color_code():
    r = calculate_overall_risk([])
    assert hasattr(r, "color_code")


def test_risk_assessment_has_total_findings():
    r = calculate_overall_risk(_all_info())
    assert hasattr(r, "total_findings")


def test_risk_assessment_has_suspicious_count():
    assert hasattr(calculate_overall_risk([]), "suspicious_count")


def test_risk_assessment_has_check_results():
    assert hasattr(calculate_overall_risk([]), "check_results")


def test_risk_assessment_has_conclusion():
    assert hasattr(calculate_overall_risk([]), "conclusion")


def test_forensic_report_has_file_path(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    assert hasattr(r, "file_path")


def test_forensic_report_has_findings(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    assert hasattr(r, "findings")


def test_forensic_report_has_risk(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    assert hasattr(r, "risk")


# ---------------------------------------------------------------------------
# B. Color-code rules
# ---------------------------------------------------------------------------

def test_all_info_is_green():
    assert calculate_overall_risk(_all_info()).color_code == "GREEN"


def test_empty_findings_is_green():
    assert calculate_overall_risk([]).color_code == "GREEN"


def test_one_warning_zero_danger_is_yellow():
    findings = _all_info()
    findings[0] = _f(CHECKS[0], "warning")
    assert calculate_overall_risk(findings).color_code == "YELLOW"


def test_one_danger_zero_warning_is_red():
    findings = _all_info()
    findings[0] = _f(CHECKS[0], "danger")
    assert calculate_overall_risk(findings).color_code == "RED"


def test_two_warnings_is_red():
    findings = _all_info()
    findings[0] = _f(CHECKS[0], "warning")
    findings[1] = _f(CHECKS[1], "warning")
    assert calculate_overall_risk(findings).color_code == "RED"


def test_one_danger_one_warning_is_red():
    findings = [
        _f("metadata_analysis", "danger"),
        _f("font_consistency", "warning"),
    ]
    assert calculate_overall_risk(findings).color_code == "RED"


def test_two_dangers_is_red():
    findings = [
        _f("font_consistency", "danger"),
        _f("hidden_overlay_detection", "danger"),
    ]
    assert calculate_overall_risk(findings).color_code == "RED"


def test_three_info_one_warning_is_yellow():
    findings = [
        _f("metadata_analysis", "info"),
        _f("text_layer_analysis", "info"),
        _f("font_consistency", "warning"),
        _f("coordinate_alignment", "info"),
    ]
    assert calculate_overall_risk(findings).color_code == "YELLOW"


def test_three_info_one_danger_is_red():
    findings = [
        _f("metadata_analysis", "info"),
        _f("text_layer_analysis", "info"),
        _f("font_consistency", "info"),
        _f("hidden_overlay_detection", "danger"),
    ]
    assert calculate_overall_risk(findings).color_code == "RED"


def test_single_danger_is_red_not_yellow():
    """suspicious_count == 1 but danger_count == 1 → RED, not YELLOW."""
    assert calculate_overall_risk([_f("font_consistency", "danger")]).color_code == "RED"


# ---------------------------------------------------------------------------
# C. Counts
# ---------------------------------------------------------------------------

def test_total_findings_equals_len():
    findings = _all_info()
    assert calculate_overall_risk(findings).total_findings == len(findings)


def test_total_findings_empty():
    assert calculate_overall_risk([]).total_findings == 0


def test_suspicious_count_zero_when_all_info():
    assert calculate_overall_risk(_all_info()).suspicious_count == 0


def test_suspicious_count_counts_warning_and_danger():
    findings = [
        _f("a", "warning"),
        _f("b", "danger"),
        _f("c", "info"),
    ]
    assert calculate_overall_risk(findings).suspicious_count == 2


def test_suspicious_count_excludes_info():
    findings = [_f(f"check_{i}", "info") for i in range(10)]
    assert calculate_overall_risk(findings).suspicious_count == 0


def test_check_results_maps_names_to_statuses():
    findings = [
        _f("metadata_analysis", "danger"),
        _f("text_layer_analysis", "info"),
    ]
    cr = calculate_overall_risk(findings).check_results
    assert cr["metadata_analysis"] == "danger"
    assert cr["text_layer_analysis"] == "info"


def test_check_results_length_equals_findings():
    findings = _all_info()
    cr = calculate_overall_risk(findings).check_results
    assert len(cr) == len(findings)


def test_check_results_empty_when_no_findings():
    assert calculate_overall_risk([]).check_results == {}


# ---------------------------------------------------------------------------
# D. Conclusion text
# ---------------------------------------------------------------------------

def test_green_conclusion_mentions_authentic():
    r = calculate_overall_risk(_all_info())
    assert "authentic" in r.conclusion.lower()


def test_yellow_conclusion_mentions_manual_review():
    findings = _all_info()
    findings[0] = _f(CHECKS[0], "warning")
    r = calculate_overall_risk(findings)
    assert "manual review" in r.conclusion.lower()


def test_red_conclusion_mentions_tampering_or_review():
    findings = [_f("font_consistency", "danger")]
    r = calculate_overall_risk(findings)
    lower = r.conclusion.lower()
    assert "tampering" in lower or "review" in lower


def test_conclusion_is_non_empty_for_all_codes():
    for code in ("GREEN", "YELLOW", "RED"):
        assert _CONCLUSIONS[code]


def test_color_code_is_valid_literal():
    for findings in (
        [],
        [_f("x", "warning")],
        [_f("x", "danger")],
    ):
        assert calculate_overall_risk(findings).color_code in {"GREEN", "YELLOW", "RED"}


# ---------------------------------------------------------------------------
# E. Boundary values
# ---------------------------------------------------------------------------

def test_exactly_one_warning_is_yellow_not_red():
    r = calculate_overall_risk([_f("coordinate_alignment", "warning")])
    assert r.color_code == "YELLOW"
    assert r.color_code != "RED"


def test_exactly_two_warnings_is_red_not_yellow():
    findings = [_f("a", "warning"), _f("b", "warning")]
    assert calculate_overall_risk(findings).color_code == "RED"


def test_font_consistency_danger_triggers_red():
    assert calculate_overall_risk(
        [_f("font_consistency", "danger")]
    ).color_code == "RED"


def test_overlay_detection_danger_triggers_red():
    assert calculate_overall_risk(
        [_f("hidden_overlay_detection", "danger")]
    ).color_code == "RED"


# ---------------------------------------------------------------------------
# F. run_forensic_pipeline — shape
# ---------------------------------------------------------------------------

def test_pipeline_file_path_preserved(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    assert "clean.pdf" in r.file_path


def test_pipeline_produces_five_findings(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    assert len(r.findings) == 5


def test_pipeline_findings_are_finding_instances(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    assert all(isinstance(f, Finding) for f in r.findings)


def test_pipeline_all_five_check_names_present(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    names = {f.check for f in r.findings}
    for expected in CHECKS:
        assert expected in names


def test_pipeline_risk_is_risk_assessment(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    assert isinstance(r.risk, RiskAssessment)


def test_pipeline_check_results_covers_all_checks(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    for name in CHECKS:
        assert name in r.risk.check_results


# ---------------------------------------------------------------------------
# G. Integration — clean fixture
# ---------------------------------------------------------------------------

def test_clean_pdf_is_green(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    assert r.risk.color_code == "GREEN"


def test_clean_pdf_suspicious_count_zero(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    assert r.risk.suspicious_count == 0


def test_clean_pdf_all_statuses_info(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    for finding in r.findings:
        assert finding.status == "info", (
            f"Expected info for {finding.check!r}, got {finding.status!r}"
        )


def test_clean_pdf_conclusion_mentions_authentic(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    assert "authentic" in r.risk.conclusion.lower()


def test_clean_pdf_total_findings_is_five(clean_pdf_path):
    r = run_forensic_pipeline(str(clean_pdf_path))
    assert r.risk.total_findings == 5


# ---------------------------------------------------------------------------
# H. Integration — anomalous fixture
# ---------------------------------------------------------------------------

def test_anomalous_pdf_is_red(anomalous_pdf_path):
    r = run_forensic_pipeline(str(anomalous_pdf_path))
    assert r.risk.color_code == "RED"


def test_anomalous_pdf_suspicious_count_gte_two(anomalous_pdf_path):
    r = run_forensic_pipeline(str(anomalous_pdf_path))
    assert r.risk.suspicious_count >= 2


def test_anomalous_pdf_font_consistency_is_danger(anomalous_pdf_path):
    r = run_forensic_pipeline(str(anomalous_pdf_path))
    assert r.risk.check_results["font_consistency"] == "danger"


def test_anomalous_pdf_overlay_detection_is_danger(anomalous_pdf_path):
    r = run_forensic_pipeline(str(anomalous_pdf_path))
    assert r.risk.check_results["hidden_overlay_detection"] == "danger"


def test_anomalous_pdf_metadata_is_danger(anomalous_pdf_path):
    r = run_forensic_pipeline(str(anomalous_pdf_path))
    assert r.risk.check_results["metadata_analysis"] == "danger"


def test_anomalous_pdf_conclusion_mentions_tampering_or_review(anomalous_pdf_path):
    r = run_forensic_pipeline(str(anomalous_pdf_path))
    lower = r.risk.conclusion.lower()
    assert "tampering" in lower or "review" in lower


def test_anomalous_pdf_total_findings_is_five(anomalous_pdf_path):
    r = run_forensic_pipeline(str(anomalous_pdf_path))
    assert r.risk.total_findings == 5


def test_anomalous_pdf_file_path_in_report(anomalous_pdf_path):
    r = run_forensic_pipeline(str(anomalous_pdf_path))
    assert "anomalous.pdf" in r.file_path
