"""FastAPI application: wizard backend for the Securities Valuation Engine."""
from __future__ import annotations

import math
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import UPLOAD_DIR, settings
from .export import build_jpeg, build_pdf
from .market import market_snapshot
from .market.feeds import fx_rate
from .ocr import DOC_TYPES, extract_document
from .quarters import resolve_period
from .valuation import run_valuation
from .valuation.engine import detect_reporting_currency

app = FastAPI(title="Securities Valuation Engine", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/health")
def health():
    """Render health check; deliberately excludes secrets and provider calls."""
    return {"status": "ok", "service": "securities-valuation-engine"}


@app.get("/api/config")
def api_config():
    return {
        "keys": settings.status(),  # {finnhub: bool, fred: bool} — never the values
        "markets": ["ASX", "NASDAQ", "NYSE", "LSE", "TSX", "HKEX", "NSE"],
        "doc_types": [{"key": d.key, "label": d.label,
                       "required": list(d.required)} for d in DOC_TYPES],
    }


class PeriodIn(BaseModel):
    date: str


@app.post("/api/period")
def api_period(body: PeriodIn):
    try:
        d = datetime.strptime(body.date, "%Y-%m-%d").date()
    except ValueError as e:
        raise HTTPException(400, "date must be YYYY-MM-DD") from e
    return resolve_period(d).to_dict()


@app.post("/api/extract")
async def api_extract(file: UploadFile = File(...), doc_type: str = Form("")):
    suffix = Path(file.filename or "upload.pdf").suffix or ".pdf"
    dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    dest.write_bytes(await file.read())
    try:
        res = extract_document(dest, doc_type=doc_type or None)
    except Exception as e:
        raise HTTPException(422, f"Extraction failed: {type(e).__name__}: {e}") from e
    out = res.to_dict()
    out["filename"] = file.filename or dest.name
    out["stored_as"] = dest.name
    return out


class ComputeIn(BaseModel):
    market: str
    ticker: str
    date: str
    docs: list[dict] = Field(default_factory=list)
    manual: dict = Field(default_factory=dict)          # OCR/fundamental overrides
    market_overrides: dict = Field(default_factory=dict)  # market/rate overrides
    method_overrides: dict = Field(default_factory=dict)  # specialist external results
    use_market: bool = True


@app.post("/api/compute")
def api_compute(body: ComputeIn):
    try:
        d = datetime.strptime(body.date, "%Y-%m-%d").date()
    except ValueError as e:
        raise HTTPException(400, "date must be YYYY-MM-DD") from e

    # Fold manual overrides into a synthetic highest-priority document.
    docs = list(body.docs)
    if body.manual:
        manual_fields = {k: {"value": _num(v), "raw": "manual entry", "line": -1}
                         for k, v in body.manual.items() if _num(v) is not None}
        docs.insert(0, {"doc_type": "annual_report", "filename": "manual-input",
                        "fields": manual_fields, "missing_required": []})

    market = {}
    target_ccy = "AUD"          # everything is priced in AUD
    if body.use_market:
        try:
            md = market_snapshot(body.market, body.ticker, d)
            market = md.to_dict()
            target_ccy = md.currency or "AUD"   # e.g. an ASX price is AUD
        except Exception as e:
            market = {"notes": [f"market snapshot failed: {type(e).__name__}"]}

    # Explicit user entries win over external feeds. Percent entries arrive in
    # human units (e.g. 4.35) and are normalized to decimals here; market cap is
    # entered in millions but MarketData stores base currency units.
    percent_keys = {"risk_free", "erp", "hist_vol", "buyback_yield", "wacc", "cost_equity"}
    for key, raw in body.market_overrides.items():
        value = _num(raw)
        if value is None:
            continue
        if key in percent_keys:
            value /= 100.0
        elif key == "market_cap_m":
            key, value = "market_cap", value * 1_000_000
        market[key] = value
        market.setdefault("sources", {})[key] = "manual"

    # Reporting currency comes from the documents; convert it into the target.
    reporting_ccy = detect_reporting_currency(docs) or target_ccy
    fx, fx_live = fx_rate(reporting_ccy, target_ccy, d)
    if fx is None:
        fx, fx_live = 1.0, False

    normalized_method_overrides = {}
    for method_id, payload in body.method_overrides.items():
        if not isinstance(payload, dict):
            continue
        clean = {key: _num(payload.get(key)) for key in ("value", "intrinsic_ps")}
        clean = {key: value for key, value in clean.items() if value is not None}
        if clean:
            normalized_method_overrides[str(method_id)] = clean

    summary = run_valuation(docs, market, currency=target_ccy,
                            reporting_currency=reporting_ccy, fx=fx, fx_live=fx_live,
                            method_overrides=normalized_method_overrides)
    summary["market"] = market
    summary["meta"] = {
        "market": body.market, "ticker": body.ticker, "date": body.date,
        "current_label": resolve_period(d).current.label,
        "currency": target_ccy, "reporting_currency": reporting_ccy,
        "fx": round(fx, 4), "fx_live": fx_live,
    }
    return summary


class ExportIn(BaseModel):
    format: str            # pdf | jpeg
    summary: dict
    meta: dict = Field(default_factory=dict)


@app.post("/api/export")
def api_export(body: ExportIn):
    fmt = body.format.lower()
    meta = body.meta or body.summary.get("meta", {})
    if fmt == "pdf":
        data = build_pdf(body.summary, meta)
        media = "application/pdf"
        ext = "pdf"
    elif fmt in ("jpeg", "jpg"):
        data = build_jpeg(body.summary, meta)
        media = "image/jpeg"
        ext = "jpg"
    else:
        raise HTTPException(400, "format must be pdf or jpeg")
    name = f"valuation-{meta.get('ticker','stock')}-{meta.get('date','')}.{ext}"
    return Response(content=data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _num(v):
    try:
        number = float(str(v).replace(",", "").strip())
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


# Serve the wizard SPA (mounted last so /api/* wins).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
