"""PDF extraction pipeline.

Goal: extract a company's financial line items from *any* filing PDF, as
robustly as possible, and report exactly what could not be read.

How it works
------------
1. **Text layer** — digital filings (ASX Appendix 4E/4D, US 10-K/10-Q, results
   presentations) carry a real text layer, which ``pdfplumber`` extracts
   accurately. This is the primary source.
2. **Image OCR fallback** — if a document is scanned (mostly empty text layer),
   we rasterise the pages and run Tesseract, when ``pytesseract`` + the
   ``tesseract`` binary + ``pdf2image`` (poppler) are available. Absent those,
   the document is reported as unreadable rather than silently producing
   nothing.
3. **Currency & scale detection** — we sniff the reporting currency (USD / AUD /
   GBP / …) and the units ("in millions" / "in thousands" / "in billions") from
   the statement headers, and normalise every monetary figure to *millions of
   the reporting currency* so downstream maths is unit-consistent.
4. **Candidate scoring** — for each canonical field we gather every line whose
   label matches, extract the current-period value (skipping note references
   like "2.5" and footnote markers like the "1" in "EBITDAX1"), and score each
   candidate so genuine statement rows beat prose, tables of contents and
   footnotes. The best-scoring candidate wins; its score becomes a confidence.

The extractor never raises for a missing figure — it simply omits the field and
lists it in ``missing_required`` so the UI can offer re-upload or manual entry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

from .fields import DOC_TYPE_BY_KEY, FIELDS, detect_doc_type

# A number token: 1,234  |  1234  |  27.7  |  (137)  |  -137  |  3.4%
_NUM = re.compile(r"\(?-?\$?\s?[\d,]+(?:\.\d+)?\)?%?")
_NOTE = re.compile(r"^\d{1,2}\.\d{1,2}(?:\([a-z]\))?$")  # e.g. 2.5, 6.4(b)

# Words that mark a line as prose / narrative / table-of-contents rather than a
# clean financial-statement row.
_PROSE = (
    "down", "refer", "page ", "growth", "increased", "decreased", "reflect",
    "compared", "comprise", "unissued", "unvested", "statement of compliance",
    "directors", " to ", "primarily", "represents", "typically", "between",
    "during the", "declaration", "contents",
)


# --------------------------------------------------------------------------- #
#  Currency & scale detection                                                  #
# --------------------------------------------------------------------------- #
# Ordered so the most specific marker wins (US$ before a bare $).
_CCY_MARKERS = (
    ("USD", (r"us\$", r"\busd\b", r"u\.s\. dollar", r"united states dollar")),
    ("AUD", (r"a\$", r"au\$", r"\baud\b", r"australian dollar")),
    ("GBP", (r"£", r"\bgbp\b", r"pound sterling")),
    ("EUR", (r"€", r"\beur\b")),
    ("CAD", (r"c\$", r"\bcad\b", r"canadian dollar")),
    ("NZD", (r"nz\$", r"\bnzd\b")),
)
_SCALE_MARKERS = (
    (0.001, (r"in thousands", r"\$['’]?000", r"\$'?000", r"thousands of", r"figures in \$?000")),
    (1000.0, (r"in billions", r"\$bn\b", r"billions of")),
    (1.0, (r"in millions", r"\$\s?m\b", r"\bmillion", r"\$'?m\b", r"us\$million", r"a\$million")),
)


def detect_currency_and_scale(text: str) -> tuple[str | None, float]:
    """Sniff (reporting_currency, scale_to_millions) from statement text.

    ``scale_to_millions`` multiplies a raw figure to express it in millions:
    values already in millions -> 1.0, thousands -> 0.001, billions -> 1000.0.
    Returns ``(None, 1.0)`` when the currency cannot be determined.
    """
    low = text.lower()
    currency = None
    best = 0
    for ccy, pats in _CCY_MARKERS:
        hits = sum(len(re.findall(p, low)) for p in pats)
        if hits > best:
            best, currency = hits, ccy
    scale = 1.0
    scale_best = 0
    for mult, pats in _SCALE_MARKERS:
        hits = sum(len(re.findall(p, low)) for p in pats)
        if hits > scale_best:
            scale_best, scale = hits, mult
    return currency, scale


def _confidence(score: int) -> str:
    """Map a candidate score to a human-readable confidence band."""
    if score >= 5:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


def _to_float(tok: str) -> float | None:
    t = tok.strip()
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace(",", "").replace("$", "").replace("%", "").replace(" ", "")
    if t in ("", "-", "–", "—"):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _numeric_tokens(tail: str) -> list[str]:
    return [t for t in _NUM.findall(tail) if t.strip(" $()%-–—")]


def _substantial(bare: str) -> bool:
    """A 'real' statement figure rather than a footnote/note marker."""
    b = bare.replace("-", "")
    return ("," in b) or ("." in b) or (len(b) >= 2) or bare == "0"


def _pick_value(tail: str) -> float | None:
    """Return the current-period value from the text after a label.

    Skips a leading run of footnote markers (bare single digits, e.g.
    "EBITDAX1, 2 3,391") and one leading note reference ("2.5", "6.4(b)").
    """
    raw = _numeric_tokens(tail)
    if not raw:
        return None
    i = 0
    # Drop leading footnote markers (single digits, possibly glued to a list
    # comma like "1," in "EBITDAX1, 2 3,391") while a real figure follows.
    while i < len(raw) and re.fullmatch(r"[1-9]", raw[i].strip("()%$ ").replace(",", "")):
        if any(_substantial(raw[j].strip("()%$ ")) for j in range(i + 1, len(raw))):
            i += 1
        else:
            break
    # Drop one leading note reference like "2.5" / "6.4(b)" if a value follows.
    if i < len(raw) and _NOTE.match(raw[i].strip("()%$ ")) and i + 1 < len(raw):
        i += 1
    for tok in raw[i:]:
        v = _to_float(tok)
        if v is not None:
            return v
    return None


def _score_candidate(line: str, tail: str, is_rate: bool, value: float | None) -> int:
    """Higher = more likely to be a genuine statement row for this field."""
    low = (line + " " + tail).lower()
    score = 0
    # A bare 4-digit year in prose (e.g. "During 2025") is almost never the
    # figure we want. A real $2,025m figure carries a thousands comma, so only
    # penalise the comma-less year form.
    if not is_rate and value is not None and 1990 <= value <= 2035 and value == int(value):
        # Match the bare year form only (a real $2,025m figure keeps its comma,
        # so "2025" won't appear contiguously for it).
        if re.search(rf"(?<!\d){int(value)}(?!\d)", line + " " + tail):
            score -= 6
    if is_rate:
        # Rate fields: prefer a plausible positive percentage; a % sign helps.
        if value is not None and 0 < value <= 40:
            score += 3
        if value is not None and value < 0:
            score -= 6
        if "%" in line or "per cent" in low:
            score += 2
        if "typically" in low or "range" in low:
            score += 1
    else:
        score -= 4 * sum(p in low for p in _PROSE)
        if "%" in line or "per cent" in low:
            score -= 4
        nums = _numeric_tokens(tail)
        if len(nums) >= 2:      # two data columns (current + prior year) = real row
            score += 3
        if any("," in n for n in nums):  # comma-grouped thousands = statement figure
            score += 2
    return score


@dataclass
class DocResult:
    """Outcome of extracting one document.

    ``fields`` maps a canonical field key to a cell describing the value found:
    ``{"value", "raw", "line", "confidence"}``. Monetary values are already
    normalised to millions of ``currency``.
    """

    filename: str
    doc_type: str
    doc_type_label: str
    pages: int
    fields: dict[str, dict]
    missing_required: list[dict]   # [{"key","label"}] required but not found
    method: str = "text"           # text | ocr
    currency: str | None = None    # detected reporting currency
    scale_to_millions: float = 1.0 # multiplier applied to raw monetary figures

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "doc_type": self.doc_type,
            "doc_type_label": self.doc_type_label,
            "pages": self.pages,
            "fields": self.fields,
            "missing_required": self.missing_required,
            "method": self.method,
            "currency": self.currency,
            "scale_to_millions": self.scale_to_millions,
            "ok": not self.missing_required,
        }


def _read_text(path: Path) -> tuple[list[str], int, str]:
    """Return (lines, page_count, method)."""
    lines: list[str] = []
    method = "text"
    with pdfplumber.open(str(path)) as pdf:
        n = len(pdf.pages)
        empty_pages = 0
        for pg in pdf.pages:
            txt = pg.extract_text() or ""
            if not txt.strip():
                empty_pages += 1
            lines.extend(txt.split("\n"))
    # If the doc looks scanned (mostly empty text), try OCR fallback.
    if n and empty_pages / n > 0.6:
        ocr_lines = _ocr_fallback(path)
        if ocr_lines:
            return ocr_lines, n, "ocr"
    return lines, n, method


def _ocr_fallback(path: Path) -> list[str]:
    try:
        import pytesseract  # noqa
        from pdf2image import convert_from_path  # noqa
    except Exception:
        return []
    try:
        images = convert_from_path(str(path))
    except Exception:
        return []
    out: list[str] = []
    for img in images:
        out.extend(pytesseract.image_to_string(img).split("\n"))
    return out


def extract_document(path: str | Path, doc_type: str | None = None) -> DocResult:
    """Extract canonical fields from one PDF and report what's missing."""
    path = Path(path)
    lines, n, method = _read_text(path)
    dtype = doc_type or detect_doc_type(path.name)
    dt = DOC_TYPE_BY_KEY.get(dtype, DOC_TYPE_BY_KEY["other"])

    # Detect currency + scale from the whole document (headers repeat the unit).
    currency, scale = detect_currency_and_scale("\n".join(lines))

    found: dict[str, dict] = {}
    for f in FIELDS:
        is_rate = f.kind == "rate"
        candidates: list[tuple[int, int, float, str]] = []  # (score, line_idx, value, raw)
        for idx, line in enumerate(lines):
            low = line.lower()
            for pat in f.patterns:
                m = re.search(pat, low)
                if not m:
                    continue
                tail = line[m.end():]
                val = _pick_value(tail)
                # Header-style label (value sits on the next line), e.g. "Dividends per share (¢)".
                if val is None and idx + 1 < len(lines):
                    tail = lines[idx + 1]
                    val = _pick_value(tail)
                if val is None:
                    continue
                candidates.append((_score_candidate(line, tail, is_rate, val), idx, val, line.strip()[:160]))
                break
        if not candidates:
            continue
        # Best score wins; earliest occurrence breaks ties (statements precede appendices).
        best = max(candidates, key=lambda c: (c[0], -c[1]))
        score, idx, value, raw = best
        # Normalise monetary figures to millions of the reporting currency.
        if f.is_monetary and scale != 1.0:
            value = value * scale
        found[f.key] = {"value": value, "raw": raw, "line": idx, "confidence": _confidence(score)}

    missing = [
        {"key": req, "label": next(fl.label for fl in FIELDS if fl.key == req)}
        for req in dt.required
        if req not in found
    ]
    return DocResult(
        filename=path.name,
        doc_type=dtype,
        doc_type_label=dt.label,
        pages=n,
        fields=found,
        missing_required=missing,
        method=method,
        currency=currency,
        scale_to_millions=scale,
    )
