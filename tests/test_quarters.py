from datetime import date

from app.quarters import resolve_period


def test_mid_year_wraps_prior_year():
    p = resolve_period(date(2025, 5, 20))  # 2025 Q2
    assert p.current.quarter == 2
    assert [q.quarter for q in p.trailing] == [2, 3, 4, 1]
    assert p.trailing[0].year == 2024
    assert p.trailing[-1].year == 2025


def test_start_of_year_gives_full_prior_year():
    p = resolve_period(date(2026, 1, 15))  # 2026 Q1
    assert p.current.quarter == 1
    assert [q.quarter for q in p.trailing] == [1, 2, 3, 4]
    assert all(q.year == 2025 for q in p.trailing)


def test_q4_window():
    p = resolve_period(date(2025, 11, 1))  # 2025 Q4
    assert p.current.quarter == 4
    assert [q.quarter for q in p.trailing] == [4, 1, 2, 3]
    assert p.trailing[0].year == 2024
    assert p.trailing[-1].year == 2025
