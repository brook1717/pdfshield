from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# PDF structural extraction
# ---------------------------------------------------------------------------

class PDFMetadata(BaseModel):
    creator: str | None = None
    producer: str | None = None
    creation_date: str | None = None
    mod_date: str | None = None


class TextBlock(BaseModel):
    text: str
    font_name: str
    font_size: float
    x: float = Field(description="Left edge of the character bounding box (pts)")
    y: float = Field(description="Top edge of the character bounding box (pts)")
    bbox: tuple[float, float, float, float] = Field(
        description="(x0, top, x1, bottom) in page coordinate space"
    )
    page_index: int = Field(description="0-based page number")


class PDFStructuralData(BaseModel):
    metadata: PDFMetadata
    page_count: int
    has_text_layer: bool
    raw_text: str
    blocks: list[TextBlock]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

Severity = Literal["info", "warning", "danger"]


class Finding(BaseModel):
    check: str
    status: Severity
    details: list[str]


# ---------------------------------------------------------------------------
# Risk assessment
# ---------------------------------------------------------------------------

ColorCode = Literal["GREEN", "YELLOW", "RED"]


class RiskAssessment(BaseModel):
    color_code: ColorCode
    total_findings: int = Field(description="Total number of check results included")
    suspicious_count: int = Field(
        description="Number of findings whose status is 'warning' or 'danger'"
    )
    check_results: dict[str, Severity] = Field(
        description="Mapping of check name to its severity status"
    )
    conclusion: str = Field(description="Short generated textual conclusion")


class ForensicReport(BaseModel):
    file_path: str
    findings: list[Finding]
    risk: RiskAssessment
