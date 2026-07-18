from app.valuation.engine import run_valuation


def _docs(**values):
    return [{
        "doc_type": "annual_report",
        "filename": "test.pdf",
        "fields": {key: {"value": value} for key, value in values.items()},
    }]


def test_missing_market_input_exposes_normalized_completion_field():
    summary = run_valuation(
        _docs(eps_basic=100),
        market={},
    )
    capm = next(result for result in summary["results"] if result["id"] == 42)

    assert capm["status"] == "na"
    assert any(field["key"] == "beta" for field in capm["completion"])
    assert any(field["key"] == "risk_free" for field in capm["completion"])
    assert any(field["key"] == "erp" for field in capm["completion"])


def test_specialist_method_requests_external_result_not_fake_scalar_inputs():
    summary = run_valuation([], market={})
    precedent = next(result for result in summary["results"] if result["id"] == 23)

    assert precedent["completion"][0]["scope"] == "method"
    assert precedent["completion"][0]["key"] == "value"
    assert precedent["completion"][1]["key"] == "intrinsic_ps"


def test_partial_assumption_method_can_be_completed_with_external_result():
    summary = run_valuation(
        _docs(total_assets=1_000, total_liabilities=400, cash=100, wtd_avg_shares=100_000_000),
        market={},
    )
    liquidation = next(result for result in summary["results"] if result["id"] == 11)

    assert liquidation["status"] == "partial"
    assert liquidation["completion"][0]["scope"] == "method"


def test_longest_missing_input_name_wins_for_ebitda():
    summary = run_valuation([], market={"market_cap": 1_000_000_000})
    ev_ebitda = next(result for result in summary["results"] if result["id"] == 19)

    assert any(field["key"] == "ebitdax" for field in ev_ebitda["completion"])
    assert not any(field["key"] == "ebit" for field in ev_ebitda["completion"])


def test_user_supplied_specialist_fair_value_updates_triangulation():
    summary = run_valuation(
        [],
        market={},
        method_overrides={"23": {"value": 1_200, "intrinsic_ps": 7.25}},
    )
    precedent = next(result for result in summary["results"] if result["id"] == 23)

    assert precedent["status"] == "ok"
    assert precedent["value"] == 1_200
    assert precedent["intrinsic_ps"] == 7.25
    assert summary["intrinsic_value_per_share"] == 7.25
    assert "User-supplied external result" in precedent["note"]


def test_factor_regression_result_is_used_by_factor_method():
    model = {
        "expected_return": 0.091,
        "alpha_annual": 0.01,
        "loadings": {"Mkt-RF": 1.1, "SMB": -0.2, "HML": 0.3},
        "r_squared": 0.72,
        "observations": 420,
    }
    summary = run_valuation(
        [],
        market={"factor_models": {"ff3": model}},
    )
    ff3 = next(result for result in summary["results"] if result["id"] == 43)

    assert ff3["status"] == "ok"
    assert ff3["value"] == 0.091
    assert "420 aligned daily returns" in ff3["note"]


def test_macro_regression_result_completes_apt_method():
    model = {"expected_return": 0.085, "alpha_annual": 0.01,
             "loadings": {"oil": 0.4, "inflation": -0.2, "production": 0.1},
             "r_squared": 0.31, "observations": 60, "as_of": "2025-12-31"}
    summary = run_valuation([], market={"macro_model": model})
    apt = next(result for result in summary["results"] if result["id"] == 46)
    assert apt["status"] == "ok"
    assert apt["value"] == 0.085
