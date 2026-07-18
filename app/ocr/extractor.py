"""PDF extraction pipeline.

Goal: extract a company's financial line items from *any* filing PDF, as
robustly as possible, and report exactly what could not be read.

How it works
------------
1. **Text layer** — digital filings (ASX Appendix 4E/4D, US 10-K/10-Q, results
   presentations) carry a real text layer, which ``pypdf`` extracts
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
from datetime import date
from pathlib import Path

from pypdf import PdfReader

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
_MONTHS = {name.lower(): index for index, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"), 1)}

_PAGE_BREAK = "__SVE_PAGE_BREAK__"
_TARGET_PAGE_TERMS = (
    "statement of income", "income statement", "profit or loss",
    "statement of financial position", "balance sheet",
    "statement of cash flows", "cash flow statement",
    "statement of changes in equity", "earnings per share",
    "issued capital", "share capital", "weighted average number",
    "interest-bearing loans", "borrowings", "debt maturity",
    "dividends paid and proposed", "dividends per share",
    "segment information", "operating segments",
)


class PdfLimitError(ValueError):
    """The uploaded PDF exceeds a configured safe processing limit."""


def _detect_period_end(lines: list[str]) -> str | None:
    """Detect a statement period end from common IFRS/US filing headings."""
    for line in lines[: min(len(lines), 1500)]:
        match = re.search(
            r"ended\s+(?:on\s+)?(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})",
            line, re.IGNORECASE)
        if not match:
            match = re.search(
                r"ended\s+(?:on\s+)?([A-Za-z]+)\s+(\d{1,2}),?\s+(20\d{2})",
                line, re.IGNORECASE)
            if match:
                month, day, year = match.groups()
            else:
                continue
        else:
            day, month, year = match.groups()
        try:
            return date(int(year), _MONTHS[month.lower()], int(day)).isoformat()
        except (KeyError, ValueError):
            continue
    return None


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

# --------------------------------------------------------------------------- #
#  Cash-flow-statement section context                                          #
# --------------------------------------------------------------------------- #
# Sub-headings inside a statement of cash flows. Knowing which section a line
# sits under lets us pull CapEx from *investing* activities (not, say, an
# income-statement depreciation line) and operating cash flow from *operating*
# activities — so the DCF uses genuine cash-flow figures rather than proxies.
_CF_SECTIONS = (
    ("cf_operating", (r"cash flows? (?:from|used in|provided by|(?:relating|related) to) operating",
                      r"^\s*(?:net )?cash (?:flows? )?from operating activities")),
    ("cf_investing", (r"cash flows? (?:from|used in|provided by|(?:relating|related) to) investing",
                      r"investing activities")),
    ("cf_financing", (r"cash flows? (?:from|used in|provided by|(?:relating|related) to) financing",
                      r"financing activities")),
)

# Which section each cash-flow field should be found under.
_FIELD_SECTION = {
    "capex": "cf_investing",
    "op_cash_flow": "cf_operating",
    "dividends_paid": "cf_financing",
}

_STATEMENT_HEADINGS = (
    ("income_statement", (r"statement of (?:comprehensive )?income", r"income statement", r"profit or loss")),
    ("balance_sheet", (r"statement of financial position", r"balance sheets?")),
    ("cash_flow_statement", (r"statement of cash flows?", r"cash flow statements?")),
    ("changes_in_equity", (r"statement of changes in equity", r"stockholders'? equity")),
    ("segment_note", (r"segment information", r"operating segments?")),
    ("debt_note", (r"interest-bearing loans and borrowings", r"borrowings and other financial liabilities")),
    ("share_note", (r"issued capital", r"share capital", r"earnings per share")),
    ("dividend_note", (r"dividends paid and proposed", r"dividends? per share")),
)

_FIELD_STATEMENTS = {
    "revenue": {"income_statement", "segment_note"}, "ebit": {"income_statement"},
    "net_profit": {"income_statement"}, "income_tax": {"income_statement"},
    "interest_expense": {"income_statement", "debt_note"},
    "total_assets": {"balance_sheet"}, "total_liabilities": {"balance_sheet"},
    "total_equity": {"balance_sheet", "changes_in_equity"}, "cash": {"balance_sheet", "cash_flow_statement"},
    "borrowings_total": {"balance_sheet", "debt_note"}, "borrowings_current": {"balance_sheet", "debt_note"},
    "borrowings_noncurrent": {"balance_sheet", "debt_note"},
    "eps_basic": {"share_note", "income_statement"}, "wtd_avg_shares": {"share_note"},
    "shares_on_issue": {"changes_in_equity", "share_note"}, "dps": {"dividend_note"},
    "op_cash_flow": {"cash_flow_statement"}, "capex": {"cash_flow_statement"},
    "cash_opening": {"cash_flow_statement"}, "cash_movement": {"cash_flow_statement"},
}


def _statement_contexts(lines: list[str]) -> list[str | None]:
    """Track the primary statement/note section for every extracted line."""
    current = None
    contexts = []
    for line in lines:
        low = line.lower().strip()
        if low == _PAGE_BREAK.lower():
            current = None
            contexts.append(None)
            continue
        for section, patterns in _STATEMENT_HEADINGS:
            if any(re.search(pattern, low) for pattern in patterns):
                current = section
                break
        contexts.append(current)
    return contexts


# Capital-expenditure component lines as they appear inside the investing
# section (a filing often splits CapEx across several rows under "Payments for:"
# rather than a single "capital expenditure" total). Summing the outflows gives
# genuine cash-flow CapEx for the DCF instead of a D&A proxy.
_CAPEX_ITEM_PATS = (
    r"oil and gas assets",
    r"exploration and evaluation",
    r"property,? plant (?:and|&) equipment",
    r"land,? buildings,? plant",
    r"mine properties",
    r"development (?:expenditure|assets|wells)",
    r"evaluation assets",
    r"intangible assets",
    r"capital(?:ised)? exploration",
)


def _capex_from_investing(lines: list[str], contexts: list[str | None],
                          scale: float) -> tuple[float | None, int]:
    """Sum PP&E / E&E cash outflows within the investing-activities section.

    Only negative (outflow) values of recognised CapEx component lines are
    counted, so proceeds, loans and acquisitions are excluded. Returns
    ``(total_in_millions, line_count)`` or ``(None, 0)`` when nothing matches.
    """
    total = 0.0
    hits = 0
    for idx, line in enumerate(lines):
        if contexts[idx] != "cf_investing":
            continue
        low = line.lower()
        if not any(re.search(p, low) for p in _CAPEX_ITEM_PATS):
            continue
        val = _pick_value(line)
        if val is None or val >= 0:   # capex is a cash outflow (negative)
            continue
        total += abs(val)
        hits += 1
    if hits:
        return total * scale, hits
    return None, 0


def _section_contexts(lines: list[str]) -> list[str | None]:
    """Tag each line with the cash-flow sub-section currently in force.

    Walks the document top-to-bottom; when a line matches a section heading the
    tag carries forward to subsequent lines until the next heading. Lines before
    any cash-flow heading (or in other statements) are tagged ``None``.
    """
    tags: list[str | None] = []
    current: str | None = None
    for line in lines:
        low = line.lower()
        if low.strip() == _PAGE_BREAK.lower():
            current = None
            tags.append(None)
            continue
        for tag, pats in _CF_SECTIONS:
            if any(re.search(p, low) for p in pats):
                current = tag
                break
        tags.append(current)
    return tags


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


def _score_candidate(line: str, tail: str, is_rate: bool, value: float | None,
                     field_key: str = "", context: str | None = None,
                     statement_context: str | None = None) -> int:
    """Higher = more likely to be a genuine statement row for this field."""
    low = (line + " " + tail).lower()
    score = 0
    # Cash-flow fields: strongly prefer a candidate sitting under the expected
    # cash-flow sub-section (e.g. CapEx under "investing activities"), and
    # penalise one found elsewhere (a stray "capital expenditure" in prose or a
    # commentary table). This is what makes the DCF use real CapEx, not a proxy.
    want_section = _FIELD_SECTION.get(field_key)
    if want_section:
        if context == want_section:
            score += 5
        elif context is None:
            score -= 3          # not under any cash-flow heading — likely prose
        else:
            score -= 2          # under the wrong cash-flow section
    wanted_statements = _FIELD_STATEMENTS.get(field_key)
    if wanted_statements and statement_context:
        score += 3 if statement_context in wanted_statements else -2
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
    period_end: str | None = None
    as_of_date: str | None = None
    pages_processed: int | None = None
    warnings: list[str] | None = None

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
            "period_end": self.period_end,
            "as_of_date": self.as_of_date,
            "pages_processed": self.pages_processed if self.pages_processed is not None else self.pages,
            "warnings": self.warnings or [],
            "ok": not self.missing_required,
        }


def _target_pages(page_text: list[str], limit: int) -> list[int]:
    """Choose high-value filing pages and their neighbours within ``limit``.

    Annual reports repeat financial-statement headings reliably.  Keeping the
    first pages plus the pages around those headings captures identity, units,
    primary statements and relevant notes without retaining hundreds of PDF
    page objects in a small production instance.
    """
    n = len(page_text)
    if n <= limit:
        return list(range(n))
    selected = set(range(min(8, n)))
    scored: list[tuple[int, int]] = []
    for idx, text in enumerate(page_text):
        low = text.lower()
        score = sum(3 for term in _TARGET_PAGE_TERMS if term in low)
        score += sum(1 for term in ("$million", "usd million", "aud million", "notes to the financial")
                     if term in low)
        if score:
            scored.append((score, idx))
    for _, idx in sorted(scored, key=lambda item: (-item[0], item[1])):
        for page_idx in (idx - 1, idx, idx + 1):
            if 0 <= page_idx < n and len(selected) < limit:
                selected.add(page_idx)
        if len(selected) >= limit:
            break
    return sorted(selected)


def _read_text(path: Path, max_pages: int = 500,
               max_text_pages: int = 80, max_ocr_pages: int = 40,
               ) -> tuple[list[str], int, str, int, list[str]]:
    """Return bounded text without retaining heavyweight page render state."""
    reader = PdfReader(str(path), strict=False)
    n = len(reader.pages)
    if n > max_pages:
        raise PdfLimitError(f"PDF has {n} pages; the safe limit is {max_pages} pages")

    page_text: list[str] = []
    empty_pages = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if not text.strip():
            empty_pages += 1
        page_text.append(text)

    warnings: list[str] = []
    selected = _target_pages(page_text, max_text_pages)
    if len(selected) < n:
        warnings.append(f"Targeted {len(selected)} of {n} pages to stay within hosting limits")

    # A mostly image-only filing needs OCR. Rasterise only a bounded page set,
    # one page at a time, at a modest DPI; never materialise the whole PDF.
    if n and empty_pages / n > 0.6:
        ocr_pages = selected[:max_ocr_pages]
        ocr_lines = _ocr_fallback(path, ocr_pages)
        if ocr_lines:
            if len(ocr_pages) < n:
                warnings.append(f"OCR limited to {len(ocr_pages)} of {n} pages")
            return ocr_lines, n, "ocr", len(ocr_pages), warnings
        warnings.append("Image-only pages could not be OCR processed on this host")

    lines: list[str] = []
    for idx in selected:
        lines.extend(page_text[idx].splitlines())
        lines.append(_PAGE_BREAK)
    return lines, n, "text", len(selected), warnings


def _ocr_fallback(path: Path, page_indexes: list[int]) -> list[str]:
    try:
        import pytesseract  # noqa
        from pdf2image import convert_from_path  # noqa
    except Exception:
        return []
    out: list[str] = []
    for index in page_indexes:
        try:
            images = convert_from_path(
                str(path), dpi=160, first_page=index + 1,
                last_page=index + 1, grayscale=True, thread_count=1)
            if images:
                out.extend(pytesseract.image_to_string(images[0]).splitlines())
                images[0].close()
                out.append(_PAGE_BREAK)
        except Exception:
            continue
    return out


def extract_document(path: str | Path, doc_type: str | None = None,
                     *, max_pages: int = 500, max_text_pages: int = 80,
                     max_ocr_pages: int = 40) -> DocResult:
    """Extract canonical fields from one PDF and report what's missing."""
    path = Path(path)
    lines, n, method, pages_processed, warnings = _read_text(
        path, max_pages=max_pages, max_text_pages=max_text_pages,
        max_ocr_pages=max_ocr_pages)
    return _extract_lines(
        lines, filename=path.name, doc_type=doc_type, pages=n,
        method=method, pages_processed=pages_processed, warnings=warnings)


def extract_page_texts(page_text: list[str], filename: str,
                       doc_type: str | None = None, *,
                       max_pages: int = 500, max_text_pages: int = 80) -> DocResult:
    """Extract fields from PDF text produced in the user's browser.

    The browser performs the memory-heavy PDF parsing.  This function applies
    the same targeted-page selection and canonical field scoring as the normal
    server-side PDF path, keeping results consistent while allowing a small
    backend instance to process large digital filings.
    """
    n = len(page_text)
    if n > max_pages:
        raise PdfLimitError(f"PDF has {n} pages; the safe limit is {max_pages} pages")
    selected = _target_pages(page_text, max_text_pages)
    lines: list[str] = []
    for idx in selected:
        lines.extend(page_text[idx].splitlines())
        lines.append(_PAGE_BREAK)
    warnings = ["PDF text extracted locally in the browser; original file was not uploaded"]
    if len(selected) < n:
        warnings.append(f"Targeted {len(selected)} of {n} pages to stay within hosting limits")
    return _extract_lines(
        lines, filename=filename, doc_type=doc_type, pages=n,
        method="browser-text", pages_processed=len(selected), warnings=warnings)


def _extract_lines(lines: list[str], *, filename: str, doc_type: str | None,
                   pages: int, method: str, pages_processed: int,
                   warnings: list[str]) -> DocResult:
    """Interpret already-extracted lines using the canonical field rules."""
    dtype = doc_type or detect_doc_type(filename)
    dt = DOC_TYPE_BY_KEY.get(dtype, DOC_TYPE_BY_KEY["other"])

    # Detect currency + scale from the whole document (headers repeat the unit).
    currency, scale = detect_currency_and_scale("\n".join(lines))
    period_end = _detect_period_end(lines)
    # Tag each line with its cash-flow sub-section for CapEx/op-CF/dividends.
    contexts = _section_contexts(lines)
    statement_contexts = _statement_contexts(lines)

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
                score = _score_candidate(line, tail, is_rate, val, field_key=f.key, context=contexts[idx],
                                         statement_context=statement_contexts[idx])
                candidates.append((score, idx, val, line.strip()[:160]))
                break
        if not candidates:
            continue
        # Best score wins; earliest occurrence breaks ties (statements precede appendices).
        best = max(candidates, key=lambda c: (c[0], -c[1]))
        score, idx, value, raw = best
        # Normalise monetary figures to millions of the reporting currency.
        if f.is_monetary and scale != 1.0:
            value = value * scale
        found[f.key] = {"value": value, "raw": raw, "line": idx, "confidence": _confidence(score),
                        "statement_section": statement_contexts[idx]}

    # Prefer genuine cash-flow CapEx: sum the investing-activities PP&E/E&E
    # outflows. This overrides a single-line match that may be a note reference
    # (e.g. "Capital expenditure, operating assets 7.1"), giving the DCF a real
    # CapEx figure rather than a D&A proxy.
    capex_cf, capex_hits = _capex_from_investing(lines, contexts, scale)
    if capex_cf is not None:
        found["capex"] = {
            "value": capex_cf,
            "raw": f"Σ {capex_hits} investing-activities PP&E/E&E outflow line(s)",
            "line": -1,
            "confidence": "high" if capex_hits >= 2 else "medium",
        }

    missing = [
        {"key": req, "label": next(fl.label for fl in FIELDS if fl.key == req)}
        for req in dt.required
        if req not in found
    ]
    return DocResult(
        filename=filename,
        doc_type=dtype,
        doc_type_label=dt.label,
        pages=pages,
        fields=found,
        missing_required=missing,
        method=method,
        currency=currency,
        scale_to_millions=scale,
        period_end=period_end,
        as_of_date=period_end,
        pages_processed=pages_processed,
        warnings=warnings,
    )
