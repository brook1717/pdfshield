"""
PDFShield application entry point.

Responsibilities
----------------
* Configure structured application logging (console + rotating file).
* Bootstrap FastAPI with CORS, static files, and routers.
* Register global exception handlers so no uncaught error ever returns a raw
  traceback to the browser — PDF parse failures, structural anomalies, and
  programming faults all map to clean, user-visible error dialogs.
"""
from __future__ import annotations

import logging
import logging.config
import logging.handlers  # noqa: F401 — needed for dictConfig class references
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.endpoints import page_router, router

# ---------------------------------------------------------------------------
# Runtime directories — created eagerly so the logging file handler can open
# ---------------------------------------------------------------------------

BASE_DIR    = Path(__file__).resolve().parent   # app/
UPLOADS_DIR = BASE_DIR.parent / "uploads"
LOGS_DIR    = BASE_DIR.parent / "logs"

for _d in (UPLOADS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Structured logging  (console + rotating file, scoped per module)
# ---------------------------------------------------------------------------

LOGGING_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "detailed": {
            "format": "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "detailed",
            "stream": "ext://sys.stdout",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "detailed",
            "filename": str(LOGS_DIR / "pdfshield.log"),
            "maxBytes": 5_242_880,   # 5 MB per file
            "backupCount": 3,
            "encoding": "utf-8",
        },
    },
    "loggers": {
        "app": {
            "level": "INFO",
            "handlers": ["console", "file"],
            "propagate": False,
        },
        "uvicorn": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
        "uvicorn.error": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
        "uvicorn.access": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
    },
    "root": {
        "level": "WARNING",
        "handlers": ["console"],
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger("app.main")

# ---------------------------------------------------------------------------
# Templates (module-level so error handlers can render pages without importing
# from endpoints, which would create a circular dependency)
# ---------------------------------------------------------------------------

templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[override]
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("PDFShield started  — uploads: %s  logs: %s", UPLOADS_DIR, LOGS_DIR)
    yield
    logger.info("PDFShield shutting down")


app = FastAPI(
    title="pdfshield",
    description="PDF forensic analysis and risk detection API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Error-boundary helpers
# ---------------------------------------------------------------------------


def _is_json_api_path(path: str) -> bool:
    """
    Return ``True`` for all ``/api/v1/*`` routes.

    Every route under ``/api/v1/`` receives JSON error responses from the
    global handlers.  ``/api/v1/upload`` serves HTML on *success*, but its
    validation errors (400) are raised as :class:`~fastapi.HTTPException`
    and correctly return JSON to programmatic callers.  Pipeline failures
    in that route are caught and rendered as HTML *directly inside the handler*,
    so they never reach this global boundary.
    """
    return path.startswith("/api/v1/")


def _render_error_page(
    request: Request, message: str, status_code: int
) -> HTMLResponse:
    """
    Render ``index.html`` with an inline error banner.

    Falls back to a bare HTML string if the template itself is unavailable,
    so the server always returns *some* human-readable response.
    """
    try:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"error": message},
            status_code=status_code,
        )
    except Exception:
        logger.exception("Template rendering failed while building error response")
        return HTMLResponse(
            content=(
                "<!doctype html><html><head><title>PDFShield — Error</title></head>"
                "<body style='font-family:sans-serif;max-width:600px;margin:4rem auto'>"
                f"<h2>Error {status_code}</h2>"
                f"<p>{message}</p>"
                "<p><a href='/'>&#8592; Return to upload</a></p>"
                "</body></html>"
            ),
            status_code=status_code,
        )


# ---------------------------------------------------------------------------
# Global exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse | HTMLResponse:
    """
    Route HTTP errors to the appropriate response format.

    * **JSON** for pure-API paths (``/api/v1/health``, ``/api/v1/export/*``).
    * **HTML error dialog** for page-serving paths (``/``, ``/api/v1/upload``).
    """
    if _is_json_api_path(request.url.path):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )
    message = exc.detail if isinstance(exc.detail, str) else "An error occurred."
    logger.warning(
        "HTTP %s on %s — %s", exc.status_code, request.url.path, message
    )
    return _render_error_page(request, message, exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse | HTMLResponse:
    """
    Translate Pydantic / FastAPI request-validation failures.

    * **JSON 422** for API paths.
    * **Human-readable inline dialog** for browser page paths.
    """
    if _is_json_api_path(request.url.path):
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    errors = exc.errors()
    first_msg = errors[0].get("msg", "Invalid request.") if errors else "Invalid request."
    logger.warning("Validation error on %s: %s", request.url.path, errors)
    return _render_error_page(request, f"Validation error: {first_msg}", 422)


@app.exception_handler(Exception)
async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse | HTMLResponse:
    """
    Catch-all boundary: ensures no raw traceback ever reaches the browser.

    Logs the full stack trace at ERROR level and returns an opaque, safe
    message — server internals are never exposed to the client.
    """
    logger.exception("Unhandled exception on %s", request.url.path)
    if _is_json_api_path(request.url.path):
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error."},
        )
    return _render_error_page(
        request,
        "An unexpected server error occurred. Please try again or upload a different file.",
        500,
    )


# ---------------------------------------------------------------------------
# Static files and routers
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

app.include_router(page_router)
app.include_router(router, prefix="/api/v1")
