from pydantic import BaseModel
from typing import Any


class AnalysisRequest(BaseModel):
    filename: str


class AnalysisResponse(BaseModel):
    filename: str
    risk_score: float
    findings: list[dict[str, Any]]


class UploadResponse(BaseModel):
    file_id: str
    original_filename: str
    storage_path: str
