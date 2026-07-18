from app.ocr.extractor import (
    _capex_from_investing,
    _detect_period_end,
    _pick_value,
    _score_candidate,
    _section_contexts,
)


def test_detects_statement_period_end():
    assert _detect_period_end(["For the year ended 31 December 2025"]) == "2025-12-31"
    assert _detect_period_end(["Six months ended June 30, 2025"]) == "2025-06-30"


def test_pick_skips_note_ref():
    assert _pick_value(" 2.5 25.2 37.8") == 25.2

def test_pick_skips_footnote_markers():
    assert _pick_value("1, 2 3,391 3,706 (9)") == 3391.0

def test_pick_parses_parenthesised_negative():
    assert _pick_value(" (137) (123) 11") == -137.0

def test_pick_ignores_dash_nil():
    assert _pick_value(" – 687") == 687.0


def test_section_contexts_tags_cashflow_subsections():
    lines = [
        "Statement of cash flows",
        "Cash flows from operating activities",
        "Receipts from customers 6,120",
        "Net cash from operating activities 3,105",
        "Cash flows from investing activities",
        "Payments for property, plant and equipment (1,842)",
        "Cash flows from financing activities",
        "Dividends paid (700)",
    ]
    ctx = _section_contexts(lines)
    assert ctx[3] == "cf_operating"
    assert ctx[5] == "cf_investing"
    assert ctx[7] == "cf_financing"


def test_capex_scored_higher_inside_investing_section():
    line = "Payments for property, plant and equipment (1,842) (1,650)"
    tail = " (1,842) (1,650)"
    in_section = _score_candidate(line, tail, False, -1842.0, field_key="capex", context="cf_investing")
    in_prose = _score_candidate(line, tail, False, -1842.0, field_key="capex", context=None)
    assert in_section > in_prose


def test_capex_summed_from_investing_outflows():
    lines = [
        "Cash flows from investing activities",
        "Payments for:",
        "Exploration and evaluation assets (121) (157)",
        "Oil and gas assets (1,918) (2,214)",
        "Other land, buildings, plant and equipment (17) (30)",
        "Proceeds from disposal of assets 2 6",   # inflow — must be excluded
        "Net cash used in investing activities (2,178) (2,685)",
    ]
    ctx = _section_contexts(lines)
    total, hits = _capex_from_investing(lines, ctx, scale=1.0)
    assert hits == 3
    assert total == 121.0 + 1918.0 + 17.0


def test_capex_ignores_lines_outside_investing_section():
    lines = ["Some prose mentioning oil and gas assets (999)"]
    ctx = _section_contexts(lines)
    total, hits = _capex_from_investing(lines, ctx, scale=1.0)
    assert total is None and hits == 0
