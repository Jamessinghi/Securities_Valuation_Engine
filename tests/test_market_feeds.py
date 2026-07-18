from datetime import date

import requests

from app.config import settings
from app.market.feeds import MarketData, _eodhd_fallback, _risk_free


class _Response:
    ok = True

    @staticmethod
    def json():
        return {"observations": [
            {"date": "2024-12-31", "value": "."},
            {"date": "2024-12-30", "value": "4.25"},
        ]}


def test_fred_risk_free_is_bounded_by_valuation_date(monkeypatch):
    captured = {}

    def fake_get(url, params, timeout):
        captured.update({"url": url, "params": params, "timeout": timeout})
        return _Response()

    monkeypatch.setattr(settings, "fred_api_key", "test-key")
    monkeypatch.setattr(requests, "get", fake_get)
    md = MarketData(symbol="TEST.AX", index_symbol="^AXJO", currency="AUD")

    _risk_free(md, date(2024, 12, 31))

    assert captured["params"]["observation_end"] == "2024-12-31"
    assert md.risk_free == 0.0425
    assert md.sources["risk_free_as_of"] == "2024-12-30"


def test_eodhd_recent_price_fallback_preserves_raw_and_adjusted_close(monkeypatch):
    class Response:
        ok = True

        @staticmethod
        def json():
            return [{"date": "2026-07-17", "close": 7.68, "adjusted_close": 7.61}]

    monkeypatch.setattr(settings, "eodhd_api_key", "test-key")
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response())
    md = MarketData(symbol="STO.AX", index_symbol="^AXJO", currency="AUD")
    _eodhd_fallback(md, "ASX", "STO", date(2026, 7, 18))
    assert md.price == 7.68
    assert md.adjusted_close == 7.61
    assert md.sources["price"] == "EODHD"
