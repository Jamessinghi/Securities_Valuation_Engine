"""FastAPI application: wizard backend for the Securities Valuation Engine."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import EXPORT_DIR, UPLOAD_DIR, settings
from .export import build_jpeg, build_pdf
from .market import market_snapshot
from .ocr import DOC_TYPES, extract_document
from .quarters import resolve_period
from .valuation import run_valuation

app = FastAPI(title="Securities Valuation Engine", version="0.1.0")

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/api/config")
def api_config():
    return {
        "keys": settings.status(),  # {finnhub: bool, fred: bool} — never the values
        "markets": ["ASX", "NASDAQ", "NYSE", "LSE", "TSX", "HKEX", "NSE"],
        "doc_types": [{"key": d.key, "label": d.label,
                       "required": [dt for dt in d.required]} for d in DOC_TYPES],
    }


class PeriodIn(BaseModel):
    date: str


@app.post("/api/period")
def api_period(body: PeriodIn):
    try:
        d = datetime.strptime(body.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    return resolve_period(d).to_dict()


@app.post("/api/extract")
async def api_extract(file: UploadFile = File(...), doc_type: str = Form("")):
    suffix = Path(file.filename or "upload.pdf").suffix or ".pdf"
    dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    dest.write_bytes(await file.read())
    try:
        res = extract_document(dest, doc_type=doc_type or None)
    except Exception as e:
        raise HTTPException(422, f"Extraction failed: {type(e).__name__}: {e}")
    out = res.to_dict()
    out["filename"] = file.filename or dest.name
    out["stored_as"] = dest.name
    return out


class ComputeIn(BaseModel):
    market: str
    ticker: str
    date: str
    docs: list[dict] = []
    manual: dict = {}          # {field_key: value} manual overrides
    use_market: bool = True


@app.post("/api/compute")
def api_compute(body: ComputeIn):
    try:
        d = datetime.strptime(body.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")

    # Fold manual overrides into a synthetic highest-priority document.
    docs = list(body.docs)
    if body.manual:
        manual_fields = {k: {"value": _num(v), "raw": "manual entry", "line": -1}
                         for k, v in body.manual.items() if _num(v) is not None}
        docs.insert(0, {"doc_type": "annual_report", "filename": "manual-input",
                        "fields": manual_fields, "missing_required": []})

    market = {}
    reporting_ccy = "USD"
    if body.use_market:
        try:
            md = market_snapshot(body.market, body.ticker, d)
            market = md.to_dict()
        except Exception as e:
            market = {"notes": [f"market snapshot failed: {type(e).__name__}"]}

    summary = run_valuation(docs, market, currency=reporting_ccy)
    summary["market"] = market
    summary["meta"] = {
        "market": body.market, "ticker": body.ticker, "date": body.date,
        "current_label": resolve_period(d).current.label,
    }
    return summary


class ExportIn(BaseModel):
    format: str            # pdf | jpeg
    summary: dict
    meta: dict = {}


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
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# Serve the wizard SPA (mounted last so /api/* wins).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
