"""Date -> reporting period logic.

Given any valuation date, work out:
  * the calendar quarter the date falls in, and
  * the *previous 4 completed quarters* (the trailing-twelve-month window),
    returned in chronological order.

Example
-------
Valuation date 2025-05-20 falls in 2025 Q2. The four *completed* quarters
before it are 2024 Q2, Q3, Q4 and 2025 Q1  ->  quarter numbers [2, 3, 4, 1].

Valuation date 2026-01-15 falls in 2026 Q1. The four completed quarters are
2025 Q1, Q2, Q3, Q4  ->  quarter numbers [1, 2, 3, 4].
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Quarter:
    year: int
    quarter: int  # 1..4

    @property
    def label(self) -> str:
        return f"{self.year} Q{self.quarter}"

    @property
    def short(self) -> str:
        return f"Q{self.quarter} {self.year}"

    def prev(self) -> Quarter:
        if self.quarter == 1:
            return Quarter(self.year - 1, 4)
        return Quarter(self.year, self.quarter - 1)


def quarter_of(d: date) -> int:
    return (d.month - 1) // 3 + 1


@dataclass
class Period:
    valuation_date: date
    current: Quarter
    trailing: list[Quarter]  # 4 completed quarters, chronological (oldest -> newest)

    def to_dict(self) -> dict:
        return {
            "valuation_date": self.valuation_date.isoformat(),
            "year": self.current.year,
            "quarter": self.current.quarter,
            "current_label": self.current.label,
            "trailing": [
                {"year": q.year, "quarter": q.quarter, "label": q.label, "short": q.short}
                for q in self.trailing
            ],
            "trailing_quarter_order": [q.quarter for q in self.trailing],
        }


def resolve_period(d: date) -> Period:
    """Return the current quarter plus the previous 4 completed quarters."""
    cur = Quarter(d.year, quarter_of(d))
    # The most recent *completed* quarter is the one before the current quarter.
    last_completed = cur.prev()
    trailing: list[Quarter] = []
    q = last_completed
    for _ in range(4):
        trailing.append(q)
        q = q.prev()
    trailing.reverse()  # chronological
    return Period(valuation_date=d, current=cur, trailing=trailing)
