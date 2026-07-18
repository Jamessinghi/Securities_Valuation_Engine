import pytest
from fastapi import HTTPException

from app.main import BrowserExtractIn, ComputeIn, api_compute, api_extract_text, health


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


def test_browser_text_endpoint_extracts_without_uploading_pdf():
    result = api_extract_text(BrowserExtractIn(
        filename="annual-report.pdf",
        doc_type="annual_report",
        pages=[
            "Consolidated income statement\nRevenue 5,000 4,500\n"
            "Net profit after tax 800 700\nUS$ million",
            "Consolidated statement of financial position\n"
            "Total assets 12,000 11,000\nTotal liabilities 5,000 4,800\n"
            "Total equity 7,000 6,200",
        ],
    ))

    assert result["method"] == "browser-text"
    assert result["pages"] == 2
    assert result["fields"]["revenue"]["value"] == 5000
    assert any("original file was not uploaded" in warning for warning in result["warnings"])


def test_browser_text_endpoint_rejects_scanned_document_placeholder():
    with pytest.raises(HTTPException) as exc:
        api_extract_text(BrowserExtractIn(
            filename="scan.pdf", doc_type="annual_report", pages=["", "", "cover"],
        ))
    assert exc.value.status_code == 422
