# Securities Valuation Engine

Upload a company's trailing-twelve-month filing set, OCR-extract the financials,
pull free market data, and run **65 valuation methods** to triangulate an
intrinsic value — for any listed security, on any chosen date.

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
  market/            free feeds: Yahoo/Stooq, Finnhub, FRED, Ken French, Damodaran
  valuation/         the 65 methods + triangulation engine
  export/            PDF (reportlab) + JPEG (Pillow)
  main.py            FastAPI routes
  static/            vanilla-JS wizard SPA
```

## Free data sources

| Need | Source | Key? |
|------|--------|------|
| Daily price, beta, volatility, shares | Yahoo Finance (`yfinance`), Stooq | no |
| Analyst consensus / price targets | Finnhub | `FINNHUB_API_KEY` |
| Risk-free yield + macro | FRED | `FRED_API_KEY` |
| Fama-French / momentum factors | Ken French (`pandas-datareader`) | no |
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

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q
```

## Notes & limitations

- Forecast growth and (where a WACC isn't disclosed) the discount rate are
  **assumptions**, surfaced on the summary screen. CapEx that OCR can't reliably
  isolate is proxied by D&A (flagged per method).
- Financials are read in the report's reporting currency; if the market price is
  in a different currency (e.g. USD financials vs an AUD share price), the
  summary flags the mismatch rather than silently mixing them.
- Precedent transactions, factor-loading regressions, and option-implied
  volatility need data no single free feed provides — those methods are marked
  accordingly.
