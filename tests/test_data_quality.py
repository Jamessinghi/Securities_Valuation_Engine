from app.data import canonicalize, reconciliation
from app.valuation.engine import _triangulate, accounting_checks, merge_fundamentals
from app.valuation.methods import SPEC, Ctx, MethodResult


def test_canonical_merge_preserves_audit_fields_and_prefers_confidence():
    docs = [
        {"doc_type": "annual_report", "filename": "annual.pdf", "currency": "USD",
         "period_end": "2025-12-31", "fields": {"revenue": {"value": 10, "confidence": "low"}}},
        {"doc_type": "quarterly", "filename": "quarter.pdf", "currency": "USD",
         "period_end": "2025-12-31", "fields": {"revenue": {"value": 12, "confidence": "high"}}},
    ]
    cell = merge_fundamentals(docs)["revenue"]
    assert cell == canonicalize(
        12, currency="USD", units="USD_m", source="quarter.pdf", source_type="filing",
        confidence="high", period_start="2025-10-01", period_end="2025-12-31",
        as_of_date="2025-12-31", is_estimated=False)


def test_accounting_equation_reconciliation_passes():
    fields = {key: {"value": value, "confidence": "high"} for key, value in {
        "total_assets": 1000, "total_liabilities": 600, "total_equity": 400}.items()}
    checks = accounting_checks(fields)
    assert checks[0]["status"] == "passed"
    assert reconciliation("x", 100, 101, tolerance=0.02)["status"] == "passed"


def test_wacc_is_calculated_not_taken_from_disclosed_impairment_rate():
    ctx = Ctx(
        {"discount_rate": {"value": 15}, "borrowings_total": {"value": 200},
         "interest_expense": {"value": 10}, "income_tax": {"value": -30},
         "net_profit": {"value": 70}},
        {"price": 10, "shares_outstanding": 100_000_000, "market_cap": 1_000_000_000,
         "beta": 1.0, "risk_free": 0.04, "erp": 0.05},
    )
    # E=1000m, D=200m, ke=9%, kd=5%, tax=30% => 8.0833%
    assert round(ctx.wacc, 6) == 0.080833
    assert ctx.disc_disclosed == 0.15


def test_filing_share_counts_are_normalized_from_millions():
    ctx = Ctx({"wtd_avg_shares": {"value": 3200}}, {})
    assert ctx.shares == 3_200_000_000
    assert ctx.per_share(3200) == 1.0


def test_triangulation_counts_correlated_methods_once_per_family():
    results = [
        MethodResult(1, "DCF", "FCFF", "ok", intrinsic_ps=4),
        MethodResult(2, "DCF", "FCFE", "ok", intrinsic_ps=6),
        MethodResult(3, "DDM", "Gordon", "partial", intrinsic_ps=9),
        MethodResult(4, "DDM", "Two stage", "partial", intrinsic_ps=10),
        MethodResult(5, "DDM", "H", "partial", intrinsic_ps=11),
        MethodResult(10, "Floor", "NCAV", "ok", intrinsic_ps=-20),
    ]
    value, families = _triangulate(results)
    assert value == 7.5  # median of cash-flow family 5 and dividend family 10
    assert len(families) == 2


def test_monte_carlo_is_a_real_reproducible_distribution():
    ctx = Ctx(
        {"ebit": {"value": 900}, "dna": {"value": 250}, "capex": {"value": 300},
         "borrowings_total": {"value": 1000}, "cash": {"value": 200},
         "wtd_avg_shares": {"value": 1000}},
        {"wacc": 0.09},
    )
    spec = SPEC[51]
    first = spec["fn"](ctx, spec)
    second = spec["fn"](ctx, spec)
    assert first.intrinsic_ps == second.intrinsic_ps
    assert first.intrinsic_ps > 0
    assert "P10=" in first.note and "P90=" in first.note
