"""PDF extraction pipeline.

Strategy: these filings are digital (text-based) PDFs, so `pdfplumber` text
extraction is accurate and fast. For scanned/image pages we fall back to
Tesseract OCR if the `pytesseract` + `tesseract` binary are available.

For each canonical field we scan every line, match a label pattern, and pull
the current-period numeric value (the first data column after the label),
skipping note-reference tokens like "2.5" or "6.4(b)".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
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
    filename: str
    doc_type: str
    doc_type_label: str
    pages: int
    fields: dict[str, dict]        # key -> {"value": float, "raw": str, "page": int}
    missing_required: list[str]    # human labels of required-but-not-found fields
    method: str = "text"           # text | ocr

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "doc_type": self.doc_type,
            "doc_type_label": self.doc_type_label,
            "pages": self.pages,
            "fields": self.fields,
            "missing_required": self.missing_required,
            "method": self.method,
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
    path = Path(path)
    lines, n, method = _read_text(path)
    dtype = doc_type or detect_doc_type(path.name)
    dt = DOC_TYPE_BY_KEY.get(dtype, DOC_TYPE_BY_KEY["other"])

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
        found[f.key] = {"value": best[2], "raw": best[3], "line": best[1]}

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
    )
