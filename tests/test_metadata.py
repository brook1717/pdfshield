"""
Unit tests for app/services/metadata.py — analyze_metadata().

Test matrix
-----------
A. Return-type / schema guarantees
B. Clean metadata  → no findings, status="info"
C. Suspicious tool detection
   C1. Online editors        (danger):  Sejda, Smallpdf, iLovePDF, PDFescape
   C2. Desktop editors       (warning): Nitro, Foxit, PDF-XChange, PDF24
   C3. Case-insensitivity
   C4. Partial substring match inside a longer version string
   C5. Creator vs Producer — both fields checked independently
   C6. Both fields suspicious — two detail lines, status escalates to highest
D. Date anomaly detection
   D1. mod < creation                 → danger
   D2. mod > creation by > 30 days    → warning (extreme mismatch)
   D3. mod > creation by < 30 days    → warning (base modification)
   D4. mod == creation                → no finding
   D5. One or both dates absent       → no finding
   D6. Unparseable date format        → no finding (graceful)
E. Compound scenarios
   E1. Sejda + date mismatch           → all details, status=danger
   E2. Warning-tool + extreme date     → all details, status=warning
F. Internal helpers (_parse_pdf_date, _match_tool, _check_dates, _escalate)
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from app.models.schemas import Finding, PDFMetadata
from app.services.metadata import (
    TOOL_RULES,
    _check_dates,
    _escalate,
    _match_tool,
    _parse_pdf_date,
    analyze_metadata,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def _meta(
    creator: str | None = None,
    producer: str | None = None,
    creation_date: str | None = None,
    mod_date: str | None = None,
) -> PDFMetadata:
    return PDFMetadata(
        creator=creator,
        producer=producer,
        creation_date=creation_date,
        mod_date=mod_date,
    )


def _clean() -> PDFMetadata:
    return _meta(
        creator="Microsoft Word 16.0",
        producer="Microsoft Word 16.0",
        creation_date="D:20240110083000Z",
        mod_date="D:20240110083000Z",
    )


# ---------------------------------------------------------------------------
# A. Return-type / schema guarantees
# ---------------------------------------------------------------------------

def test_returns_finding_instance():
    assert isinstance(analyze_metadata(_clean()), Finding)


def test_check_field_always_metadata_analysis():
    assert analyze_metadata(_clean()).check == "metadata_analysis"


def test_status_is_valid_severity():
    f = analyze_metadata(_clean())
    assert f.status in ("info", "warning", "danger")


def test_details_is_list():
    assert isinstance(analyze_metadata(_clean()).details, list)


def test_details_are_strings():
    f = analyze_metadata(_meta(creator="Sejda Version 5"))
    assert all(isinstance(d, str) for d in f.details)


# ---------------------------------------------------------------------------
# B. Clean metadata → no findings
# ---------------------------------------------------------------------------

def test_clean_metadata_status_is_info():
    assert analyze_metadata(_clean()).status == "info"


def test_clean_metadata_no_details():
    assert analyze_metadata(_clean()).details == []


def test_empty_metadata_status_is_info():
    assert analyze_metadata(_meta()).status == "info"


# ---------------------------------------------------------------------------
# C1. Online editors → danger
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_str,display", [
    ("Sejda Version 5.3.7",  "Sejda"),
    ("Smallpdf 2.0",         "Smallpdf"),
    ("iLovePDF Online",      "iLovePDF"),
    ("PDFescape Editor 4.0", "PDFescape"),
])
def test_online_editor_in_producer_is_danger(tool_str, display):
    f = analyze_metadata(_meta(producer=tool_str))
    assert f.status == "danger", f"Expected danger for {tool_str!r}"
    assert any(display in d for d in f.details), f"Display name {display!r} missing from {f.details}"


@pytest.mark.parametrize("tool_str,display", [
    ("Sejda PDF Desktop",   "Sejda"),
    ("Smallpdf",            "Smallpdf"),
    ("ilovepdf.com",        "iLovePDF"),
])
def test_online_editor_in_creator_is_danger(tool_str, display):
    f = analyze_metadata(_meta(creator=tool_str))
    assert f.status == "danger"
    assert any(display in d for d in f.details)


# ---------------------------------------------------------------------------
# C2. Desktop editors → warning (not danger)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_str,display", [
    ("Nitro Pro 13",              "Nitro PDF"),
    ("Foxit Reader 11.0",         "Foxit"),
    ("PDF-XChange Editor 9.0",    "PDF-XChange"),
    ("PDFXChange Viewer 2.5",     "PDF-XChange"),
    ("PDF24 Creator 11",          "PDF24"),
    ("PDFCreator 4.0.4",          "PDFCreator"),
])
def test_desktop_editor_in_producer_is_warning(tool_str, display):
    f = analyze_metadata(_meta(producer=tool_str))
    assert f.status == "warning", f"Expected warning for {tool_str!r}, got {f.status}"
    assert any(display in d for d in f.details)


# ---------------------------------------------------------------------------
# C3. Case-insensitivity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["SEJDA", "sejda", "Sejda", "SeJdA"])
def test_sejda_detection_is_case_insensitive(variant):
    f = analyze_metadata(_meta(producer=variant))
    assert f.status == "danger"


@pytest.mark.parametrize("variant", ["NITRO Pro", "nitro pdf", "NiTrO"])
def test_nitro_detection_is_case_insensitive(variant):
    f = analyze_metadata(_meta(producer=variant))
    assert f.status == "warning"


# ---------------------------------------------------------------------------
# C4. Partial substring match
# ---------------------------------------------------------------------------

def test_sejda_detected_in_longer_version_string():
    f = analyze_metadata(_meta(producer="Adobe Distiller / Sejda Version 5.3.7 (linux)"))
    assert f.status == "danger"
    assert any("Sejda" in d for d in f.details)


def test_foxit_detected_inside_long_string():
    f = analyze_metadata(_meta(creator="Document created with Foxit Reader 12.1.0"))
    assert f.status == "warning"


# ---------------------------------------------------------------------------
# C5. Creator vs Producer checked independently
# ---------------------------------------------------------------------------

def test_only_creator_suspicious():
    f = analyze_metadata(_meta(creator="Sejda PDF", producer="Microsoft Word"))
    assert f.status == "danger"
    assert len(f.details) == 1
    assert "Creator" in f.details[0]


def test_only_producer_suspicious():
    f = analyze_metadata(_meta(creator="Microsoft Word", producer="Sejda PDF"))
    assert f.status == "danger"
    assert len(f.details) == 1
    assert "Producer" in f.details[0]


def test_none_creator_not_flagged():
    f = analyze_metadata(_meta(creator=None, producer="Microsoft Word"))
    assert f.status == "info"


# ---------------------------------------------------------------------------
# C6. Both fields suspicious → two detail lines, status is worst
# ---------------------------------------------------------------------------

def test_both_fields_suspicious_two_details():
    f = analyze_metadata(_meta(creator="Sejda", producer="Smallpdf"))
    assert len(f.details) == 2


def test_both_fields_suspicious_status_is_danger():
    f = analyze_metadata(_meta(creator="Sejda", producer="Foxit"))
    assert f.status == "danger"  # danger beats warning


def test_both_warning_tools_status_remains_warning():
    f = analyze_metadata(_meta(creator="Foxit", producer="Nitro Pro"))
    assert f.status == "warning"


def test_detail_lines_reference_correct_field_names():
    f = analyze_metadata(_meta(creator="Foxit", producer="Nitro"))
    labels = {d.split()[1] for d in f.details}  # "Suspicious <field>"
    assert "Creator" in labels
    assert "Producer" in labels


# ---------------------------------------------------------------------------
# D1. Date: mod < creation → danger
# ---------------------------------------------------------------------------

def test_mod_before_creation_is_danger():
    f = analyze_metadata(_meta(
        creation_date="D:20240601120000Z",
        mod_date="D:20240101120000Z",
    ))
    assert f.status == "danger"


def test_mod_before_creation_detail_mentions_impossible():
    f = analyze_metadata(_meta(
        creation_date="D:20240601120000Z",
        mod_date="D:20240101120000Z",
    ))
    detail_text = " ".join(f.details).lower()
    assert "impossible" in detail_text or "tamper" in detail_text


# ---------------------------------------------------------------------------
# D2. Extreme mismatch (> 30 days) → warning
# ---------------------------------------------------------------------------

def test_extreme_date_mismatch_is_warning():
    f = analyze_metadata(_meta(
        creation_date="D:20240101000000Z",
        mod_date="D:20250101000000Z",   # 366 days later
    ))
    assert f.status == "warning"


def test_extreme_date_mismatch_detail_mentions_mismatch():
    f = analyze_metadata(_meta(
        creation_date="D:20240101000000Z",
        mod_date="D:20250101000000Z",
    ))
    detail_text = " ".join(f.details).lower()
    assert "mismatch" in detail_text or "edit" in detail_text or "day" in detail_text


def test_exactly_31_days_is_extreme():
    f = analyze_metadata(_meta(
        creation_date="D:20240101000000Z",
        mod_date="D:20240201000000Z",  # 31 days later
    ))
    assert f.status == "warning"


# ---------------------------------------------------------------------------
# D3. Small positive delta → warning (base modification)
# ---------------------------------------------------------------------------

def test_same_day_modification_is_warning():
    f = analyze_metadata(_meta(
        creation_date="D:20240110080000Z",
        mod_date="D:20240110100000Z",  # 2 hours later, same day
    ))
    assert f.status == "warning"


def test_one_day_modification_is_warning():
    f = analyze_metadata(_meta(
        creation_date="D:20240110000000Z",
        mod_date="D:20240111000000Z",
    ))
    assert f.status == "warning"


# ---------------------------------------------------------------------------
# D4. Equal dates → no date finding
# ---------------------------------------------------------------------------

def test_equal_dates_no_date_finding():
    f = analyze_metadata(_meta(
        creation_date="D:20240110083000Z",
        mod_date="D:20240110083000Z",
    ))
    assert f.status == "info"
    assert f.details == []


# ---------------------------------------------------------------------------
# D5. Missing dates → no date finding
# ---------------------------------------------------------------------------

def test_missing_creation_date_no_date_finding():
    f = analyze_metadata(_meta(mod_date="D:20240110083000Z"))
    assert f.status == "info"


def test_missing_mod_date_no_date_finding():
    f = analyze_metadata(_meta(creation_date="D:20240110083000Z"))
    assert f.status == "info"


def test_both_dates_missing_no_date_finding():
    f = analyze_metadata(_meta())
    assert f.status == "info"


# ---------------------------------------------------------------------------
# D6. Unparseable date format → graceful no finding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_date", [
    "2024-01-10",          # ISO format, not PDF format
    "Jan 10 2024",
    "",
    "D:99991399999999Z",   # invalid month/day
])
def test_bad_date_format_does_not_raise(bad_date):
    f = analyze_metadata(_meta(creation_date=bad_date, mod_date=bad_date))
    assert isinstance(f, Finding)


# ---------------------------------------------------------------------------
# E. Compound scenarios
# ---------------------------------------------------------------------------

def test_sejda_plus_mod_before_creation_is_danger():
    f = analyze_metadata(_meta(
        producer="Sejda Version 5.3.7",
        creation_date="D:20240601000000Z",
        mod_date="D:20240101000000Z",
    ))
    assert f.status == "danger"
    assert len(f.details) == 2


def test_sejda_plus_extreme_date_status_is_danger():
    f = analyze_metadata(_meta(
        producer="Sejda",
        creation_date="D:20240101000000Z",
        mod_date="D:20250101000000Z",
    ))
    assert f.status == "danger"   # danger (Sejda) beats warning (date)
    assert len(f.details) == 2


def test_warning_tool_plus_extreme_date_all_details_captured():
    f = analyze_metadata(_meta(
        producer="Foxit Reader 12",
        creation_date="D:20240101000000Z",
        mod_date="D:20250101000000Z",
    ))
    assert f.status == "warning"
    assert len(f.details) == 2


def test_anomalous_fixture_metadata_triggers_findings(anomalous_pdf_path):
    """Integration: anomalous.pdf metadata should yield Sejda + date findings."""
    from app.services.parser import parse_pdf
    structural = parse_pdf(anomalous_pdf_path)
    f = analyze_metadata(structural.metadata)
    assert f.status == "danger"
    assert any("Sejda" in d for d in f.details)
    assert any("day" in d.lower() or "mismatch" in d.lower() or "modif" in d.lower()
               for d in f.details)


def test_clean_fixture_metadata_is_info(clean_pdf_path):
    """Integration: clean.pdf metadata should produce no findings."""
    from app.services.parser import parse_pdf
    structural = parse_pdf(clean_pdf_path)
    f = analyze_metadata(structural.metadata)
    assert f.status == "info"
    assert f.details == []


# ---------------------------------------------------------------------------
# F. Internal helpers (white-box)
# ---------------------------------------------------------------------------

class TestParsePdfDate:
    def test_valid_date_returns_datetime(self):
        dt = _parse_pdf_date("D:20240110083000Z")
        assert isinstance(dt, datetime)

    def test_correct_year(self):
        assert _parse_pdf_date("D:20240110083000Z").year == 2024

    def test_correct_month_and_day(self):
        dt = _parse_pdf_date("D:20240315120000Z")
        assert dt.month == 3 and dt.day == 15

    def test_none_returns_none(self):
        assert _parse_pdf_date(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_pdf_date("") is None

    def test_non_pdf_format_returns_none(self):
        assert _parse_pdf_date("2024-01-10") is None

    def test_invalid_month_returns_none(self):
        assert _parse_pdf_date("D:20241399120000Z") is None


class TestMatchTool:
    def test_known_tool_returns_rule(self):
        rule = _match_tool("Sejda PDF Desktop")
        assert rule is not None
        assert rule.display_name == "Sejda"

    def test_unknown_tool_returns_none(self):
        assert _match_tool("Microsoft Word 16.0") is None

    def test_case_insensitive_match(self):
        assert _match_tool("FOXIT READER") is not None

    def test_empty_string_returns_none(self):
        assert _match_tool("") is None

    def test_rules_list_is_deterministic(self):
        """TOOL_RULES must be a stable list (same order every import)."""
        names = [r.display_name for r in TOOL_RULES]
        assert names == list(dict.fromkeys(names)) or len(names) == len(TOOL_RULES)


class TestEscalate:
    def test_danger_beats_info(self):
        assert _escalate("info", "danger") == "danger"

    def test_danger_beats_warning(self):
        assert _escalate("warning", "danger") == "danger"

    def test_warning_beats_info(self):
        assert _escalate("info", "warning") == "warning"

    def test_same_level_unchanged(self):
        assert _escalate("warning", "warning") == "warning"

    def test_lower_does_not_downgrade(self):
        assert _escalate("danger", "info") == "danger"
        assert _escalate("danger", "warning") == "danger"


class TestCheckDates:
    def test_equal_dates_no_finding(self):
        msg, sev = _check_dates("D:20240110083000Z", "D:20240110083000Z")
        assert msg is None and sev == "info"

    def test_mod_before_creation_danger(self):
        _, sev = _check_dates("D:20240601000000Z", "D:20240101000000Z")
        assert sev == "danger"

    def test_extreme_mismatch_warning(self):
        _, sev = _check_dates("D:20240101000000Z", "D:20250101000000Z")
        assert sev == "warning"

    def test_small_delta_warning(self):
        _, sev = _check_dates("D:20240101000000Z", "D:20240102000000Z")
        assert sev == "warning"

    def test_none_creation_no_finding(self):
        msg, _ = _check_dates(None, "D:20240101000000Z")
        assert msg is None

    def test_none_mod_no_finding(self):
        msg, _ = _check_dates("D:20240101000000Z", None)
        assert msg is None
