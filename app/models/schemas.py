from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

class UploadResponse(BaseModel):
    file_id: str
    original_filename: str
    storage_path: str


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

class AnalysisRequest(BaseModel):
    filename: str


class AnalysisResponse(BaseModel):
    filename: str
    risk_score: float
    findings: list[dict[str, Any]]
