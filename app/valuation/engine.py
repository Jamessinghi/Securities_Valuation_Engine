"""Valuation orchestration: merge extracted docs, run all 65 methods, triangulate.

Everything is priced in a single **target currency** (AUD by default). The
reporting currency is taken from the uploaded documents (OCR-detected), and an
FX rate for the valuation date converts the financials into the target currency
so the triangulated intrinsic value is directly comparable with the (AUD) market
price.
"""
from __future__ import annotations

from statistics import median

from .methods import SPEC, Ctx, MethodResult

# When the same field appears in several uploaded documents, prefer this order.
_DOC_PRIORITY = ("annual_report", "half_year", "results_presentation", "quarterly", "agm", "other")


def merge_fundamentals(docs: list[dict]) -> dict:
    """Merge per-document extracted fields into one fundamentals dict.

    ``docs`` is a list of DocResult.to_dict(). Higher-priority documents win.
    """
    order = {k: i for i, k in enumerate(_DOC_PRIORITY)}
    merged: dict[str, dict] = {}
    for doc in sorted(docs, key=lambda d: order.get(d.get("doc_type", "other"), 99)):
        for key, cell in (doc.get("fields") or {}).items():
            if key not in merged:  # first (highest-priority) doc to supply it wins
                merged[key] = {"value": cell.get("value"), "source": doc.get("filename")}
    return merged


def detect_reporting_currency(docs: list[dict]) -> str | None:
    """Reporting currency from the highest-priority document that declares one."""
    order = {k: i for i, k in enumerate(_DOC_PRIORITY)}
    for doc in sorted(docs, key=lambda d: order.get(d.get("doc_type", "other"), 99)):
        if doc.get("currency"):
            return doc["currency"]
    return None


def build_context(docs: list[dict], market: dict | None, currency: str = "AUD",
                  reporting_currency: str | None = None, fx: float = 1.0,
                  fx_live: bool = True) -> Ctx:
    fundamentals = merge_fundamentals(docs)
    return Ctx(fundamentals, market, currency=currency,
               reporting_currency=reporting_currency, fx=fx, fx_live=fx_live)


def run_valuation(docs: list[dict], market: dict | None, currency: str = "AUD",
                  reporting_currency: str | None = None, fx: float = 1.0,
                  fx_live: bool = True) -> dict:
    ctx = build_context(docs, market, currency=currency,
                        reporting_currency=reporting_currency, fx=fx, fx_live=fx_live)
    results: list[MethodResult] = []
    for spec in SPEC:
        try:
            res = spec["fn"](ctx, spec)
        except Exception as e:  # never let one method break the run
            res = MethodResult(id=spec["id"], section=spec["section"], name=spec["name"],
                               status="na", note=f"error: {type(e).__name__}")
        results.append(res)

    # Triangulated intrinsic value: median of per-share fair values that were
    # actually computed (ok/partial), in the reporting currency.
    ps_values = [r.intrinsic_ps for r in results
                 if r.intrinsic_ps is not None and r.status in ("ok", "partial")]
    intrinsic = median(ps_values) if ps_values else None

    counts = {"ok": 0, "partial": 0, "na": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    verdict = None
    if intrinsic is not None and ctx.price is not None:
        # only meaningful when currencies match; flagged otherwise
        gap = (intrinsic - ctx.price) / ctx.price
        verdict = {
            "intrinsic": round(intrinsic, 3),
            "price": ctx.price,
            "upside": round(gap, 4),
            "currency": ctx.currency,
            "currency_price": ctx.market_ccy,
            "comparable": ctx.market_ccy == ctx.currency,
        }

    return {
        "results": [r.to_dict() for r in results],
        "intrinsic_value_per_share": round(intrinsic, 3) if intrinsic is not None else None,
        "intrinsic_currency": ctx.currency,
        "reporting_currency": ctx.reporting_currency,
        "fx": ctx.fx,
        "n_intrinsic_models": len(ps_values),
        "counts": counts,
        "verdict": verdict,
        "assumptions": ctx.assumptions,
        "context": {
            "shares": ctx.shares, "price": ctx.price, "market_cap": ctx.market_cap,
            "wacc": ctx.wacc, "cost_equity": ctx.cost_equity, "beta": ctx.beta,
            "net_debt": ctx.net_debt, "revenue": ctx.revenue, "ebit": ctx.ebit,
            "net_income": ctx.net_income, "book_equity": ctx.book_equity,
            "eps": ctx.eps, "dps": ctx.dps,
        },
    }
