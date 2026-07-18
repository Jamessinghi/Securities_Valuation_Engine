"""Valuation orchestration: merge extracted docs, run all 65 methods, triangulate.

Everything is priced in a single **target currency** (AUD by default). The
reporting currency is taken from the uploaded documents (OCR-detected), and an
FX rate for the valuation date converts the financials into the target currency
so the triangulated intrinsic value is directly comparable with the (AUD) market
price.
"""
from __future__ import annotations

import math
from datetime import date
from statistics import median

from ..data import canonicalize, confidence_rank, reconciliation
from ..ocr.fields import FIELD_BY_KEY
from .methods import SPEC, Ctx, MethodResult

# Map human-readable missing-input messages to concrete normalized inputs. The
# UI uses this metadata to request the right unit and the compute endpoint feeds
# the value back through OCR fundamentals or the market context before rerunning
# every method.
_COMPLETION_INPUTS = {
    "ebit": ("manual", "ebit", "EBIT", "Reporting-currency millions"),
    "d&a": ("manual", "dna", "Depreciation & amortisation", "Reporting-currency millions"),
    "operating cash flow": ("manual", "op_cash_flow", "Operating cash flow", "Reporting-currency millions"),
    "capex": ("manual", "capex", "Capital expenditure", "Positive reporting-currency millions"),
    "fcf": ("manual", "free_cash_flow", "Free cash flow", "Reporting-currency millions"),
    "net income": ("manual", "net_profit", "Net income", "Reporting-currency millions"),
    "book equity": ("manual", "total_equity", "Book equity", "Reporting-currency millions"),
    "total equity": ("manual", "total_equity", "Total equity", "Reporting-currency millions"),
    "current assets": ("manual", "total_current_assets", "Current assets", "Reporting-currency millions"),
    "total liabilities": ("manual", "total_liabilities", "Total liabilities", "Reporting-currency millions"),
    "total assets": ("manual", "total_assets", "Total assets", "Reporting-currency millions"),
    "eps": ("manual", "eps_basic", "EPS", "Reporting-currency cents per share"),
    "dps": ("manual", "dps", "Dividend per share", "Reporting-currency cents per share"),
    "dividends per share": ("manual", "dps", "Dividend per share", "Reporting-currency cents per share"),
    "debt": ("manual", "borrowings_total", "Interest-bearing debt", "Reporting-currency millions"),
    "ebitda": ("manual", "ebitdax", "EBITDA", "Reporting-currency millions"),
    "sales": ("manual", "revenue", "Revenue", "Reporting-currency millions"),
    "wacc": ("market", "wacc", "WACC", "Percent, e.g. 8.50"),
    "discount rate": ("market", "wacc", "Discount rate / WACC", "Percent, e.g. 8.50"),
    "cost of equity": ("market", "cost_equity", "Cost of equity", "Percent, e.g. 9.25"),
    "price": ("market", "price", "Market price", "Traded currency per share"),
    "market price": ("market", "price", "Market price", "Traded currency per share"),
    "market cap": ("market", "market_cap_m", "Market capitalisation", "Traded-currency millions"),
    "shares": ("market", "shares_outstanding", "Shares outstanding", "Number of shares"),
    "beta": ("market", "beta", "Equity beta", "Unitless"),
    "risk-free": ("market", "risk_free", "Risk-free rate", "Percent, e.g. 4.35"),
    "erp": ("market", "erp", "Equity risk premium", "Percent, e.g. 4.50"),
    "equity vol": ("market", "hist_vol", "Annualised equity volatility", "Percent"),
    "volatility": ("market", "hist_vol", "Annualised volatility", "Percent"),
    "forward earnings": ("market", "forward_eps", "Forward EPS", "Traded currency per share"),
    "buybacks": ("market", "buyback_yield", "Buyback yield", "Percent"),
    "analyst consensus": ("market", "target_mean", "Consensus mean price target", "Traded currency per share"),
}


def _completion_fields(result: MethodResult) -> list[dict]:
    """Return de-duplicated entry fields for a result's missing inputs."""
    fields = []
    seen = set()
    for missing in result.missing:
        low = missing.lower()
        match = next(
            (spec for phrase, spec in sorted(_COMPLETION_INPUTS.items(), key=lambda item: -len(item[0]))
             if phrase in low),
            None,
        )
        if match and (match[0], match[1]) not in seen:
            scope, key, label, unit = match
            fields.append({"scope": scope, "key": key, "label": label, "unit": unit, "required": True})
            seen.add((scope, key))
    if fields:
        return fields
    # Specialist methods need an externally prepared appraisal/model output.
    return [
        {"scope": "method", "key": "value", "label": "Externally calculated method value",
         "unit": result.unit or "Use the unit described by the method", "required": True},
        {"scope": "method", "key": "intrinsic_ps", "label": "Fair value per share (if applicable)",
         "unit": "Target currency per share", "required": False},
    ]

# When the same field appears in several uploaded documents, prefer this order.
_DOC_PRIORITY = ("annual_report", "half_year", "results_presentation", "quarterly", "agm", "other")

# Headline triangulation uses one observation per independent valuation family.
# Accounting floors, ratios, terminal values, market price and technical signals
# remain visible but do not pretend to be going-concern intrinsic values.
_TRIANGULATION_FAMILIES = {
    "cash_flow": {1, 2, 7, 13, 40, 52, 53, 61},
    "dividends": {3, 4, 5},
    "residual_income": {6, 8, 34, 38},
    "justified_fundamentals": {26, 27, 30, 31, 32},
    "market_relative": {23, 24, 57},
    "sum_of_parts": {62},
}


def _triangulate(results: list[MethodResult]) -> tuple[float | None, list[dict]]:
    """Median within each independent family, then median across families."""
    families = []
    for name, ids in _TRIANGULATION_FAMILIES.items():
        members = [result for result in results if result.id in ids and result.status in ("ok", "partial")
                   and result.intrinsic_ps is not None and math.isfinite(result.intrinsic_ps)
                   and result.intrinsic_ps > 0]
        if not members:
            continue
        values = [result.intrinsic_ps for result in members]
        families.append({
            "family": name,
            "value": median(values),
            "methods": [result.id for result in members],
            "ok_methods": sum(result.status == "ok" for result in members),
            "partial_methods": sum(result.status == "partial" for result in members),
        })
    intrinsic = median([family["value"] for family in families]) if families else None
    return intrinsic, families


def merge_fundamentals(docs: list[dict]) -> dict:
    """Merge per-document extracted fields into one fundamentals dict.

    ``docs`` is a list of DocResult.to_dict(). Higher-priority documents win.
    """
    order = {k: i for i, k in enumerate(_DOC_PRIORITY)}
    merged: dict[str, dict] = {}
    for doc in sorted(docs, key=lambda d: order.get(d.get("doc_type", "other"), 99)):
        for key, cell in (doc.get("fields") or {}).items():
            candidate = canonicalize(
                cell.get("value"), currency=doc.get("currency"),
                units=(FIELD_BY_KEY.get(key).unit if FIELD_BY_KEY.get(key) else "unknown"),
                source=doc.get("filename", "uploaded document"),
                source_type="manual" if doc.get("filename") == "manual-input" else "filing",
                confidence="high" if doc.get("filename") == "manual-input" else cell.get("confidence", "medium"),
                period_start=doc.get("period_start") or _infer_period_start(doc), period_end=doc.get("period_end"),
                as_of_date=doc.get("as_of_date") or doc.get("period_end"),
                is_estimated=False,
            )
            existing = merged.get(key)
            if existing is None or confidence_rank(candidate["confidence"]) > confidence_rank(existing["confidence"]):
                merged[key] = candidate
    return merged


def _infer_period_start(doc: dict) -> str | None:
    end = doc.get("period_end")
    if not end:
        return None
    try:
        end_date = date.fromisoformat(end)
        months = {"annual_report": 12, "half_year": 6, "quarterly": 3}.get(doc.get("doc_type"))
        if not months:
            return None
        month_index = end_date.year * 12 + end_date.month - months
        return date(month_index // 12, month_index % 12 + 1, 1).isoformat()
    except (TypeError, ValueError):
        return None


def accounting_checks(f: dict) -> list[dict]:
    """Run statement cross-checks without manufacturing missing values."""
    val = lambda key: (f.get(key) or {}).get("value")  # noqa: E731
    checks = [
        reconciliation("Assets = liabilities + equity", val("total_assets"),
                       _sum_known(val("total_liabilities"), val("total_equity")),
                       inputs=["total_assets", "total_liabilities", "total_equity"]),
        reconciliation("Opening cash + movement = closing cash", val("cash"),
                       _sum_known(val("cash_opening"), val("cash_movement")),
                       inputs=["cash_opening", "cash_movement", "cash"]),
        reconciliation("Operating cash flow - CapEx = free cash flow",
                       val("free_cash_flow"), _subtract_known(val("op_cash_flow"), val("capex")),
                       tolerance=0.10, inputs=["op_cash_flow", "capex", "free_cash_flow"]),
        reconciliation("Basic EPS = attributable profit / weighted-average shares",
                       _eps_currency(val("eps_basic")), _per_share(val("profit_attributable"), val("wtd_avg_shares")),
                       tolerance=0.05, inputs=["eps_basic", "profit_attributable", "wtd_avg_shares"]),
        reconciliation("Debt total = debt-note components", val("borrowings_total"),
                       _sum_known(val("borrowings_current"), val("borrowings_noncurrent")), tolerance=0.03,
                       inputs=["borrowings_total", "borrowings_current", "borrowings_noncurrent"]),
    ]
    failed = {key for check in checks if check["status"] == "failed" for key in check["inputs"]}
    for key in failed:
        if key in f:
            f[key]["confidence"] = "low"
    return checks


def _sum_known(a, b):
    return None if a is None or b is None else a + b


def _subtract_known(a, b):
    return None if a is None or b is None else a - abs(b)


def _eps_currency(value):
    return None if value is None else value / 100.0


def _per_share(value_m, shares_m):
    return None if value_m is None or not shares_m else value_m / shares_m


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
    checks = accounting_checks(fundamentals)
    if market is not None:
        market.setdefault("accounting_checks", checks)
        market.setdefault("canonical_inputs", {})
    return Ctx(fundamentals, market, currency=currency,
               reporting_currency=reporting_currency, fx=fx, fx_live=fx_live)


def run_valuation(docs: list[dict], market: dict | None, currency: str = "AUD",
                  reporting_currency: str | None = None, fx: float = 1.0,
                  fx_live: bool = True, method_overrides: dict | None = None) -> dict:
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

    supplied = method_overrides or {}
    for result in results:
        override = supplied.get(str(result.id)) or supplied.get(result.id)
        if override:
            value = override.get("value")
            intrinsic = override.get("intrinsic_ps")
            result.value = float(value) if value is not None else result.value
            result.intrinsic_ps = float(intrinsic) if intrinsic is not None else result.intrinsic_ps
            result.status = "ok"
            result.missing = []
            result.note = "User-supplied external result; verify source and units. " + result.note
        elif result.status in ("partial", "na"):
            result.completion = _completion_fields(result)

    intrinsic, family_values = _triangulate(results)

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
        "n_intrinsic_models": sum(len(family["methods"]) for family in family_values),
        "n_intrinsic_families": len(family_values),
        "triangulation": {
            "method": "median of independent family medians",
            "families": family_values,
            "excluded": "accounting floors, ratios, terminal values, market price, and technical signals",
        },
        "counts": counts,
        "verdict": verdict,
        "assumptions": ctx.assumptions,
        "data_quality": {
            "accounting_checks": (market or {}).get("accounting_checks", []),
            "canonical_inputs": {**ctx.canonical_fundamentals, **ctx.canonical_market},
        },
        "context": {
            "shares": ctx.shares, "price": ctx.price, "market_cap": ctx.market_cap,
            "wacc": ctx.wacc, "cost_equity": ctx.cost_equity, "beta": ctx.beta,
            "net_debt": ctx.net_debt, "revenue": ctx.revenue, "ebit": ctx.ebit,
            "net_income": ctx.net_income, "book_equity": ctx.book_equity,
            "eps": ctx.eps, "dps": ctx.dps,
        },
    }
