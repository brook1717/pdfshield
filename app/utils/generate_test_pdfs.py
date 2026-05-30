"""
Generates forensic test PDF fixtures in tests/fixtures/.

Standalone usage:
    python -m app.utils.generate_test_pdfs

Called by pytest via tests/conftest.py session fixtures.

Produces:
    clean.pdf     – standard invoice with Helvetica text and clean metadata.
    anomalous.pdf – invoice with Sejda metadata, a white rectangle overlay,
                    and a price digit rendered in a mismatched font/size.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import fitz  # PyMuPDF

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

# Font aliases used throughout
BODY_FONT = "helv"   # Helvetica  (standard)
ALT_FONT  = "tiro"   # Times-Roman (anomaly marker)
BODY_SIZE = 12.0
ALT_SIZE  = 14.0
LINE_GAP  = 1.6      # line-height multiplier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pdf_now() -> str:
    """Return current UTC time as a PDF date string."""
    now = datetime.now(timezone.utc)
    return now.strftime("D:%Y%m%d%H%M%SZ")


def _tw(text: str, fontname: str, fontsize: float) -> float:
    """Return rendered advance width of *text* in points."""
    return fitz.Font(fontname).text_length(text, fontsize=fontsize)


def _put(page: fitz.Page, x: float, y: float, text: str,
         fontname: str = BODY_FONT, fontsize: float = BODY_SIZE) -> None:
    """Insert a single text run at (x, y)."""
    page.insert_text(
        fitz.Point(x, y),
        text,
        fontname=fontname,
        fontsize=fontsize,
        color=(0, 0, 0),
    )


# ---------------------------------------------------------------------------
# clean.pdf
# ---------------------------------------------------------------------------

def generate_clean_pdf(dest: Path) -> Path:
    """Standard invoice: Helvetica throughout, well-aligned, clean metadata."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)

    doc.set_metadata({
        "title":        "Sample Invoice #1001",
        "author":       "ACME Corp",
        "creator":      "Microsoft Word 16.0",
        "producer":     "Microsoft Word 16.0",
        "creationDate": "D:20240110083000Z",
        "modDate":      "D:20240110083000Z",
    })

    rows: list[tuple[str, float]] = [
        ("ACME Corp",                                       20),
        ("",                                                12),
        ("Invoice #: 1001",                                 12),
        ("Date:          2024-01-15",                       12),
        ("Due Date:       2024-02-15",                      12),
        ("",                                                12),
        ("Bill To:",                                        12),
        ("John Doe",                                        12),
        ("123 Main St, Springfield",                        12),
        ("",                                                12),
        ("Description              Qty   Unit Price   Total", 12),
        ("-" * 56,                                          12),
        ("Widget A                   2     $100.00    $200.00", 12),
        ("Widget B                   1     $350.00    $350.00", 12),
        ("-" * 56,                                          12),
        ("Invoice Total:                              $550.00", 12),
    ]

    y = 80.0
    for text, size in rows:
        if text:
            _put(page, 72, y, text, BODY_FONT, size)
        y += size * LINE_GAP

    doc.save(str(dest), garbage=4, deflate=True)
    doc.close()
    return dest


# ---------------------------------------------------------------------------
# anomalous.pdf
# ---------------------------------------------------------------------------

def generate_anomalous_pdf(dest: Path) -> Path:
    """
    Invoice with three forensic anomalies:

    1. Metadata fingerprint — creator/producer set to 'Sejda Version 5.3.7'
       with a modDate newer than creationDate (edit indicator).

    2. White rectangle overlay — a filled white fitz.Rect drawn on top of the
       Widget A line, visually hiding the original value in the content stream.

    3. Font-family mismatch — in the price field "Invoice Total: $550.00"
       the leading '5' of 550 is rendered in Times-Roman 14 pt while the rest
       of the document uses Helvetica 12 pt.
    """
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)

    # --- Anomaly 1: Sejda metadata ---
    doc.set_metadata({
        "title":        "Sample Invoice #1001",
        "author":       "ACME Corp",
        "creator":      "Sejda Version 5.3.7",
        "producer":     "Sejda Version 5.3.7",
        "creationDate": "D:20240110083000Z",
        "modDate":      _pdf_now(),        # modDate > creationDate → edited
    })

    rows: list[tuple[str, float]] = [
        ("ACME Corp",                                        20),
        ("",                                                 12),
        ("Invoice #: 1001",                                  12),
        ("Date:          2024-01-15",                        12),
        ("Due Date:       2024-02-15",                       12),
        ("",                                                 12),
        ("Bill To:",                                         12),
        ("John Doe",                                         12),
        ("123 Main St, Springfield",                         12),
        ("",                                                 12),
        ("Description              Qty   Unit Price   Total", 12),
        ("-" * 56,                                           12),
    ]

    y = 80.0
    for text, size in rows:
        if text:
            _put(page, 72, y, text, BODY_FONT, size)
        y += size * LINE_GAP

    # "Widget A" row — written first, then covered by white rect below
    widget_a_y = y
    _put(page, 72, widget_a_y, "Widget A                   2     $100.00    $200.00", BODY_FONT, 12)
    y += 12 * LINE_GAP

    _put(page, 72, y, "Widget B                   1     $350.00    $350.00", BODY_FONT, 12)
    y += 12 * LINE_GAP

    _put(page, 72, y, "-" * 56, BODY_FONT, 12)
    y += 12 * LINE_GAP

    # --- Anomaly 2: white rectangle overlay covering Widget A row ---
    # Placed AFTER drawing the text so it sits above it in the content stream.
    rect_top = widget_a_y - 12          # a few pts above the baseline
    rect_bot = widget_a_y + 4
    overlay = fitz.Rect(72, rect_top, 540, rect_bot)
    page.draw_rect(overlay, color=(1, 1, 1), fill=(1, 1, 1), fill_opacity=1)

    # --- Anomaly 3: font-family mismatch in price field ---
    # "Invoice Total: $" and "50.00" in Helvetica 12;
    # the leading "5" (hundreds digit) in Times-Roman 14.
    prefix = "Invoice Total:                              $"
    anomaly_char = "5"
    suffix = "50.00"

    _put(page, 72, y, prefix, BODY_FONT, BODY_SIZE)
    x_after_prefix = 72 + _tw(prefix, BODY_FONT, BODY_SIZE)

    _put(page, x_after_prefix, y, anomaly_char, ALT_FONT, ALT_SIZE)
    x_after_anomaly = x_after_prefix + _tw(anomaly_char, ALT_FONT, ALT_SIZE)

    _put(page, x_after_anomaly, y, suffix, BODY_FONT, BODY_SIZE)

    doc.save(str(dest), garbage=4, deflate=True)
    doc.close()
    return dest


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate_all(output_dir: Path = FIXTURES_DIR) -> dict[str, Path]:
    """Generate both fixtures and return a mapping of name → Path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    return {
        "clean":     generate_clean_pdf(output_dir / "clean.pdf"),
        "anomalous": generate_anomalous_pdf(output_dir / "anomalous.pdf"),
    }


if __name__ == "__main__":
    paths = generate_all()
    print("Generated test PDF fixtures:")
    for name, path in paths.items():
        size_kb = path.stat().st_size / 1024
        print(f"  [{name:10s}]  {path}  ({size_kb:.1f} KB)")
