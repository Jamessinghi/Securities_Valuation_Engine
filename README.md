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
   A method that still lacks an input has a **Complete?** button. Entering a
   filing, market, rate, or specialist external result reruns all 65 methods and
   refreshes both the affected rows and the triangulated value.

The headline is the median of independent valuation-family medians, not the
median of every row. Cash flow, dividends, residual income, justified
fundamentals, market-relative evidence and SOTP each receive at most one vote.
Accounting floors, ratios, standalone terminal values, market price and
technical signals remain visible but are excluded from intrinsic triangulation;
negative and non-finite per-share results are also excluded. The API exposes
the exact included method IDs under `triangulation.families`.

## Architecture

```
app/
  data.py            canonical input records + accounting reconciliations
  quarters.py        date -> current quarter + previous 4 completed quarters
  ocr/               bounded pypdf extraction + field schema (+ Tesseract fallback)
    fields.py        canonical field schema; AU/IFRS + US-GAAP label synonyms
    extractor.py     currency/scale detection, candidate scoring, confidence
  market/            free feeds + FX: Yahoo/Stooq, Finnhub, FRED, Ken French, Damodaran
    public_sources.py SEC company facts/search, ASX research links, AQR catalog
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
- **Free-tier-safe large reports** — uploads are written in 1 MB chunks rather
  than buffered in memory. Digital filings are scanned with `pypdf`, then the
  extractor retains at most 80 high-value pages: the cover/identity pages plus
  financial statements, equity, EPS, debt, dividend and segment-note pages and
  their neighbours. The original and processed page counts remain auditable.
- **Bounded OCR** — image-only PDFs are rendered one page at a time at 160 DPI
  grayscale, with no more than 40 OCR pages per request. Images are closed
  immediately after recognition instead of retaining a document-sized batch.
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
- **Statement and note context** — matches are tagged and scored inside the
  income statement, balance sheet, cash-flow statement, statement of changes
  in equity, segment note, debt note, share/EPS note and dividend note. A label
  in the correct statement beats the same words in narrative commentary.
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

### Canonical input and audit layer

Every filing, manual and market observation is normalized before a valuation
method sees it. The API returns this ledger under `data_quality.canonical_inputs`:

```json
{
  "value": 6150,
  "currency": "USD",
  "units": "USD_m",
  "period_start": "2025-01-01",
  "period_end": "2025-12-31",
  "as_of_date": "2026-02-20",
  "source": "annual-report.pdf",
  "source_type": "filing",
  "confidence": "high",
  "is_estimated": false
}
```

Monetary statement inputs use millions of reporting currency, filing share
counts use millions and are converted to absolute shares in the valuation
context, per-share values retain their declared units, and rates are decimals
internally. Higher-confidence observations can supersede lower-confidence
matches while retaining an explicit source.

Before calculation the engine performs these reconciliations and returns them
under `data_quality.accounting_checks`:

- assets versus liabilities plus equity;
- opening cash plus net movement versus closing cash;
- operating cash flow less CapEx versus reported FCF;
- basic EPS versus attributable profit divided by weighted-average shares; and
- total debt versus current plus non-current debt-note components.

Checks use relative materiality tolerances and report `passed`, `failed`, or
`not_tested`. Fields involved in a failed check are downgraded to low confidence,
so dependent methods remain `PARTIAL` rather than receiving a false `OK`.

### Calculated WACC

The impairment-note discount rate is no longer used as corporate WACC. The
engine calculates:

```text
cost of equity = risk-free rate + beta × equity risk premium
pre-tax debt cost = interest expense ÷ interest-bearing debt
WACC = E/(D+E) × cost of equity + D/(D+E) × debt cost × (1-tax rate)
```

Equity is weighted at valuation-date market value; debt uses the statement/debt
note; the effective normalized tax rate is used when available. If debt cost is
missing, the risk-free rate is an explicitly auditable fallback. A disclosed
asset discount rate is shown only as a cross-check. Manual WACC remains an
explicit override and is labelled as such.

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
| Raw close, adjusted close, beta, volatility, shares | Yahoo Finance (`yfinance`), Stooq | no |
| Analyst consensus / forward EPS / buybacks | Finnhub, Yahoo fallback | `FINNHUB_API_KEY` |
| Valuation-date risk-free yield | FRED | `FRED_API_KEY` |
| FF3 / FF5 / Carhart factors and regressions | Ken French (`pandas-datareader`) | no |
| FX rate (reporting currency → AUD) | Yahoo Finance (`{FROM}{TO}=X`) | no |
| Equity risk premium | Damodaran current workbook; dated fallback | no |
| Recent global/ASX EOD price fallback | EODHD free tier | `EODHD_API_KEY` (optional) |
| US peer facts and merger/proxy documents | SEC Company Facts + EDGAR full text | no |
| ASX scheme/IER source documents | Official ASX company announcements | no |
| Factor backup/catalog | AQR public datasets | no |

The market pipeline also computes SMA(20/50/200), RSI(14), MACD and historical
volatility from closes no later than the valuation date. Factor-model results
are OLS regressions of the security's aligned daily excess returns on the Ken
French factor series, with observations, loadings and R² shown in method notes.
Stooq is a best-effort keyless fallback when Yahoo history is unavailable.
Beta uses five years of weekly raw/adjusted-price history and applies the
standard Blume adjustment toward 1.0; both raw and adjusted beta are retained.
An APT-style method regresses monthly security returns against point-in-time
FRED oil, inflation, industrial-production and term-spread factors when at
least 36 aligned observations exist. For AUD securities, official RBA Table F2
is a keyless fallback for the Australian Government 10-year yield.

Monte Carlo is a reproducible 10,000-draw FCFF simulation rather than a renamed
point estimate. It varies starting cash flow, high-growth rate, terminal growth
and WACC, rejects invalid WACC/terminal-growth combinations, and reports P10,
median and P90. Scenario analysis independently recalculates bear/base/bull
values using explicit growth, terminal-growth and WACC shocks rather than an
arbitrary percentage around the base price.
Exact `(market, ticker, valuation date)` snapshots are cached in `.cache/`
(git-ignored). Price output explicitly contains `raw_close`, `adjusted_close`,
`price_type`, and the actual trading-day `as_of`; valuation comparisons use the
raw close, while total-return calculations use adjusted history.

### Point-in-time discipline

- Prices, technical indicators, beta, volatility and FRED yields are bounded by
  the selected valuation date.
- Current Yahoo/Finnhub analyst targets, forward EPS and current shares are used
  only for a current valuation. They are deliberately not backfilled into a
  historical valuation because that would introduce look-ahead bias.
- Historical consensus requires a paid point-in-time archive or a dated value
  entered through the row's **Complete?** workflow.
- Every market response includes source flags and notes; unavailable feeds do
  not silently become zero.

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

`EODHD_API_KEY` is optional. Its free plan currently provides 20 calls/day and
one year of EOD history; the engine calls it only if Yahoo and Stooq fail and
the valuation date is inside that free historical window. It is useful for ASX
resilience but does not replace the five-year Yahoo history needed for beta.

The FRED integration observes the provider's required attribution: “This
product uses the FRED® API but is not endorsed or certified by the Federal
Reserve Bank of St. Louis.”

### Row-level completion and recalculation

For an unavailable/partial method with explicit missing inputs, **Complete?**
requests the normalized engine input and its unit. Common entries include:

- reporting-currency millions for statement fields such as EBIT, CapEx and FCF;
- reporting-currency cents for OCR EPS/DPS fields;
- traded-currency price, price target or forward EPS;
- percentages for risk-free rate, ERP, volatility, buyback yield and WACC; and
- unitless beta or absolute share count.

The API normalizes percentage and market-cap units, gives manual values explicit
precedence over feeds, rebuilds the valuation context and runs all 65 methods.
For methods that inherently require an external study or dataset—precedent M&A,
replacement-cost appraisal, SOTP, or real options—the form instead accepts the
externally calculated method value and optional fair value per share. These are
labelled as user-supplied in the notes; only a supplied per-share value enters
the headline triangulation.

## Run

```bash
./run.sh                 # creates .venv, installs deps, starts on :8000
# or manually:
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000.

## Hosted deployment: GitHub Pages + Render

The production deployment is intentionally split because GitHub Pages serves
static files and cannot execute the Python/FastAPI calculation engine.

- Frontend: `https://jamessinghi.github.io/Securities_Valuation_Engine/`
- Backend: `https://jamessinghi-securities-valuation-engine-yjua.onrender.com`
- Health check: `https://jamessinghi-securities-valuation-engine-yjua.onrender.com/health`

The workflow at `.github/workflows/deploy-pages.yml` publishes `app/static/`
after relevant pushes to `main`. Static assets use relative URLs so the site
works under the repository subpath. `config.js` uses same-origin API calls
locally and switches to the Render origin only on `github.io`.

The root `render.yaml` defines a free Python web service, health check,
auto-deployment and CORS allow-list. To create it:

1. Open `https://dashboard.render.com/blueprints` and create a Blueprint from
   `https://github.com/Jamessinghi/Securities_Valuation_Engine`.
2. Render detects `render.yaml`. Supply `FINNHUB_API_KEY` and `FRED_API_KEY` in
   its secret prompts; `EODHD_API_KEY` remains optional.
3. Confirm the proposed service name
   `jamessinghi-securities-valuation-engine`, then deploy.
4. Check `/health` returns `{"status":"ok",...}` before using the Pages site.

Secrets exist only in Render's environment. They are never embedded in the
GitHub Pages JavaScript. The backend accepts browser requests only from
`https://jamessinghi.github.io` in production. Render free services can spin
down when idle, so the first request after inactivity may take longer.

### Hosted PDF resource limits

Digital PDFs are parsed on the user's device with PDF.js. The browser sends
page text to `/api/extract-text`, and the backend applies the same targeted-page
selection, statement context and canonical field scoring as a normal upload.
The original PDF therefore never enters Render memory on the common path.
Progress is displayed while pages are read, and the result records
`method=browser-text` for auditability.

Image-only/scanned documents, unavailable browser PDF.js, or local extraction
errors automatically fall back to `/api/extract`, where the bounded server OCR
pipeline remains available. API keys, market feeds, canonical matching and the
valuation engine stay server-side; no provider secret is exposed to Pages.

The defaults are 50 MB per fallback upload, 20 MB of browser-extracted text,
500 source pages, 80 retained text pages and 40 OCR pages. They can be changed
with `MAX_UPLOAD_MB`, `MAX_BROWSER_TEXT_MB`, `MAX_PDF_PAGES`,
`MAX_TEXT_PAGES` and `MAX_OCR_PAGES`. Exceeding a hard limit returns HTTP 413
with a specific UI message. Temporary fallback uploads are deleted after
extraction, including failure paths. A targeted extraction adds a warning such
as `80/286 targeted pages` instead of silently pretending every page was
processed.

The Pages frontend loads the version-pinned PDF.js module and worker from
jsDelivr. If the CDN is unavailable, the application transparently attempts the
original server extraction route. PDF page processing yields to the browser
regularly and PDF.js performs parsing in its own worker, keeping the wizard
responsive during large filings.

The synchronous backend design is intentional for the free deployment:
Render's local filesystem is ephemeral and the service sleeps when idle, so an
in-process background queue could lose jobs. The browser now supplies the
expensive PDF parsing compute; a durable asynchronous queue should only be
introduced together with external object storage and a persistent job store.

## Tests & linting

```bash
./.venv/bin/python -m pytest tests/ -q     # quarters, extraction, FCF valuation
./.venv/bin/ruff check app tests           # lint (config in pyproject.toml)
```

## Notes & limitations

- Forecast growth remains an **assumption**, surfaced on the summary screen.
  WACC is calculated from capital-market and statement inputs. CapEx that OCR can't reliably
  isolate is proxied by D&A (flagged per method).
- Financials are converted from the detected reporting currency to AUD at the
  valuation-date FX rate (live from Yahoo, with a flagged fallback). If the FX
  fetch fails the fallback rate is used and labelled as such.
- Precedent transactions, factor-loading regressions, and option-implied
  volatility need data no single free feed provides — those methods are marked
  accordingly.
