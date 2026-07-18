"""Canonical financial field schema, per-field label patterns, and document types.

Each canonical field carries several regex label alternatives so the extractor
generalises across reporting standards — Australian (ASX / IFRS) statements
(e.g. "Product sales", "Net profit for the period", "Interest-bearing loans")
*and* US-GAAP filings (e.g. "Net sales", "Net income", "Long-term debt",
"Total stockholders' equity"). Values are stored in the report's reporting
currency, in millions (the extractor normalises any "thousands"/"billions"
scale), except per-share figures which stay in the reported per-share unit.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Field:
    """One canonical financial line item and how to find it in raw text.

    Attributes
    ----------
    key      : stable identifier used throughout the engine.
    label    : human-readable name shown in the UI / manual-entry form.
    patterns : ordered regex alternatives (matched case-insensitively) that a
               line's label text must contain for the line to be a candidate.
    unit     : ``USD_m`` (monetary, millions of reporting currency),
               ``cents`` (per-share as reported), ``percent`` (a rate), or
               ``count_m`` (a share count).
    kind     : ``flow`` | ``stock`` | ``pershare`` | ``rate`` — controls scale
               normalisation (only monetary flow/stock values are rescaled).
    """

    key: str
    label: str
    patterns: tuple[str, ...]
    unit: str = "USD_m"
    kind: str = "flow"

    @property
    def is_monetary(self) -> bool:
        """True when the value is a currency amount subject to scale/FX."""
        return self.unit == "USD_m"


# ---- Canonical fields ------------------------------------------------------
# Patterns list AU/IFRS wording first, then US-GAAP synonyms, so the extractor
# works on Santos-style *and* 10-K-style statements.
FIELDS: list[Field] = [
    Field("revenue", "Revenue / Net sales",
          (r"product sales", r"revenue from contracts with customers[ \-–]+product sales",
           r"total revenues?", r"net sales", r"net revenues?", r"total net sales",
           r"sales revenue", r"^revenue\b")),
    Field("ebitdax", "EBITDA(X)", (r"ebitdax", r"ebitda")),
    Field("ebit", "EBIT / Operating income",
          (r"^ebit\b", r"\bebit2?\b", r"operating income", r"operating profit",
           r"income from operations")),
    Field("dna", "Depreciation & depletion/amortisation",
          (r"depreciation and depletion", r"depreciation, depletion", r"depreciation and amortis",
           r"depreciation, depletion and amortis", r"depreciation & amortis")),
    Field("impairment", "Impairment", (r"impairment loss", r"impairment of non-current assets",
                                       r"impairment charge", r"asset impairment")),
    Field("net_profit", "Net profit / Net income",
          (r"net profit/?\(?loss\)? for the period(?! attributable)", r"net profit for the period",
           r"profit for the period", r"net income(?! per)(?! \()", r"net earnings",
           r"net income attributable")),
    Field("underlying_profit", "Underlying / adjusted profit",
          (r"underlying profit for the period", r"underlying profit", r"adjusted net income")),
    Field("income_tax", "Income tax expense",
          (r"income tax expense", r"income tax benefit", r"provision for income taxes",
           r"income tax (?:expense|provision)")),
    Field("interest_expense", "Finance costs / interest expense",
          (r"finance costs?", r"interest expense", r"net finance costs?", r"borrowing costs?")),
    Field("profit_attributable", "Profit attributable to ordinary shareholders",
          (r"profit attributable to (?:owners|ordinary shareholders|members)",
           r"net income attributable to (?:common shareholders|the parent)")),
    Field("eps_basic", "Basic EPS (per share)",
          (r"^\s*basic (?:profit|earnings|net income|net loss)(?:/?\(?loss\)?)? per share",
           r"^\s*basic (?:earnings|income) per (?:common )?share", r"^\s*basic per share"),
          unit="cents", kind="pershare"),
    Field("eps_diluted", "Diluted EPS (per share)",
          (r"^\s*diluted (?:profit|earnings|net income|net loss)(?:/?\(?loss\)?)? per share",
           r"^\s*diluted (?:earnings|income) per (?:common )?share"),
          unit="cents", kind="pershare"),
    Field("dps", "Dividends per share",
          (r"^\s*(?:total )?dividends? per share", r"^\s*(?:final |interim )?dividend per share",
           r"^\s*(?:cash )?dividends? declared per (?:common )?share"),
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
          (r"cash and cash equivalents at the end", r"cash and cash equivalents",
           r"cash and short-term investments"), kind="stock"),
    Field("cash_opening", "Cash at beginning of period",
          (r"cash and cash equivalents at (?:the )?beginning", r"cash at beginning of (?:the )?period"), kind="stock"),
    Field("cash_movement", "Net increase/decrease in cash",
          (r"net increase/?\(?decrease\)? in cash", r"net change in cash", r"increase in cash and cash equivalents")),
    Field("borrowings_total", "Total interest-bearing debt",
          (r"total interest-bearing (?:loans and )?borrowings", r"total debt", r"total borrowings"), kind="stock"),
    Field("borrowings_current", "Current interest-bearing debt",
          (r"current (?:interest-bearing )?(?:loans and )?borrowings", r"current debt"), kind="stock"),
    Field("borrowings_noncurrent", "Non-current interest-bearing debt",
          (r"non-current (?:interest-bearing )?(?:loans and )?borrowings", r"non-current debt"), kind="stock"),
    Field("total_equity", "Total equity / net assets",
          (r"total equity", r"net assets/?equity", r"^net assets",
           r"total (?:stockholders|shareholders)'? equity", r"total equity attributable"), kind="stock"),
    Field("op_cash_flow", "Net cash from operating activities",
          (r"net cash (?:flows? )?from operating activities", r"net cash provided by operating",
           r"cash (?:flows? )?(?:provided by|from) operations",
           r"net cash (?:provided by|used in) operating"), kind="flow"),
    Field("capex", "Capital expenditure / PP&E purchases",
          (r"capital expenditure", r"payments for (?:property, plant and equipment|oil and gas assets|exploration)",
           r"purchases? of property,? plant and equipment", r"purchases? of property and equipment",
           r"acquisitions? of property,? plant and equipment", r"additions to (?:property|oil and gas|exploration)",
           r"payments for (?:exploration|evaluation|development|mine properties)",
           r"expenditure on (?:property|exploration|development)", r"capital expenditures?"), kind="flow"),
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
