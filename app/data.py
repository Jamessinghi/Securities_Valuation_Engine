"""Canonical, auditable inputs and accounting-quality checks."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any


@dataclass
class CanonicalInput:
    """One normalized observation used by the calculation engine."""

    value: Any
    currency: str | None
    units: str
    period_start: str | None
    period_end: str | None
    as_of_date: str | None
    source: str
    source_type: str
    confidence: str
    is_estimated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def confidence_rank(value: str | None) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(value or "", 0)


def canonicalize(
    value: Any,
    *,
    currency: str | None,
    units: str,
    source: str,
    source_type: str,
    confidence: str = "high",
    period_start: str | None = None,
    period_end: str | None = None,
    as_of_date: str | date | None = None,
    is_estimated: bool = False,
) -> dict:
    if isinstance(as_of_date, date):
        as_of_date = as_of_date.isoformat()
    return CanonicalInput(
        value=value, currency=currency, units=units,
        period_start=period_start, period_end=period_end,
        as_of_date=as_of_date, source=source, source_type=source_type,
        confidence=confidence, is_estimated=is_estimated,
    ).to_dict()


def reconciliation(name: str, lhs: float | None, rhs: float | None,
                   *, tolerance: float = 0.02, inputs: list[str] | None = None) -> dict:
    """Compare two accounting totals using a materiality-aware tolerance."""
    if lhs is None or rhs is None:
        return {"name": name, "status": "not_tested", "difference": None,
                "tolerance": tolerance, "inputs": inputs or []}
    difference = lhs - rhs
    base = max(abs(lhs), abs(rhs), 1.0)
    relative = abs(difference) / base
    return {"name": name, "status": "passed" if relative <= tolerance else "failed",
            "difference": difference, "relative_difference": relative,
            "tolerance": tolerance, "inputs": inputs or []}
