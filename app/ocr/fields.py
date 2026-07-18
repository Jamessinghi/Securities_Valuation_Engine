"""Canonical financial field schema, per-field label patterns, and document types.

Every value is expressed in the report's reporting currency (Santos reports in
US$ million) except per-share figures which are in cents as reported.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Field:
    key: str
    label: str            # human label shown in UI / manual entry
    patterns: tuple[str, ...]  # regex label alternatives (case-insensitive)
    unit: str = "USD_m"   # USD_m | cents | percent | count_m
    kind: str = "flow"    # flow | stock | pershare | rate


# ---- Canonical fields ------------------------------------------------------
FIELDS: list[Field] = [
    Field("revenue", "Product sales / Revenue",
          (r"product sales", r"revenue from contracts with customers[ \-–]+product sales",
           r"total revenue", r"^revenue")),
    Field("ebitdax", "EBITDA(X)", (r"ebitdax", r"ebitda")),
    Field("ebit", "EBIT", (r"^ebit\b", r"\bebit2?\b")),
    Field("dna", "Depreciation & depletion/amortisation",
          (r"depreciation and depletion", r"depreciation, depletion", r"depreciation and amortis")),
    Field("impairment", "Impairment", (r"impairment loss", r"impairment of non-current assets")),
    Field("net_profit", "Net profit for the period",
          (r"net profit/?\(?loss\)? for the period(?! attributable)", r"net profit for the period",
           r"profit for the period")),
    Field("underlying_profit", "Underlying profit", (r"underlying profit for the period",)),
    Field("income_tax", "Income tax expense", (r"income tax expense", r"income tax benefit")),
    Field("eps_basic", "Basic EPS (cents)",
          (r"^\s*basic (?:profit|earnings)(?:/?\(?loss\)?)? per share",), unit="cents", kind="pershare"),
    Field("eps_diluted", "Diluted EPS (cents)",
          (r"^\s*diluted (?:profit|earnings)(?:/?\(?loss\)?)? per share",), unit="cents", kind="pershare"),
    Field("dps", "Dividends per share (cents)",
          (r"^\s*(?:total )?dividends? per share", r"^\s*(?:final |interim )?dividend per share"),
          unit="cents", kind="pershare"),
    Field("wtd_avg_shares", "Weighted avg shares (m)", (r"weighted average number of (?:ordinary )?shares",),
          unit="count_m", kind="stock"),
    Field("shares_on_issue", "Shares on issue (m)",
          (r"(?:ordinary )?shares on issue", r"number of shares on issue", r"issued (?:ordinary )?shares"),
          unit="count_m", kind="stock"),
    Field("total_assets", "Total assets", (r"total assets",), kind="stock"),
    Field("total_current_assets", "Total current assets", (r"total current assets",), kind="stock"),
    Field("total_liabilities", "Total liabilities", (r"total liabilities",), kind="stock"),
    Field("total_current_liabilities", "Total current liabilities", (r"total current liabilities",), kind="stock"),
    Field("cash", "Cash & cash equivalents",
          (r"cash and cash equivalents at the end", r"cash and cash equivalents"), kind="stock"),
    Field("borrowings_current", "Interest-bearing borrowings (current)",
          (r"interest-bearing loans and borrowings",), kind="stock"),
    Field("total_equity", "Total equity / net assets",
          (r"total equity", r"net assets/?equity", r"^net assets"), kind="stock"),
    Field("op_cash_flow", "Net cash from operating activities",
          (r"net cash (?:flows? )?from operating activities", r"net cash provided by operating"), kind="flow"),
    Field("capex", "Capital expenditure / payments for PP&E",
          (r"capital expenditure", r"payments for (?:property, plant and equipment|oil and gas assets|exploration)"),
          kind="flow"),
    Field("dividends_paid", "Dividends paid", (r"dividends paid",), kind="flow"),
    Field("free_cash_flow", "Free cash flow", (r"free cash flow",), kind="flow"),
    Field("discount_rate", "Discount rate / WACC (%)",
          (r"pre-tax discount rate", r"post-tax discount rate", r"weighted average cost of capital",
           r"discount rate"), unit="percent", kind="rate"),
    Field("grant_volatility", "Expected volatility (%)", (r"expected volatility",), unit="percent", kind="rate"),
    Field("risk_free_disclosed", "Risk-free rate disclosed (%)", (r"risk-free interest rate", r"risk-free rate"),
          unit="percent", kind="rate"),
]

FIELD_BY_KEY = {f.key: f for f in FIELDS}


# ---- Document types --------------------------------------------------------
@dataclass(frozen=True)
class DocType:
    key: str
    label: str
    # fields we *require* this document to yield for a clean extraction
    required: tuple[str, ...]
    # filename hints for auto-detection
    hints: tuple[str, ...] = field(default_factory=tuple)


DOC_TYPES: list[DocType] = [
    DocType("annual_report", "Annual Report / Appendix 4E",
            required=("revenue", "ebit", "net_profit", "eps_basic", "total_assets",
                      "total_liabilities", "total_equity", "op_cash_flow", "cash", "discount_rate"),
            hints=("annual", "4e", "full-year", "full year")),
    DocType("half_year", "Half-Year Report / Appendix 4D",
            required=("revenue", "net_profit", "eps_basic", "total_equity"),
            hints=("half-year", "half year", "hy", "4d", "interim")),
    DocType("results_presentation", "Full-Year Results Presentation",
            required=("revenue", "free_cash_flow"),
            hints=("results_presentation", "results-presentation", "results presentation", "presentation")),
    DocType("quarterly", "Quarterly Activities & Cashflow Report",
            required=("revenue",),
            hints=("quarter", "q1", "q2", "q3", "q4", "first-quarter", "second-quarter",
                   "third-quarter", "fourth-quarter")),
    DocType("agm", "AGM Results", required=(), hints=("agm", "annual-general-meeting", "general meeting")),
    DocType("other", "Supporting Document", required=(), hints=()),
]

DOC_TYPE_BY_KEY = {d.key: d for d in DOC_TYPES}


def detect_doc_type(filename: str) -> str:
    name = filename.lower()
    # Order matters: check most-specific hints first.
    for dt in DOC_TYPES:
        for h in dt.hints:
            if h in name:
                return dt.key
    return "other"
