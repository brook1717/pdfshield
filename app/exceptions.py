"""Project-wide custom exceptions."""


class PDFShieldError(Exception):
    """Base exception for all pdfshield errors."""


class PDFParseError(PDFShieldError):
    """Raised when a PDF cannot be opened or its content extracted."""


class PDFValidationError(PDFShieldError):
    """Raised when a PDF fails a structural or forensic validation rule."""
