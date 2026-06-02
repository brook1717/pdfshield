"""
SQLite-backed job state store.

Each forensic analysis request is tracked as a row in the ``jobs`` table.
Status lifecycle:

    PENDING  →  PROCESSING  →  COMPLETED
                            ↘  FAILED

The database file is created automatically on first access so neither the
lifespan hook nor test fixtures need to bootstrap the schema manually — though
calling :func:`init_db` explicitly (e.g. from the FastAPI lifespan) is
encouraged in production for early failure detection.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database location
# ---------------------------------------------------------------------------

#: SQLite file placed at the project root (one level above ``app/``).
DB_PATH: Path = Path(__file__).resolve().parents[2] / "pdfshield.db"

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

PENDING    = "PENDING"
PROCESSING = "PROCESSING"
COMPLETED  = "COMPLETED"
FAILED     = "FAILED"

STATUSES: frozenset[str] = frozenset({PENDING, PROCESSING, COMPLETED, FAILED})

# ---------------------------------------------------------------------------
# Typed result shape (all fields optional so partial records can be returned)
# ---------------------------------------------------------------------------


class JobRecord(TypedDict, total=False):
    job_id:        str
    filename:      str
    status:        str
    risk_level:    str | None
    annotated_url: str | None
    results_json:  str | None
    created_at:    str
    updated_at:    str


# ---------------------------------------------------------------------------
# Lazy initialisation
# ---------------------------------------------------------------------------

_initialized: bool = False


def _ensure_db() -> None:
    """Initialise the schema on the first call; no-op thereafter."""
    global _initialized
    if not _initialized:
        init_db()


def init_db() -> None:
    """
    Create the ``jobs`` table if it does not already exist.

    Safe to call multiple times — the underlying ``CREATE TABLE IF NOT EXISTS``
    is idempotent.
    """
    global _initialized
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH), check_same_thread=False) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id        TEXT PRIMARY KEY,
                filename      TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'PENDING',
                risk_level    TEXT,
                annotated_url TEXT,
                results_json  TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        conn.commit()
    _initialized = True
    logger.info("db: jobs table ready at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    """Open a new connection with :class:`sqlite3.Row` factory."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Public CRUD
# ---------------------------------------------------------------------------

def create_job(job_id: str, filename: str) -> None:
    """
    Insert a new job record with ``status=PENDING``.

    Parameters
    ----------
    job_id:
        UUID string that also names the uploaded file on disk.
    filename:
        Original filename as submitted by the client.
    """
    _ensure_db()
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (job_id, filename, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, filename, PENDING, now, now),
        )
        conn.commit()
    logger.info("db: job created — id=%s filename=%s", job_id, filename)


def update_job(
    job_id: str,
    *,
    status: str,
    risk_level:    str | None = None,
    annotated_url: str | None = None,
    results_json:  str | None = None,
) -> None:
    """
    Update mutable fields on an existing job record.

    Only non-``None`` keyword arguments overwrite the stored value;
    passing ``None`` leaves the current column value unchanged
    (via ``COALESCE``).

    Parameters
    ----------
    job_id:
        Target job UUID.
    status:
        New status — must be one of :data:`STATUSES`.
    risk_level:
        ``"GREEN"`` / ``"YELLOW"`` / ``"RED"`` color code from the report.
    annotated_url:
        Relative URL of the first annotated PNG, if produced.
    results_json:
        Full :class:`~app.models.schemas.ForensicReport` serialised as JSON.

    Raises
    ------
    ValueError
        When *status* is not a recognised value.
    """
    if status not in STATUSES:
        raise ValueError(f"Invalid job status: {status!r}")
    _ensure_db()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status        = ?,
                risk_level    = COALESCE(?, risk_level),
                annotated_url = COALESCE(?, annotated_url),
                results_json  = COALESCE(?, results_json),
                updated_at    = ?
            WHERE job_id = ?
            """,
            (status, risk_level, annotated_url, results_json, _now(), job_id),
        )
        conn.commit()
    logger.info("db: job updated — id=%s status=%s", job_id, status)


def get_job(job_id: str) -> JobRecord | None:
    """
    Return the job record as a plain ``dict``, or ``None`` if not found.

    The ``results_json`` column is included; callers that only need the status
    summary should discard it to avoid loading large payloads unnecessarily.
    """
    _ensure_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    return dict(row) if row is not None else None
