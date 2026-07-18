from app.main import ComputeIn, api_compute, health


def test_compute_normalizes_manual_rate_overrides_and_reruns_capm():
    response = api_compute(ComputeIn(**{
        "market": "ASX",
        "ticker": "TEST",
        "date": "2026-07-18",
        "docs": [],
        "manual": {},
        "market_overrides": {"beta": 1.1, "risk_free": 4.0, "erp": 5.0},
        "method_overrides": {},
        "use_market": False,
    }))

    capm = next(result for result in response["results"] if result["id"] == 42)
    assert capm["status"] == "ok"
    assert capm["value"] == 0.095


def test_compute_accepts_specialist_method_override():
    response = api_compute(ComputeIn(**{
        "market": "ASX",
        "ticker": "TEST",
        "date": "2026-07-18",
        "method_overrides": {"23": {"value": 900, "intrinsic_ps": 6.4}},
        "use_market": False,
    }))

    precedent = next(result for result in response["results"] if result["id"] == 23)
    assert precedent["status"] == "ok"
    assert precedent["intrinsic_ps"] == 6.4


def test_health_check_is_provider_independent():
    assert health() == {"status": "ok", "service": "securities-valuation-engine"}
