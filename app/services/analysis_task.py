"""
Background forensic analysis task.

This module contains the single callable that FastAPI's
:class:`~fastapi.BackgroundTasks` dispatches after the upload endpoint
returns ``202 Accepted``.  It owns the full status-transition lifecycle for
one job:

    PENDING  →  PROCESSING  →  COMPLETED
                            ↘  FAILED

The task is a plain synchronous function so that FastAPI routes it through
``anyio``'s thread-pool executor.  This keeps the event loop responsive even
during heavy PyMuPDF / pdfplumber processing.
"""
from __future__ import annotations

import logging

from app.db.jobs import COMPLETED, FAILED, PROCESSING, update_job
from app.services.risk_engine import run_forensic_pipeline
from app.utils.annotator import annotate_pdf_anomalies

logger = logging.getLogger(__name__)


def run_analysis_task(job_id: str, file_path: str, filename: str) -> None:
    """
    Execute the forensic pipeline for *file_path* and persist results.

    Parameters
    ----------
    job_id:
        UUID that identifies the job row in the database.
    file_path:
        Absolute filesystem path to the uploaded PDF.
    filename:
        Original filename as submitted by the client (used for logging).

    Side-effects
    ------------
    * Transitions job status PENDING → PROCESSING at task start.
    * On success: persists ``risk_level``, ``annotated_url``, and
      ``results_json`` and sets status to COMPLETED.
    * On any unhandled exception: sets status to FAILED and re-raises
      nothing — the background worker must never propagate exceptions to
      the ASGI event loop.
    """
    logger.info("task[%s]: starting analysis for '%s'", job_id, filename)
    update_job(job_id, status=PROCESSING)

    try:
        report       = run_forensic_pipeline(file_path)
        annotated    = annotate_pdf_anomalies(file_path, report.findings) or None

        update_job(
            job_id,
            status        = COMPLETED,
            risk_level    = report.risk.color_code,
            annotated_url = annotated,
            results_json  = report.model_dump_json(),
        )
        logger.info(
            "task[%s]: completed — risk=%s annotated=%s",
            job_id, report.risk.color_code, annotated,
        )

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "task[%s]: analysis failed for '%s' — %s",
            job_id, filename, exc, exc_info=True,
        )
        update_job(job_id, status=FAILED)
