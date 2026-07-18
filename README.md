# Securities Valuation Engine

Upload a company's trailing-twelve-month filing set, OCR-extract the financials,
pull free market data, and run **65 valuation methods** to triangulate an
intrinsic value — for any listed security, on any chosen date. Everything is
**priced in AUD**: the reporting currency is detected from the documents and
converted at the valuation-date FX rate so the intrinsic value is directly
comparable with the ASX price.

> Educational reference only. Not personalised investment advice.

## What it does

A five-step wizard:

1. **Stock** — enter `MARKET:TICKER` (e.g. `ASX:STO`, `NASDAQ:AAPL`).
2. **Date** — pick any day; the engine derives the year, quarter, and the
   **previous four completed quarters** (e.g. `2,3,4,1` or `1,2,3,4`).
3. **Documents** — drop the Annual Report / Half-Year / Results Presentation and
   the four quarterly reports. Each bubble runs OCR with a live spinner; if a
   required figure can't be read, it says **exactly what's missing** and offers
   **re-upload** (same page) or **manual input** (next page).
4. **Manual input** — type any values OCR missed.
5. **Valuation** — all 65 methods in a scrollable table, the triangulated
   intrinsic value below it, and an **Export ▾** (PDF / JPEG) button top-right.

## Architecture

```
app/
  quarters.py        date -> current quarter + previous 4 completed quarters
  ocr/               pdfplumber extraction + field schema (+ Tesseract fallback)
    fields.py        canonical field schema; AU/IFRS + US-GAAP label synonyms
    extractor.py     currency/scale detection, candidate scoring, confidence
  market/            free feeds + FX: Yahoo/Stooq, Finnhub, FRED, Ken French, Damodaran
  valuation/         the 65 methods + triangulation engine (FX -> AUD)
  export/            PDF (reportlab) + JPEG (Pillow)
  main.py            FastAPI routes
  static/            vanilla-JS wizard SPA
```

### OCR engine

Designed to read *any* filing, not just one company's template:

- **Two-layer read** — uses the PDF text layer first (accurate on digital
  filings); for scanned documents it falls back to Tesseract OCR when
  `pytesseract` + `tesseract` + `pdf2image`/poppler are installed. If a document
  is genuinely unreadable it is reported as such rather than returning silence.
- **Currency & scale detection** — sniffs the reporting currency (USD/AUD/GBP/…)
  and units ("in millions"/"in thousands"/"in billions") from statement headers
  and normalises every monetary figure to *millions of the reporting currency*.
- **Broad label synonyms** — each canonical field matches both AU/IFRS wording
  (e.g. "Product sales", "Interest-bearing loans and borrowings") and US-GAAP
  wording (e.g. "Net sales", "Long-term debt", "Total stockholders' equity").
- **Candidate scoring** — every matching line is scored so genuine statement
  rows beat prose, tables of contents and footnotes; note references ("2.5") and
  footnote markers (the "1" in "EBITDAX1") are stripped, and bare years in prose
  are penalised. Each extracted value carries a `high`/`medium`/`low` confidence.
- **Cash-flow-aware CapEx & FCF** — each line is tagged with its cash-flow
  sub-section (operating / investing / financing), so CapEx is taken from the
  *investing-activities* outflows (summed across split rows such as "Oil and gas
  assets", "Exploration and evaluation assets", "Property, plant and equipment"
  under a "Payments for:" header) rather than a stray "capital expenditure"
  mention in prose or a note index. The DCF then uses genuine operating cash
  flow − CapEx instead of proxying CapEx by depreciation; where a company also
  states a (non-IFRS) free cash flow figure it is captured and shown as a
  cross-check in the method notes.
- **Never silently wrong** — a field the engine can't read with confidence is
  omitted and listed in `missing_required`, which drives the UI's
  "exactly what's missing" warning + re-upload / manual-entry paths.

### Currency handling (AUD)

1. The OCR layer detects each document's reporting currency.
2. `market/feeds.fx_rate()` fetches the reporting→AUD rate for the valuation
   date from Yahoo (`USDAUD=X`, inverting `AUDUSD=X` if needed), with a static
   fallback flagged in the output.
3. The valuation engine converts every monetary and per-share input to AUD up
   front, so all 65 methods, the triangulated intrinsic value, and the price
   comparison are in one currency. Rates, ratios and share counts are never
   FX-scaled. The FX rate used is shown on the summary screen and in exports.

### Free-cash-flow input priority

DCF, FCF-yield, Gordon terminal-value, and reverse-DCF methods use one consistent
FCF input, in this order:

1. Cash flow from operating activities minus cash-flow-statement CapEx.
2. Operating cash flow minus D&A only when reliable CapEx was not extracted;
   affected methods explicitly label this as a proxy.
3. Company-reported FCF only when operating cash flow is unavailable, because
   reported FCF is commonly a non-IFRS/non-GAAP measure. When both statement
   inputs and reported FCF exist, the latter is displayed only as a cross-check.

CapEx is normalised to a positive outflow magnitude before subtraction and is
rejected if it is implausibly large relative to operating cash flow or revenue.
All monetary components are converted from the reporting currency to AUD using
the same valuation-date FX multiplier before FCF is calculated.

## Free data sources

| Need | Source | Key? |
|------|--------|------|
| Daily price, beta, volatility, shares | Yahoo Finance (`yfinance`), Stooq | no |
| Analyst consensus / price targets | Finnhub | `FINNHUB_API_KEY` |
| Risk-free yield + macro | FRED | `FRED_API_KEY` |
| Fama-French / momentum factors | Ken French (`pandas-datareader`) | no |
| FX rate (reporting currency → AUD) | Yahoo Finance (`{FROM}{TO}=X`) | no |
| Equity risk premium | Damodaran (fallback constant) | no |

Everything degrades gracefully: no network or no key just leaves those methods
marked *needs external data* — the document-based methods still run.

## API keys (public-repo safe)

Keys live only in `.env`, which is git-ignored. Copy the template and paste yours:

```bash
cp .env.example .env
# edit .env:  FINNHUB_API_KEY=...   FRED_API_KEY=...
```

Nothing in the committed source contains a key; `/api/config` reports only
whether each key is *present*, never its value.

## Run

```bash
./run.sh                 # creates .venv, installs deps, starts on :8000
# or manually:
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000.

## Tests & linting

```bash
./.venv/bin/python -m pytest tests/ -q     # quarters, extraction, FCF valuation
./.venv/bin/ruff check app tests           # lint (config in pyproject.toml)
```

## Notes & limitations

- Forecast growth and (where a WACC isn't disclosed) the discount rate are
  **assumptions**, surfaced on the summary screen. CapEx that OCR can't reliably
  isolate is proxied by D&A (flagged per method).
- Financials are converted from the detected reporting currency to AUD at the
  valuation-date FX rate (live from Yahoo, with a flagged fallback). If the FX
  fetch fails the fallback rate is used and labelled as such.
- Precedent transactions, factor-loading regressions, and option-implied
  volatility need data no single free feed provides — those methods are marked
  accordingly.
