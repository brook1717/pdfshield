# PDFShield 🛡️

### Deterministic forensic analysis engine for detecting tampered PDF documents

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-552%20passing-brightgreen.svg)](#testing)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED.svg)](docker-compose.yml)

---

## The Problem

Invoice fraud and document manipulation cost businesses billions annually. The
attack surface is precise: a fraudster opens a legitimate PDF in Sejda,
Smallpdf, or a similar online editor, pastes a new account number or amount
over the original, re-exports, and submits. The visual output is pixel-perfect.
No standard PDF viewer raises an alarm.

PDFShield tears the document apart at the byte level: metadata timestamps,
font fingerprints, character-coordinate distributions, and raw drawing-command
streams — five independent, deterministic checks that expose the forensic traces
those edits invariably leave behind.

---

## Architecture

```
Browser / API client
      │
      │  POST /api/v1/upload  (multipart/form-data)
      ▼
┌───────────────────────────────────────────────────────────┐
│  ASGI Stack  (Uvicorn + FastAPI)                          │
│                                                           │
│  ┌─────────────────────────┐  ┌────────────────────────┐  │
│  │  SecurityHeadersMiddleware│  │  slowapi Rate Limiter  │  │
│  │  CSP · nosniff · DENY   │  │  5 uploads / min / IP  │  │
│  └─────────────────────────┘  └────────────────────────┘  │
│                                                           │
│  POST /upload                                             │
│    ├─ secure_filename()          path-traversal guard     │
│    ├─ MIME + extension check     dual-layer validation    │
│    ├─ 10 MB size guard           read before write        │
│    ├─ dest.write_bytes(contents)                          │
│    ├─ INSERT jobs (PENDING)  ←── SQLite pdfshield.db      │
│    ├─ BackgroundTask.add_task(run_analysis_task)          │
│    └─ 202 Accepted  { job_id, status, filename }          │
│                                                           │
│  GET /api/v1/status/{job_id}  ◄── JS polls every 1.5 s   │
│  GET /report/{job_id}         ◄── server-rendered HTML    │
│  GET /api/v1/export/{job_id}  ◄── JSON attachment         │
└───────────────────────────────────────────────────────────┘
                │
                │  BackgroundTask  (anyio thread-pool executor)
                ▼
┌───────────────────────────────────────────────────────────┐
│  Forensic Pipeline  (synchronous, CPU-bound)              │
│                                                           │
│  UPDATE jobs → PROCESSING                                 │
│                                                           │
│  parse_pdf(path)  ──►  PDFStructuralData                  │
│       │                 (metadata · blocks · raw_text)    │
│       │                                                   │
│       ├──►  metadata.analyze_metadata()                   │
│       ├──►  text_layer.analyze_text_layer()               │
│       ├──►  font_analysis.analyze_font_consistency()      │
│       ├──►  coordinate_analysis.analyze_coord_alignment() │
│       └──►  overlay_detection.detect_hidden_overlays()    │
│                                                           │
│  calculate_overall_risk()  ──►  GREEN / YELLOW / RED      │
│  annotate_pdf_anomalies()  ──►  PNG  app/static/annotated/│
│                                                           │
│  UPDATE jobs → COMPLETED  (risk_level · results_json)     │
│                    ╎  on exception:                       │
│                    └─ UPDATE jobs → FAILED                │
│                       Path(file_path).unlink()  ◄ finally │
└───────────────────────────────────────────────────────────┘
                │
                ▼
         SQLite  pdfshield.db
         ┌──────────────────────────────────────────┐
         │ jobs                                     │
         │  job_id · filename · status              │
         │  risk_level · annotated_url              │
         │  results_json · created_at · updated_at  │
         └──────────────────────────────────────────┘
```

**Key design decisions**

- The pipeline runs in `anyio`'s thread-pool executor — heavy PyMuPDF and
  pdfplumber work never blocks the ASGI event loop.
- The upload endpoint returns `202 Accepted` *before* a single byte of analysis
  runs, keeping P99 upload latency sub-100 ms regardless of document complexity.
- SQLite is intentionally chosen over a message broker: the workload is
  single-server, the access pattern is point-lookups by UUID, and the dependency
  footprint stays minimal. Swap in Postgres + Celery when horizontal scale is
  required — the `app/db/jobs.py` CRUD interface is the only boundary that
  changes.

---

## Forensic Mechanics

Five independent rule-based checks, each returning a typed `Finding`
(`status: "info" | "warning" | "danger"`, `details: list[str]`).

### 1 · Metadata fingerprint & date anomaly  
`app/services/metadata.py`

Inspects the four standard PDF metadata fields (`Creator`, `Producer`,
`CreationDate`, `ModDate`). Two signal classes:

- **Tool fingerprints** — an ordered rule table (`TOOL_RULES`) of known
  online editing tools (Sejda, Smallpdf, iLovePDF, PDF2Go, …). Each rule maps
  a case-insensitive substring match to a severity (`"warning"` or `"danger"`).
  Online editors are `"danger"` because they are the primary instrument for
  invoice manipulation.
- **Date anomaly** — if `ModDate < CreationDate` the document has been
  back-dated (physically impossible without tampering). A modification more than
  30 days after creation is a secondary `"warning"`. Both checks parse the PDF
  `D:YYYYMMDDHHmmSS` date format to UTC-aware `datetime` objects.

The finding reflects the *worst* severity encountered across all signals.

---

### 2 · Text-layer presence  
`app/services/text_layer.py`

A scanned-and-reprinted PDF carries no selectable text; a genuine digital PDF
always does. This check confirms the document has an extractable text layer via
pdfplumber. Absence is a `"warning"` — it does not prove manipulation but
removes the ability to run checks 3–4.

---

### 3 · Font consistency across numeric spans  
`app/services/font_analysis.py`

The canonical tamper pattern is replacing one digit in an amount field with a
character pasted from a different application. That character retains its
source font even when colour and size appear identical to the human eye.

**Algorithm**

1. Group `TextBlock` objects by page and *bottom-edge baseline*
   (`bbox[3]` ± `BASELINE_TOLERANCE = 1.5 pt`). The bottom edge is stable
   across font-size changes; the top edge is not.
2. Within each sorted baseline, find **numeric spans**: maximal runs of
   characters in `NUMERIC_CHARS` (`[0-9.,$ £€¥%]`) of at least
   `MIN_SPAN_LENGTH = 4` characters containing at least one digit.
3. Compute the *consensus* font family and size for the span via
   `Counter.most_common(1)`.
4. Flag any character whose font family differs from the consensus, or whose
   size deviates by more than `FONT_SIZE_TOLERANCE = 0.5 pt`.
5. De-duplicate within-span; preserve cross-span duplicates (distinct fields).

A single deviation is `"danger"` — font family mismatch is a hard forensic
indicator.

---

### 4 · Coordinate alignment  
`app/services/coordinate_analysis.py`

Manually pasted characters land at slightly different vertical positions or
create irregular horizontal spacing. Both are invisible to the eye but
detectable in the coordinate stream.

**Algorithm**

1. Same baseline-grouping as check 3.
2. **Y-shift**: compute the *median* `block.y` across the span.  
   Any character deviating by more than `Y_SHIFT_TOLERANCE = 2.0 pt` is
   flagged.
3. **X-spacing**: collect consecutive x-increments across the span.
   Compute the *median* advance. Flag gaps exceeding
   `X_GAP_HIGH_RATIO × median` (paste gap) or below
   `X_GAP_LOW_RATIO × median` (compressed/overlapping characters).

Using the *median* (rather than mean) makes both checks resistant to a single
extreme outlier. This check emits `"warning"` — it is probabilistic, not
deterministic.

---

### 5 · Hidden overlay / white-mask detection  
`app/services/overlay_detection.py`

The lowest-level forgery technique: draw an opaque white rectangle over
existing text, then render replacement text on top. The original content is
invisible but remains in the PDF byte stream.

**Algorithm** (via `fitz.Page.get_drawings()`)

1. Extract all text-span bounding boxes from `page.get_text("dict")`.
2. Iterate drawing commands. For each filled path (`type ∈ {"f", "fs"}`):
   - **White/near-white guard**: all RGB channels ≥ `WHITE_CHANNEL_THRESHOLD = 0.85`.
   - **Opacity guard**: `fill_opacity ≥ MIN_FILL_OPACITY = 0.9` (semi-transparent
     decorative elements are not masks).
   - **Area guard**: bounding-rect area ≥ `MIN_RECT_AREA = 100 sq pt` (excludes
     hairlines and tick marks).
3. For each qualifying drawing, check overlap with every text span.
   If the intersection covers ≥ `MIN_OVERLAP_RATIO = 0.5` of the span area →
   the drawing is masking text → `"danger"` finding on that page.

The pure-logic helpers `_is_masking_fill` and `_check_coverage` are importable
in isolation, enabling unit tests that do not require a real PDF file.

---

### Risk triage

```
┌─────────────────────────────────────────────────────────────┐
│  suspicious_count = danger_count + warning_count            │
│                                                             │
│  suspicious_count == 0              →  GREEN                │
│  suspicious_count == 1, danger == 0 →  YELLOW               │
│  danger >= 1  OR  suspicious >= 2   →  RED                  │
└─────────────────────────────────────────────────────────────┘
```

The first-match ordering means a single `"danger"` finding always escalates
directly to RED regardless of the warning count, preventing severity dilution.

---

### Visual annotation  
`app/utils/annotator.py`

On `COMPLETED` jobs where findings include localised anomalies, PyMuPDF renders
the page to a 2× DPI PNG and draws translucent red rectangles over the exact
character bounding boxes. The resulting image is served from
`/static/annotated/` and embedded directly in the HTML report, giving a
reviewer a pixel-accurate map of where in the document the forensic signals
were detected.

---

## Production Design Highlights

### Async job lifecycle

```
PENDING  →  PROCESSING  →  COMPLETED
                        ↘  FAILED
```

Status is persisted in SQLite before the background task dispatches and updated
atomically at each state transition. `GET /api/v1/status/{job_id}` is a
sub-millisecond point-lookup — the polling browser tab imposes negligible load.
The job record stores `results_json` (full `ForensicReport`) alongside the
`annotated_url`, so the report page is a single DB read with no re-analysis.

### Security hardening

| Layer | Mechanism |
|---|---|
| **Request throttling** | `slowapi` token-bucket limiter — 5 uploads / minute / IP, `429` on breach |
| **Filename sanitisation** | `secure_filename()` — unicode normalise → ASCII, `os.path.basename`, allow-list regex, leading-dot strip, `..` collapse, 255-byte POSIX cap |
| **Upload validation** | Dual MIME + extension check, full content read before any disk I/O, 10 MB hard cap |
| **File write guard** | `try/except` around `create_job()` — `dest.unlink(missing_ok=True)` on DB failure, preventing orphaned uploads |
| **Failure cleanup** | `_failed` flag + `finally` block in background task — removes the uploaded PDF if the pipeline raises |
| **Browser headers** | `SecurityHeadersMiddleware` (outermost) injects `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, strict `Content-Security-Policy`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-XSS-Protection: 0` on **every** response |
| **Error boundary** | Global `Exception` handler returns opaque `500` — no stack traces ever reach the client |

### Logging

Structured rotating-file + console logging via `logging.dictConfig`.  
Each service module acquires its own named logger (`app.services.font_analysis`,
etc.), producing trace lines like:

```
2024-06-01T14:23:05 [INFO    ] app.services.risk_engine — risk_engine: color=RED  danger=1  warning=0  total=5
```

Log files rotate at 5 MB with 3 backup generations, bound to `./logs/` via a
Docker volume mount for host-side access without `docker exec`.

### Schema-first data model

All inter-layer contracts are typed `pydantic.BaseModel` objects:

```python
PDFStructuralData  →  metadata · page_count · has_text_layer · raw_text · blocks
Finding            →  check · status: Severity · details: list[str]
ForensicReport     →  file_path · findings · risk: RiskAssessment
```

The pipeline never passes raw dicts between modules. The `ForensicReport` is
serialised with `model_dump_json()` and stored verbatim in SQLite, then
deserialised with `model_validate_json()` on report retrieval — a round-trip
contract enforced by the type system.

### Infrastructure as Code

`docker-compose.yml` is the single source of truth for the runtime environment:
named volumes for `uploads/` and `logs/`, a `curl`-based healthcheck against
`/api/v1/health` (interval 30 s, 3 retries, 15 s start grace period), and
`restart: unless-stopped` for unattended recovery.

---

## Quick Start

### Native Python

```bash
# 1. Clone and enter the project
git clone https://github.com/brook1717/pdfshield.git
cd pdfshield

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the server
uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000` → drag-and-drop a PDF → the UI polls for
completion and redirects to the visual report automatically.

**API-only usage**

```bash
# Submit a PDF
JOB=$(curl -s -X POST http://localhost:8000/api/v1/upload \
  -F "file=@invoice.pdf" | jq -r .job_id)

# Poll until COMPLETED
curl http://localhost:8000/api/v1/status/$JOB

# Download the full JSON report
curl -OJ http://localhost:8000/api/v1/export/$JOB
```

---

### Docker Compose

```bash
# Build and start (runs in the background)
docker compose up --build -d

# Tail logs
docker compose logs -f

# Healthcheck status
docker inspect --format='{{.State.Health.Status}}' pdfshield_app

# Stop and remove containers (volumes are preserved)
docker compose down
```

The `uploads/` and `logs/` directories are bind-mounted from the host, so
forensic artefacts and rotating log files persist across container restarts and
image rebuilds.

---

## Testing

```bash
# Full suite (552 tests, ~35 seconds on a laptop)
python -m pytest tests/ -q

# Single module
python -m pytest tests/test_security.py -v

# With coverage
python -m pytest tests/ --cov=app --cov-report=term-missing
```

**Test modules**

| Module | Coverage area |
|---|---|
| `test_metadata.py` | Tool-fingerprint rules, date-anomaly edge cases |
| `test_font_analysis.py` | Baseline grouping, majority-vote consensus, tolerance boundaries |
| `test_coordinate_analysis.py` | Median y-shift, x-gap ratio, span detection |
| `test_overlay_detection.py` | `_is_masking_fill`, `_check_coverage`, full-page scan |
| `test_text_layer.py` | Text presence detection |
| `test_parser.py` | Structural extraction, multi-page handling |
| `test_risk_engine.py` | Triage matrix, all color-code transitions |
| `test_annotator.py` | Bounding-box extraction, PNG generation |
| `test_jobs.py` | SQLite CRUD, status transitions, `/status` endpoint |
| `test_upload.py` | File validation, 202 flow, size/MIME rejection |
| `test_routing.py` | Page routes, report render, redirect logic |
| `test_security.py` | Secure filename, rate limiting, security headers, cleanup |
| `test_main.py` | App health |

### Deterministic test fixture generator  
`app/utils/generate_test_pdfs.py`

The test suite requires two controlled PDF artefacts:

- **`clean.pdf`** — a standard Helvetica invoice with clean `pypdf` metadata
  and no structural anomalies. Every forensic check returns `"info"`.
- **`anomalous.pdf`** — the same layout with three deliberate forensic signals
  embedded programmatically via PyMuPDF: a `Sejda` `Creator` field, a
  white-filled rectangle masking a text span, and a single price digit rendered
  in `Times-Roman 14 pt` within a `Helvetica 12 pt` numeric span.

Both files are generated **once per pytest session** by a `session`-scoped
`autouse` fixture in `conftest.py` and written to `tests/fixtures/`. They are
regenerated automatically on the next `pytest` run if the files are missing,
eliminating fragile binary fixture commits from the repository.

Run standalone to inspect the outputs:

```bash
python -m app.utils.generate_test_pdfs
```

---

## Project Layout

```
pdfshield/
├── app/
│   ├── api/
│   │   └── endpoints.py          # page routes + REST API
│   ├── db/
│   │   └── jobs.py               # SQLite CRUD (init · create · update · get)
│   ├── middleware/
│   │   └── rate_limit.py         # slowapi Limiter singleton
│   ├── models/
│   │   └── schemas.py            # Pydantic contracts
│   ├── services/
│   │   ├── analysis_task.py      # BackgroundTask orchestrator
│   │   ├── coordinate_analysis.py
│   │   ├── font_analysis.py
│   │   ├── metadata.py
│   │   ├── overlay_detection.py
│   │   ├── parser.py
│   │   ├── risk_engine.py        # pipeline controller + triage
│   │   └── text_layer.py
│   ├── templates/
│   │   ├── index.html            # upload workspace (vanilla JS fetch)
│   │   ├── processing.html       # animated polling page
│   │   └── report.html           # annotated visual report
│   ├── utils/
│   │   ├── annotator.py          # PyMuPDF PNG generation
│   │   ├── generate_test_pdfs.py # deterministic fixture builder
│   │   └── secure_filename.py    # path-traversal sanitiser
│   └── main.py                   # app factory · middleware · error handlers
├── tests/                        # 552 tests across 13 modules
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Tech Stack

| Concern | Library / Tool |
|---|---|
| Web framework | FastAPI 0.111 + Uvicorn (ASGI) |
| PDF parsing | PyMuPDF (fitz) + pdfplumber + pypdf |
| Schema validation | Pydantic v2 |
| Persistence | SQLite (stdlib `sqlite3`) |
| Rate limiting | slowapi 0.1.9 (token-bucket, in-memory) |
| Templating | Jinja2 |
| Testing | pytest + httpx (ASGI `TestClient`) |
| Container | Docker + Compose |
| Python | 3.12 |
