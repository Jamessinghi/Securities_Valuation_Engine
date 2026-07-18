"""Free official/reference-source clients not covered by quote APIs.

These adapters return dated canonical observations and cache provider payloads.
They deliberately return no value rather than backfilling current information
into a historical valuation.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from ..data import canonicalize

_CACHE = Path(__file__).resolve().parents[2] / ".cache" / "public"
_CACHE.mkdir(parents=True, exist_ok=True)


def sec_company_facts(cik: str, as_of: date) -> dict:
    """Fetch official SEC XBRL company facts, bounded by filing date."""
    cik10 = str(cik).lstrip("0").zfill(10)
    payload = _cached_json(
        f"sec_{cik10}.json", f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json",
        headers={"User-Agent": "SecuritiesValuationEngine educational-contact@example.com"})
    if not payload:
        return {}
    facts = {}
    for taxonomy in ("us-gaap", "ifrs-full"):
        for concept, item in payload.get("facts", {}).get(taxonomy, {}).items():
            observations = []
            for units in item.get("units", {}).values():
                observations.extend(units)
            eligible = [row for row in observations if row.get("filed", "9999-12-31") <= as_of.isoformat()]
            if eligible:
                facts[concept] = max(eligible, key=lambda row: (row.get("filed", ""), row.get("end", "")))
    return facts


def sec_filing_search(query: str, as_of: date) -> list[dict]:
    """Search SEC full text for merger/proxy source documents."""
    response = requests.get(
        "https://efts.sec.gov/LATEST/search-index",
        params={"q": query, "forms": "DEFM14A,S-4", "enddt": as_of.isoformat()},
        headers={"User-Agent": "SecuritiesValuationEngine educational-contact@example.com"}, timeout=20)
    if not response.ok:
        return []
    return response.json().get("hits", {}).get("hits", [])


def asx_announcement_search_url(ticker: str) -> str:
    """Official ASX announcements page used for scheme/IER document research."""
    return f"https://www.asx.com.au/markets/company/{ticker.upper()}"


def aqr_dataset_catalog() -> dict:
    """Auditable descriptor for AQR's free factor-data backup."""
    return canonicalize(
        "https://www.aqr.com/Insights/Datasets", currency=None, units="dataset_catalog",
        source="AQR Datasets", source_type="reference_dataset", confidence="high",
        as_of_date=date.today())


def _cached_json(filename: str, url: str, *, headers: dict | None = None) -> dict:
    path = _CACHE / filename
    if path.exists() and datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date() == date.today():
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        path.write_text(json.dumps(payload))
        return payload
    except (requests.RequestException, ValueError, OSError):
        return {}
