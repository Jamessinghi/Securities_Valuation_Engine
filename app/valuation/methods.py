"""The 65 valuation methods.

Each method is a function ``fn(ctx) -> MethodResult``. A result is one of:
  * ok      — computed from available data
  * partial — computed but leans on an assumption or a proxy input
  * na      — needs data no uploaded document / free feed provides

Methods that yield a per-share fair value set ``intrinsic_ps`` so the engine can
triangulate a single intrinsic value. All monetary inputs reach a method already
converted to the target currency (AUD by default) and expressed in millions
unless noted; per-share values are in target currency per share.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class MethodResult:
    id: int
    section: str
    name: str
    status: str = "na"                 # ok | partial | na
    value: float | None = None
    unit: str = ""
    note: str = ""
    missing: list[str] = field(default_factory=list)
    intrinsic_ps: float | None = None
    completion: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# --------------------------------------------------------------------------- #
#  Context                                                                     #
# --------------------------------------------------------------------------- #
def _g(d: dict, k: str) -> float | None:
    """Read a numeric field value from a merged fundamentals dict (or None)."""
    v = d.get(k)
    if isinstance(v, dict):
        v = v.get("value")
    return float(v) if isinstance(v, (int, float)) else None


def _cvt(v: float | None, fx: float) -> float | None:
    """Apply an FX multiplier to a monetary value, preserving None."""
    return None if v is None else v * fx


class Ctx:
    """Normalised inputs derived from OCR fundamentals + market snapshot.

    All monetary inputs are converted into the ``currency`` (default AUD) using
    ``fx`` (units of target per unit of the report's reporting currency) so that
    every downstream figure — intrinsic value, multiples, the price comparison —
    is expressed in one currency. Rates, ratios and share counts are never
    scaled by FX.
    """

    def __init__(self, fundamentals: dict, market: dict | None, currency: str = "AUD",
                 reporting_currency: str | None = None, fx: float = 1.0, fx_live: bool = True,
                 g_high: float = 0.05, g_term: float = 0.025, horizon: int = 5):
        f = fundamentals or {}
        m = market or {}
        self.canonical_fundamentals = f
        self.canonical_market = m.get("canonical_inputs") or {}
        self.currency = currency                       # target/display currency
        self.reporting_currency = reporting_currency or currency
        self.fx = fx if fx else 1.0                    # reporting_currency -> currency
        self.g_high = g_high
        self.g_term = g_term
        self.horizon = horizon
        self.assumptions: list[str] = []
        if self.reporting_currency != self.currency and self.fx != 1.0:
            self.assumptions.append(
                f"converted {self.reporting_currency}->{self.currency} at {self.fx:.4f}"
                + ("" if fx_live else " (fallback rate)"))

        fxm = self.fx  # shorthand for monetary conversion

        # --- fundamentals, converted to millions of target currency ---
        self.revenue = _cvt(_g(f, "revenue"), fxm)
        self.ebit = _cvt(_g(f, "ebit"), fxm)
        self.ebitda = _cvt(_g(f, "ebitdax"), fxm)
        _dna = _g(f, "dna")
        self.dna = _cvt(abs(_dna), fxm) if _dna is not None else None
        self.net_income = _cvt(_g(f, "net_profit"), fxm)
        self.income_tax = _cvt(_g(f, "income_tax"), fxm)
        self.total_assets = _cvt(_g(f, "total_assets"), fxm)
        self.total_current_assets = _cvt(_g(f, "total_current_assets"), fxm)
        self.total_liabilities = _cvt(_g(f, "total_liabilities"), fxm)
        self.total_current_liabilities = _cvt(_g(f, "total_current_liabilities"), fxm)
        self.cash = _cvt(_g(f, "cash"), fxm)
        debt = _g(f, "borrowings_total")
        if debt is None:
            current_debt, noncurrent_debt = _g(f, "borrowings_current"), _g(f, "borrowings_noncurrent")
            if current_debt is not None or noncurrent_debt is not None:
                debt = (current_debt or 0) + (noncurrent_debt or 0)
        self.total_debt = _cvt(debt, fxm)
        _interest = _g(f, "interest_expense")
        self.interest_expense = _cvt(abs(_interest), fxm) if _interest is not None else None
        self.book_equity = _cvt(_g(f, "total_equity"), fxm)
        self.op_cf = _cvt(_g(f, "op_cash_flow"), fxm)
        _div = _g(f, "dividends_paid")
        self.dividends_paid = _cvt(abs(_div), fxm) if _div is not None else None
        # per-share figures: reported in cents -> currency/share, then FX.
        self.eps = _cvt(_g(f, "eps_basic") / 100.0, fxm) if _g(f, "eps_basic") is not None else None
        self.dps = _cvt(_g(f, "dps") / 100.0, fxm) if _g(f, "dps") is not None else None
        # rates are unit-less — never FX-scaled.
        self.disc_disclosed = (_g(f, "discount_rate") / 100.0) if _g(f, "discount_rate") is not None else None
        self.grant_vol = (_g(f, "grant_volatility") / 100.0) if _g(f, "grant_volatility") is not None else None

        # CapEx is a cash outflow — often printed in parentheses, so it parses
        # negative. Store it as a positive magnitude (so "+ D&A − CapEx" is
        # correct), and only trust a figure that is plausible relative to
        # operating cash flow / revenue, so a stray line can't distort the DCF.
        _capex = _g(f, "capex")
        self.capex = None
        if _capex is not None:
            mag = abs(_capex)
            _ocf, _rev = _g(f, "op_cash_flow"), _g(f, "revenue")
            ceiling = abs(_ocf) * 3 if _ocf else (abs(_rev) if _rev else None)
            if mag > 1 and (ceiling is None or mag <= ceiling):
                self.capex = _cvt(mag, fxm)

        # Directly-reported free cash flow (results presentations often state it).
        _fcf = _g(f, "free_cash_flow")
        self.reported_fcf = _cvt(_fcf, fxm) if _fcf is not None else None

        # --- shares ---
        filing_shares = _g(f, "wtd_avg_shares")
        # Canonical filing share counts are expressed in millions; market feeds
        # report absolute shares. Normalize both to absolute shares.
        self.shares = filing_shares * 1_000_000 if filing_shares is not None else m.get("shares_outstanding")

        # --- market ---  (price already in the traded/target currency)
        self.price = m.get("price")
        # market cap expressed in MILLIONS to match the fundamentals' scale.
        mc = m.get("market_cap")
        if not mc and self.price and self.shares:
            mc = self.price * self.shares
        self.market_cap = (mc / 1_000_000) if mc else None
        self.beta = m.get("beta")
        self.hist_vol = m.get("hist_vol")
        self.risk_free = m.get("risk_free")
        self.erp = m.get("erp")
        self.target_mean = m.get("target_mean")
        self.forward_eps = m.get("forward_eps")
        self.buyback_yield = m.get("buyback_yield")
        self.technical = m.get("technical")
        self.factor_models = m.get("factor_models") or {}
        self.ff_factors = m.get("ff_factors")
        self.macro_model = m.get("macro_model")
        self.market_sources = m.get("sources") or {}
        self.market_ccy = m.get("currency", currency)

        # --- derived ---
        self.pretax = None
        self.tax_rate = None
        if self.net_income is not None and self.income_tax is not None:
            self.pretax = self.net_income - self.income_tax  # income_tax stored negative
            if self.pretax:
                self.tax_rate = abs(self.income_tax) / self.pretax
        if self.tax_rate is None:
            self.tax_rate = 0.30
            self.assumptions.append("tax rate assumed 30%")

        self.net_debt = None
        if self.total_debt is not None:
            self.net_debt = self.total_debt - (self.cash or 0)

        # CAPM cost of equity and market-value WACC. Disclosed asset/impairment
        # discount rates are retained only as a cross-check, never as WACC.
        self.cost_equity = None
        if self.beta is not None and self.risk_free is not None and self.erp is not None:
            self.cost_equity = self.risk_free + self.beta * self.erp
        if self.cost_equity is None and self.disc_disclosed is not None:
            self.cost_equity = self.disc_disclosed
            self.assumptions.append("cost of equity proxied by disclosed discount rate")
        self.cost_debt = None
        if self.interest_expense is not None and self.total_debt and self.total_debt > 0:
            self.cost_debt = min(max(self.interest_expense / self.total_debt, 0.0), 0.30)
        self.wacc = None
        self.wacc_source = None
        if self.cost_equity is not None and self.market_cap and self.total_debt is not None:
            total_capital = self.market_cap + self.total_debt
            debt_cost = self.cost_debt if self.cost_debt is not None else self.risk_free
            if total_capital > 0 and debt_cost is not None:
                self.wacc = (
                    self.market_cap / total_capital * self.cost_equity
                    + self.total_debt / total_capital * debt_cost * (1 - self.tax_rate)
                )
                self.wacc_source = "calculated market-value WACC"
                self.assumptions.append("WACC calculated from market-value capital weights")
        if self.wacc is None and self.cost_equity is not None and not self.total_debt:
            self.wacc = self.cost_equity
            self.wacc_source = "all-equity CAPM"
            self.assumptions.append("WACC equals cost of equity because debt was unavailable or zero")
        # Explicit row-completion entries are already normalized to decimals by
        # the API and intentionally override derived/feed values.
        if m.get("cost_equity") is not None:
            self.cost_equity = float(m["cost_equity"])
        if m.get("wacc") is not None:
            self.wacc = float(m["wacc"])
            self.wacc_source = "manual override"

    # helpers
    def per_share(self, value_m: float | None) -> float | None:
        if value_m is None or not self.shares:
            return None
        return value_m * 1_000_000 / self.shares

    def input_status(self, fundamental_keys: tuple[str, ...] = (), market_keys: tuple[str, ...] = ()) -> str:
        """OK only when all material inputs are reliable and non-estimated."""
        cells = [self.canonical_fundamentals.get(key) for key in fundamental_keys]
        cells += [self.canonical_market.get(key) for key in market_keys]
        cells = [cell for cell in cells if cell]
        if not cells:
            return "partial"
        return "ok" if all(
            cell.get("confidence") == "high" and not cell.get("is_estimated", False) for cell in cells
        ) else "partial"

    def free_cash_flow(self) -> float | None:
        """Best available free cash flow, in target-currency millions.

        Priority favours rigour: (1) operating cash flow − CapEx, both taken
        straight from the cash-flow statement; (2) operating cash flow − D&A as
        a last-resort proxy when CapEx couldn't be extracted; (3) the company's
        reported (often non-IFRS) FCF only when operating cash flow is missing.
        Returns None when nothing is available.
        """
        if self.op_cf is not None:
            capex = self.capex if self.capex is not None else (self.dna or 0)
            return self.op_cf - capex
        return self.reported_fcf

    def fcf_note(self) -> str:
        """Describe how ``free_cash_flow()`` was derived (for method notes)."""
        if self.op_cf is None:
            return "company-reported free cash flow"
        base = ("operating cash flow − CapEx" if self.capex is not None
                else "operating cash flow − D&A (CapEx not extracted; proxy)")
        if self.reported_fcf is not None:
            base += f"; cf. company-reported FCF {self.reported_fcf:,.0f}"
        return base

    def fx_note(self) -> str:
        """Trailing note flagging any residual currency mismatch.

        Everything is converted to ``self.currency`` up front, so this only
        fires in the unusual case where the market price is quoted in a
        different currency than the target (kept as a safety flag).
        """
        if self.price and self.market_ccy and self.market_ccy != self.currency:
            return f" (NB: price quoted in {self.market_ccy}, values in {self.currency})"
        return ""


def _two_stage_pv(cf0: float, g_high: float, g_term: float, rate: float, n: int) -> float | None:
    if rate is None or rate <= g_term:
        return None
    pv = 0.0
    cf = cf0
    for t in range(1, n + 1):
        cf = cf * (1 + g_high)
        pv += cf / (1 + rate) ** t
    tv = cf * (1 + g_term) / (rate - g_term)
    pv += tv / (1 + rate) ** n
    return pv


# --------------------------------------------------------------------------- #
#  Result builders                                                             #
# --------------------------------------------------------------------------- #
def _mk(spec, **kw) -> MethodResult:
    return MethodResult(id=spec["id"], section=spec["section"], name=spec["name"], **kw)


# Each compute fn receives (ctx, spec) and returns a MethodResult.
def _fcff(ctx: Ctx, spec):
    if ctx.ebit is None or ctx.dna is None:
        return _mk(spec, status="na", missing=["EBIT", "D&A"], note="Needs EBIT and D&A.")
    capex = ctx.capex if ctx.capex is not None else ctx.dna
    note = ctx.fx_note()
    if ctx.capex is None:
        note = "CapEx not reliably extracted; assumed ≈ D&A (steady-state)." + note
    fcff0 = ctx.ebit * (1 - ctx.tax_rate) + ctx.dna - capex
    ev = _two_stage_pv(fcff0, ctx.g_high, ctx.g_term, ctx.wacc, ctx.horizon)
    if ev is None:
        return _mk(spec, status="na", missing=["WACC"], note="Needs a valid WACC > terminal growth.")
    equity = ev - (ctx.net_debt or 0)
    ps = ctx.per_share(equity)
    status = ctx.input_status(("ebit", "dna", "capex", "borrowings_total"),
                              ("beta", "risk_free", "erp", "price"))
    return _mk(spec, status=status, value=equity, unit=f"{ctx.currency}m EV-based equity",
               intrinsic_ps=ps, note=f"2-stage; g={ctx.g_high:.0%}->{ctx.g_term:.1%}, WACC={ctx.wacc:.1%}. " + note)


def _fcfe(ctx: Ctx, spec):
    if ctx.op_cf is None:
        return _mk(spec, status="na", missing=["operating cash flow"])
    ke = ctx.cost_equity
    if ke is None:
        return _mk(spec, status="na", missing=["cost of equity (beta/ERP or disclosed rate)"])
    fcfe0 = ctx.free_cash_flow()
    val = _two_stage_pv(fcfe0, ctx.g_high, ctx.g_term, ke, ctx.horizon)
    if val is None:
        return _mk(spec, status="na", note="Cost of equity must exceed terminal growth.")
    status = ctx.input_status(("op_cash_flow", "capex"), ("beta", "risk_free", "erp"))
    return _mk(spec, status=status, value=val, unit=f"{ctx.currency}m equity",
               intrinsic_ps=ctx.per_share(val),
               note=f"FCFE 2-stage, ke={ke:.1%}; {ctx.fcf_note()}." + ctx.fx_note())


def _ddm_gordon(ctx: Ctx, spec):
    if ctx.dps is None:
        return _mk(spec, status="na", missing=["dividends per share"])
    ke = ctx.cost_equity
    if ke is None or ke <= ctx.g_term:
        return _mk(spec, status="na", missing=["cost of equity > g"])
    v = ctx.dps * (1 + ctx.g_term) / (ke - ctx.g_term)
    return _mk(spec, status="partial", value=v, unit=f"{ctx.currency}/share", intrinsic_ps=v,
               note=f"D1/(ke-g); ke={ke:.1%}, g={ctx.g_term:.1%}." + ctx.fx_note())


def _ddm_multi(ctx: Ctx, spec):
    if ctx.dps is None:
        return _mk(spec, status="na", missing=["dividends per share"])
    ke = ctx.cost_equity
    if ke is None or ke <= ctx.g_term:
        return _mk(spec, status="na", missing=["cost of equity > g"])
    v = _two_stage_pv(ctx.dps, ctx.g_high, ctx.g_term, ke, ctx.horizon)
    return _mk(spec, status="partial", value=v, unit=f"{ctx.currency}/share", intrinsic_ps=v,
               note=f"Two-stage dividends, ke={ke:.1%}." + ctx.fx_note())


def _h_model(ctx: Ctx, spec):
    if ctx.dps is None:
        return _mk(spec, status="na", missing=["dividends per share"])
    ke = ctx.cost_equity
    if ke is None or ke <= ctx.g_term:
        return _mk(spec, status="na", missing=["cost of equity > g"])
    H = ctx.horizon / 2.0
    v = ctx.dps * ((1 + ctx.g_term) + H * (ctx.g_high - ctx.g_term)) / (ke - ctx.g_term)
    return _mk(spec, status="partial", value=v, unit=f"{ctx.currency}/share", intrinsic_ps=v,
               note=f"H-model, H={H}, ke={ke:.1%}." + ctx.fx_note())


def _residual_income(ctx: Ctx, spec):
    if ctx.book_equity is None or ctx.net_income is None:
        return _mk(spec, status="na", missing=["book equity", "net income"])
    ke = ctx.cost_equity
    if ke is None:
        return _mk(spec, status="na", missing=["cost of equity"])
    ri = ctx.net_income - ke * ctx.book_equity
    if ke <= ctx.g_term:
        return _mk(spec, status="na", note="ke must exceed g.")
    val = ctx.book_equity + ri * (1 + ctx.g_term) / (ke - ctx.g_term)
    return _mk(spec, status="partial", value=val, unit=f"{ctx.currency}m equity",
               intrinsic_ps=ctx.per_share(val), note=f"B0 + PV(residual income), ke={ke:.1%}." + ctx.fx_note())


def _owner_earnings(ctx: Ctx, spec):
    if ctx.net_income is None or ctx.dna is None:
        return _mk(spec, status="na", missing=["net income", "D&A"])
    capex = ctx.capex if ctx.capex is not None else ctx.dna
    oe = ctx.net_income + ctx.dna - capex
    rate = ctx.wacc or ctx.cost_equity
    if not rate or rate <= ctx.g_term:
        return _mk(spec, status="na", missing=["discount rate"])
    val = oe * (1 + ctx.g_term) / (rate - ctx.g_term)
    return _mk(spec, status="partial", value=val, unit=f"{ctx.currency}m",
               intrinsic_ps=ctx.per_share(val), note="Buffett owner earnings capitalised." + ctx.fx_note())


def _cap_earnings(ctx: Ctx, spec):
    if ctx.net_income is None:
        return _mk(spec, status="na", missing=["net income"])
    rate = ctx.cost_equity or ctx.wacc
    if not rate:
        return _mk(spec, status="na", missing=["capitalization rate"])
    val = ctx.net_income / rate
    return _mk(spec, status="partial", value=val, unit=f"{ctx.currency}m",
               intrinsic_ps=ctx.per_share(val), note=f"Earnings / {rate:.1%} cap rate." + ctx.fx_note())


def _book_value(ctx: Ctx, spec):
    if ctx.book_equity is None:
        return _mk(spec, status="na", missing=["total equity"])
    ps = ctx.per_share(ctx.book_equity)
    return _mk(spec, status="ok", value=ctx.book_equity, unit=f"{ctx.currency}m", intrinsic_ps=ps,
               note="Book value of equity.")


def _ncav(ctx: Ctx, spec):
    if ctx.total_current_assets is None or ctx.total_liabilities is None:
        return _mk(spec, status="na", missing=["current assets", "total liabilities"])
    v = ctx.total_current_assets - ctx.total_liabilities
    return _mk(spec, status="ok", value=v, unit=f"{ctx.currency}m", intrinsic_ps=ctx.per_share(v),
               note="Current assets − total liabilities (Graham net-net).")


def _liquidation(ctx: Ctx, spec):
    if ctx.total_assets is None or ctx.total_liabilities is None:
        return _mk(spec, status="na", missing=["total assets", "total liabilities"])
    # crude orderly recovery haircut applied to non-cash assets
    v = (ctx.cash or 0) + 0.7 * (ctx.total_assets - (ctx.cash or 0)) - ctx.total_liabilities
    return _mk(spec, status="partial", value=v, unit=f"{ctx.currency}m", intrinsic_ps=ctx.per_share(v),
               note="Assumes 70% recovery on non-cash assets — replace with asset-level haircuts.")


def _replacement(ctx: Ctx, spec):
    return _mk(spec, status="na", missing=["current asset replacement cost"],
              note="Book cost only in filings; true replacement cost needs an independent valuation.")


def _epv(ctx: Ctx, spec):
    if ctx.ebit is None:
        return _mk(spec, status="na", missing=["EBIT"])
    if not ctx.wacc:
        return _mk(spec, status="na", missing=["WACC"])
    ev = ctx.ebit * (1 - ctx.tax_rate) / ctx.wacc
    equity = ev - (ctx.net_debt or 0)
    return _mk(spec, status="partial", value=equity, unit=f"{ctx.currency}m", intrinsic_ps=ctx.per_share(equity),
               note=f"Greenwald EPV = NOPAT/WACC ({ctx.wacc:.1%}), no growth." + ctx.fx_note())


def _tobin_q(ctx: Ctx, spec):
    if ctx.market_cap is None or ctx.total_debt is None or ctx.total_assets is None:
        return _mk(spec, status="na", missing=["market cap", "total assets (replacement proxy)"])
    q = (ctx.market_cap + ctx.total_debt) / ctx.total_assets
    return _mk(spec, status="partial", value=q, unit="ratio",
               note="Uses book total assets as a replacement-cost proxy.")


def _ratio(ctx, num, den):
    if num is None or den in (None, 0):
        return None
    return num / den


def _pe(ctx: Ctx, spec):
    if ctx.price is None or ctx.eps in (None, 0):
        return _mk(spec, status="na", missing=["price", "EPS"])
    pe = ctx.price / ctx.eps
    return _mk(spec, status="partial", value=pe, unit="x",
               note="Own trailing P/E; peer set needed to value." + ctx.fx_note())


def _peg(ctx: Ctx, spec):
    if ctx.price is None or ctx.eps in (None, 0):
        return _mk(spec, status="na", missing=["price", "EPS", "growth"])
    pe = ctx.price / ctx.eps
    g = ctx.g_high * 100
    return _mk(spec, status="partial", value=pe / g, unit="ratio",
               note=f"PEG with assumed {g:.0f}% growth; peer PEG needed.")


def _pb(ctx: Ctx, spec):
    bvps = ctx.per_share(ctx.book_equity)
    r = _ratio(ctx, ctx.price, bvps)
    if r is None:
        return _mk(spec, status="na", missing=["price", "book value/share"])
    return _mk(spec, status="partial", value=r, unit="x", note="Own P/B; peer set needed." + ctx.fx_note())


def _ps(ctx: Ctx, spec):
    sps = ctx.per_share(ctx.revenue)
    r = _ratio(ctx, ctx.price, sps)
    if r is None:
        return _mk(spec, status="na", missing=["price", "sales/share"])
    return _mk(spec, status="partial", value=r, unit="x", note="Own P/S; peer set needed." + ctx.fx_note())


def _ev_ebitda(ctx: Ctx, spec):
    if ctx.market_cap is None or ctx.net_debt is None or ctx.ebitda in (None, 0):
        return _mk(spec, status="na", missing=["market cap", "net debt", "EBITDA"])
    ev = ctx.market_cap + ctx.net_debt
    return _mk(spec, status="partial", value=ev / ctx.ebitda, unit="x", note="Own EV/EBITDA; peers needed.")


def _ev_multi(ctx: Ctx, spec):
    if ctx.market_cap is None or ctx.net_debt is None:
        return _mk(spec, status="na", missing=["market cap", "net debt"])
    ev = ctx.market_cap + ctx.net_debt
    parts = {}
    if ctx.revenue:
        parts["EV/Sales"] = round(ev / ctx.revenue, 2)
    if ctx.ebit:
        parts["EV/EBIT"] = round(ev / ctx.ebit, 2)
    if not parts:
        return _mk(spec, status="na", missing=["sales/EBIT"])
    return _mk(spec, status="partial", value=list(parts.values())[0], unit="x",
               note="; ".join(f"{k}={v}x" for k, v in parts.items()) + " (peers needed).")


def _pcf(ctx: Ctx, spec):
    cfps = ctx.per_share(ctx.op_cf)
    r = _ratio(ctx, ctx.price, cfps)
    if r is None:
        return _mk(spec, status="na", missing=["price", "cash flow/share"])
    return _mk(spec, status="partial", value=r, unit="x", note="Own P/CF; peers needed." + ctx.fx_note())


def _div_yield(ctx: Ctx, spec):
    if ctx.price in (None, 0) or ctx.dps is None:
        return _mk(spec, status="na", missing=["price", "DPS"])
    y = ctx.dps / ctx.price
    return _mk(spec, status="partial", value=y, unit="yield",
               note="Own dividend yield; sector comparison needed." + ctx.fx_note())


def _fcf_yield(ctx: Ctx, spec):
    fcf = ctx.free_cash_flow()
    if fcf is None or not ctx.market_cap:
        return _mk(spec, status="na", missing=["FCF", "market cap"])
    return _mk(spec, status="ok", value=fcf / ctx.market_cap, unit="yield",
               note=f"{ctx.fcf_note()} / market cap.")


def _precedent(ctx, spec):
    return _mk(spec, status="na", missing=["M&A deal comps"],
              note="Precedent transactions have no free API — feed Scheme Booklets/IERs or a deal database.")


def _comps_regression(ctx, spec):
    return _mk(spec, status="na", missing=["peer dataset"],
              note="Needs a cross-section of peer multiples + fundamentals.")


def _justified_pe(ctx: Ctx, spec):
    if ctx.eps is None or ctx.dps is None or ctx.eps == 0:
        return _mk(spec, status="na", missing=["EPS", "DPS"])
    ke = ctx.cost_equity
    if not ke or ke <= ctx.g_term:
        return _mk(spec, status="na", missing=["cost of equity"])
    payout = ctx.dps / ctx.eps
    jpe = payout * (1 + ctx.g_term) / (ke - ctx.g_term)
    fair = jpe * ctx.eps
    return _mk(spec, status="partial", value=jpe, unit="x", intrinsic_ps=fair,
               note=f"Justified P/E={jpe:.1f}x -> fair {fair:.2f}/sh." + ctx.fx_note())


def _justified_pb(ctx: Ctx, spec):
    if ctx.book_equity is None or ctx.net_income is None:
        return _mk(spec, status="na", missing=["book equity", "net income"])
    ke = ctx.cost_equity
    if not ke or ke <= ctx.g_term:
        return _mk(spec, status="na", missing=["cost of equity"])
    roe = ctx.net_income / ctx.book_equity
    jpb = (roe - ctx.g_term) / (ke - ctx.g_term)
    bvps = ctx.per_share(ctx.book_equity)
    fair = jpb * bvps if bvps else None
    return _mk(spec, status="partial", value=jpb, unit="x", intrinsic_ps=fair,
               note=f"(ROE−g)/(ke−g); ROE={roe:.1%}." + ctx.fx_note())


def _justified_ps(ctx: Ctx, spec):
    if ctx.revenue is None or ctx.net_income is None or ctx.eps is None or ctx.dps is None:
        return _mk(spec, status="na", missing=["margin", "payout"])
    ke = ctx.cost_equity
    if not ke or ke <= ctx.g_term:
        return _mk(spec, status="na", missing=["cost of equity"])
    margin = ctx.net_income / ctx.revenue
    payout = ctx.dps / ctx.eps if ctx.eps else 0
    jps = margin * payout * (1 + ctx.g_term) / (ke - ctx.g_term)
    return _mk(spec, status="partial", value=jps, unit="x", note=f"Net margin={margin:.1%}.")


def _justified_dy(ctx: Ctx, spec):
    ke = ctx.cost_equity
    if not ke:
        return _mk(spec, status="na", missing=["cost of equity"])
    return _mk(spec, status="partial", value=ke - ctx.g_term, unit="yield",
               note="Justified dividend yield = ke − g.")


def _peg_fair(ctx: Ctx, spec):
    if ctx.eps is None:
        return _mk(spec, status="na", missing=["EPS", "benchmark PEG"])
    g = ctx.g_high * 100
    fair = 1.0 * g * ctx.eps  # PEG=1 benchmark
    return _mk(spec, status="partial", value=fair, unit=f"{ctx.currency}/share", intrinsic_ps=fair,
               note=f"Fair price at PEG=1 with {g:.0f}% growth." + ctx.fx_note())


def _graham_number(ctx: Ctx, spec):
    bvps = ctx.per_share(ctx.book_equity)
    if ctx.eps is None or bvps is None or ctx.eps < 0 or bvps < 0:
        return _mk(spec, status="na", missing=["positive EPS", "positive BVPS"])
    v = math.sqrt(22.5 * ctx.eps * bvps)
    return _mk(spec, status="ok", value=v, unit=f"{ctx.currency}/share", intrinsic_ps=v,
               note="√(22.5 × EPS × BVPS)." + ctx.fx_note())


def _graham_revised(ctx: Ctx, spec):
    if ctx.eps is None:
        return _mk(spec, status="na", missing=["EPS"])
    y = (ctx.risk_free * 100) if ctx.risk_free else None
    if y is None:
        return _mk(spec, status="partial", value=None, missing=["AAA/long-bond yield"],
                   note="Needs current AAA bond yield (FRED). V=EPS(8.5+2g)·4.4/Y.")
    g = ctx.g_high * 100
    v = ctx.eps * (8.5 + 2 * g) * 4.4 / y
    return _mk(spec, status="partial", value=v, unit=f"{ctx.currency}/share", intrinsic_ps=v,
               note=f"Graham revised, Y={y:.1f}%." + ctx.fx_note())


def _nopat(ctx):
    return None if ctx.ebit is None else ctx.ebit * (1 - ctx.tax_rate)


def _invested_capital(ctx):
    if ctx.book_equity is None or ctx.total_debt is None:
        return None
    return ctx.book_equity + ctx.total_debt - (ctx.cash or 0)


def _eva(ctx: Ctx, spec):
    nopat = _nopat(ctx)
    ic = _invested_capital(ctx)
    if nopat is None or ic is None or not ctx.wacc:
        return _mk(spec, status="na", missing=["NOPAT", "invested capital", "WACC"])
    eva = nopat - ctx.wacc * ic
    return _mk(spec, status="partial", value=eva, unit=f"{ctx.currency}m",
               note=f"NOPAT − WACC×IC; WACC={ctx.wacc:.1%}, IC={ic:,.0f}.")


def _disc_econ_profit(ctx: Ctx, spec):
    nopat = _nopat(ctx)
    ic = _invested_capital(ctx)
    if nopat is None or ic is None or not ctx.wacc or ctx.wacc <= ctx.g_term:
        return _mk(spec, status="na", missing=["NOPAT", "IC", "WACC>g"])
    ep = nopat - ctx.wacc * ic
    val = ic + ep * (1 + ctx.g_term) / (ctx.wacc - ctx.g_term) - (ctx.net_debt or 0)
    return _mk(spec, status="partial", value=val, unit=f"{ctx.currency}m", intrinsic_ps=ctx.per_share(val),
               note="McKinsey: IC + PV(economic profit) − net debt.")


def _mva(ctx: Ctx, spec):
    if ctx.market_cap is None or ctx.book_equity is None:
        return _mk(spec, status="na", missing=["market cap", "book equity"])
    return _mk(spec, status="ok", value=ctx.market_cap - ctx.book_equity, unit=f"{ctx.currency}m",
               note="Market cap − book equity.")


def _roic_wacc(ctx: Ctx, spec):
    nopat = _nopat(ctx)
    ic = _invested_capital(ctx)
    if nopat is None or ic in (None, 0) or not ctx.wacc:
        return _mk(spec, status="na", missing=["NOPAT", "IC", "WACC"])
    roic = nopat / ic
    return _mk(spec, status="partial", value=roic - ctx.wacc, unit="spread",
               note=f"ROIC={roic:.1%} vs WACC={ctx.wacc:.1%}.")


def _cfroi(ctx: Ctx, spec):
    if ctx.op_cf is None or _invested_capital(ctx) in (None, 0):
        return _mk(spec, status="na", missing=["gross cash flow", "gross investment"])
    return _mk(spec, status="partial", value=ctx.op_cf / _invested_capital(ctx), unit="ratio",
               note="Simplified CFROI ≈ operating cash flow / invested capital (not inflation-adjusted).")


def _reoi(ctx: Ctx, spec):
    return _disc_econ_profit(ctx, spec)  # same family, operating basis


def _mm_taxes(ctx: Ctx, spec):
    if ctx.total_debt is None:
        return _mk(spec, status="na", missing=["debt", "unlevered value"])
    shield = ctx.tax_rate * ctx.total_debt
    return _mk(spec, status="partial", value=shield, unit=f"{ctx.currency}m",
               note=f"Tax shield = T×D = {shield:,.0f}; add to unlevered value for VL.")


def _apv(ctx: Ctx, spec):
    if ctx.ebit is None or not ctx.wacc:
        return _mk(spec, status="na", missing=["unlevered FCF", "unlevered cost of equity"])
    vu = ctx.ebit * (1 - ctx.tax_rate) / ctx.wacc
    shield = ctx.tax_rate * (ctx.total_debt or 0)
    val = vu + shield - (ctx.net_debt or 0)
    return _mk(spec, status="partial", value=val, unit=f"{ctx.currency}m", intrinsic_ps=ctx.per_share(val),
               note="Vu (NOPAT/WACC) + tax shield − net debt; unlevered rate proxied by WACC.")


def _wacc(ctx: Ctx, spec):
    if ctx.wacc:
        status = "ok" if ctx.wacc_source == "calculated market-value WACC" and ctx.cost_debt is not None else "partial"
        comparison = f"; disclosed asset rate {ctx.disc_disclosed:.2%}" if ctx.disc_disclosed else ""
        return _mk(spec, status=status, value=ctx.wacc, unit="rate",
                   note=f"WACC = {ctx.wacc:.2%} ({ctx.wacc_source or 'derived'}){comparison}.")
    return _mk(spec, status="na", missing=["cost of equity", "cost of debt", "weights"])


def _capm(ctx: Ctx, spec):
    if ctx.beta is None or ctx.risk_free is None or ctx.erp is None:
        return _mk(spec, status="na", missing=["beta", "risk-free", "ERP"])
    ke = ctx.risk_free + ctx.beta * ctx.erp
    return _mk(spec, status="ok", value=ke, unit="rate",
               note=f"rf {ctx.risk_free:.2%} + β{ctx.beta:.2f}×ERP {ctx.erp:.2%}.")


def _factor_model(name):
    def fn(ctx: Ctx, spec):
        key = {"Fama–French 3": "ff3", "Fama–French 5": "ff5", "Carhart 4": "carhart4"}.get(name)
        model = ctx.factor_models.get(key) if key else None
        if model:
            loads = ", ".join(f"{k}={v:.2f}" for k, v in model["loadings"].items())
            return _mk(spec, status="ok", value=model["expected_return"], unit="expected return",
                       note=(f"{name} OLS on {model['observations']} aligned daily returns; "
                             f"R²={model['r_squared']:.2f}; {loads}."))
        if ctx.ff_factors:
            return _mk(spec, status="partial", value=None, missing=["factor loadings (regression)"],
                       note=f"{name} factors loaded; needs a return regression to estimate loadings.")
        return _mk(spec, status="na", missing=["factor-return dataset", "loadings"],
                   note=f"{name}: load Ken-French/AQR factors and regress stock returns.")
    return fn


def _apt(ctx, spec):
    model = ctx.macro_model
    if not model:
        return _mk(spec, status="na", missing=["macro-factor betas"],
                   note="Needs sufficient point-in-time price and FRED macro history.")
    loadings = ", ".join(f"{name}={value:.2f}" for name, value in model["loadings"].items())
    return _mk(spec, status="ok", value=model["expected_return"], unit="expected return",
               note=(f"APT OLS on {model['observations']} monthly observations through {model['as_of']}; "
                     f"R²={model['r_squared']:.2f}; {loadings}."))


def _build_up(ctx: Ctx, spec):
    if ctx.risk_free is None or ctx.erp is None:
        return _mk(spec, status="na", missing=["risk-free", "ERP", "size premium"])
    ke = ctx.risk_free + ctx.erp
    return _mk(spec, status="partial", value=ke, unit="rate",
               note="rf + ERP only; add size & specific-risk premia (Kroll) for full build-up.")


def _merton(ctx: Ctx, spec):
    E, D, sigmaE, r = ctx.market_cap, ctx.total_debt, ctx.hist_vol, ctx.risk_free
    if not all(x is not None for x in (E, D, sigmaE, r)) or not D:
        return _mk(spec, status="na", missing=["market cap", "debt", "equity vol", "risk-free"])
    T = 1.0
    # One-shot approximation: treat asset value ≈ E + D, asset vol ≈ equity vol × E/(E+D).
    V = E + D * 1_000_000 / 1_000_000  # keep m units consistent (E is currency, D is m)
    V = E + D
    sigmaV = sigmaE * E / V if V else sigmaE
    try:
        d2 = (math.log(V / D) + (r - 0.5 * sigmaV ** 2) * T) / (sigmaV * math.sqrt(T))
    except (ValueError, ZeroDivisionError):
        return _mk(spec, status="na", note="Distance-to-default undefined for these inputs.")
    from statistics import NormalDist
    pd = NormalDist().cdf(-d2)
    return _mk(spec, status="partial", value=d2, unit="DD",
               note=f"Distance-to-default≈{d2:.2f}, implied default prob≈{pd:.2%} (equity-vol proxy).")


def _black_scholes(ctx: Ctx, spec):
    return _mk(spec, status="na", missing=["asset value", "asset volatility"],
              note="Structural equity-as-call; use Merton output as the practical proxy.")


def _na_with(reasons, note):
    def fn(ctx, spec):
        return _mk(spec, status="na", missing=reasons, note=note)
    return fn


def _monte_carlo(ctx: Ctx, spec):
    if ctx.ebit is None or ctx.dna is None or ctx.wacc is None or not ctx.shares:
        return _mk(spec, status="na", missing=["base DCF", "input distributions"])
    try:
        import numpy as np

        rng = np.random.default_rng(65)
        count = 10_000
        capex = ctx.capex if ctx.capex is not None else ctx.dna
        base_fcf = ctx.ebit * (1 - ctx.tax_rate) + ctx.dna - capex
        fcf = base_fcf * rng.lognormal(mean=-0.5 * 0.15 ** 2, sigma=0.15, size=count)
        high_growth = np.clip(rng.normal(ctx.g_high, 0.025, count), -0.15, 0.20)
        terminal_growth = np.clip(rng.normal(ctx.g_term, 0.006, count), -0.02, 0.05)
        discount = np.clip(rng.normal(ctx.wacc, 0.0125, count), 0.03, 0.30)
        valid = discount > terminal_growth + 0.005
        values = []
        for cf, growth, terminal, rate in zip(
                fcf[valid], high_growth[valid], terminal_growth[valid], discount[valid], strict=True):
            enterprise = _two_stage_pv(float(cf), float(growth), float(terminal), float(rate), ctx.horizon)
            if enterprise is not None:
                values.append(ctx.per_share(enterprise - (ctx.net_debt or 0)))
        values = np.asarray([value for value in values if value is not None and np.isfinite(value) and value > 0])
        if len(values) < 1000:
            return _mk(spec, status="na", missing=["valid simulation draws"])
        p10, p50, p90 = np.percentile(values, [10, 50, 90])
        return _mk(spec, status="partial", value=float(p50), unit=f"{ctx.currency}/share",
                   intrinsic_ps=float(p50),
                   note=(f"10,000 deterministic FCFF draws; P10={p10:.2f}, median={p50:.2f}, P90={p90:.2f}; "
                         "FCF, growth, terminal growth and WACC varied."))
    except Exception as exc:
        return _mk(spec, status="na", note=f"Simulation failed: {type(exc).__name__}.")


def _scenario(ctx: Ctx, spec):
    if ctx.ebit is None or ctx.dna is None or ctx.wacc is None:
        return _mk(spec, status="na", missing=["base model"])
    capex = ctx.capex if ctx.capex is not None else ctx.dna
    fcf = ctx.ebit * (1 - ctx.tax_rate) + ctx.dna - capex
    scenarios = {
        "bear": (ctx.g_high - 0.03, ctx.g_term - 0.005, ctx.wacc + 0.015),
        "base": (ctx.g_high, ctx.g_term, ctx.wacc),
        "bull": (ctx.g_high + 0.03, ctx.g_term + 0.005, max(ctx.wacc - 0.015, ctx.g_term + 0.01)),
    }
    per_share = {}
    for name, (growth, terminal, rate) in scenarios.items():
        enterprise = _two_stage_pv(fcf, growth, terminal, rate, ctx.horizon)
        per_share[name] = ctx.per_share(enterprise - (ctx.net_debt or 0)) if enterprise is not None else None
    if per_share["base"] is None:
        return _mk(spec, status="na", missing=["valid base scenario"])
    return _mk(spec, status="partial", value=per_share["base"], unit=f"{ctx.currency}/share",
               intrinsic_ps=per_share["base"],
               note=(f"Bear={per_share['bear']:.2f} / Base={per_share['base']:.2f} / "
                     f"Bull={per_share['bull']:.2f}; growth ±3pp, terminal growth ±0.5pp, WACC ∓1.5pp."))


def _reverse_dcf(ctx: Ctx, spec):
    fcf0 = ctx.free_cash_flow()
    if ctx.price is None or fcf0 is None or not ctx.wacc or not ctx.shares:
        return _mk(spec, status="na", missing=["price", "FCF", "WACC"])
    mcap = ctx.price * ctx.shares / 1_000_000
    # implied perpetuity growth from price: mcap = fcf0*(1+g)/(wacc-g)
    denom = mcap + fcf0
    if denom == 0:
        return _mk(spec, status="na")
    g = (ctx.wacc * mcap - fcf0) / denom
    return _mk(spec, status="partial", value=g, unit="implied g",
               note=f"Growth the market is pricing in ≈ {g:.1%} (at WACC {ctx.wacc:.1%})." + ctx.fx_note())


def _icc(ctx: Ctx, spec):
    earnings = ctx.forward_eps if ctx.forward_eps is not None else ctx.eps
    if ctx.price is None or earnings is None or ctx.price == 0:
        return _mk(spec, status="na", missing=["price", "forward earnings"])
    # single-stage ICC proxy: r = E1/P + g
    r = earnings / ctx.price + ctx.g_term
    source = "forward EPS" if ctx.forward_eps is not None else "trailing EPS proxy"
    return _mk(spec, status="ok" if ctx.forward_eps is not None else "partial", value=r, unit="rate",
               note=f"Implied cost of capital ≈ E1/P + g using {source}." + ctx.fx_note())


def _total_yield(ctx: Ctx, spec):
    if ctx.dps is None or ctx.price in (None, 0):
        return _mk(spec, status="na", missing=["DPS", "price", "buybacks"])
    dy = ctx.dps / ctx.price
    if ctx.buyback_yield is None:
        return _mk(spec, status="partial", value=dy, unit="yield", missing=["buybacks"],
                   note="Dividend yield shown; buyback yield unavailable." + ctx.fx_note())
    total = dy + ctx.buyback_yield
    return _mk(spec, status="ok", value=total, unit="yield",
               note=f"Dividend yield {dy:.2%} + buyback yield {ctx.buyback_yield:.2%}." + ctx.fx_note())


def _analyst_targets(ctx: Ctx, spec):
    if ctx.target_mean:
        source = "Finnhub" if ctx.market_sources.get("consensus") else "Yahoo Finance"
        return _mk(spec, status="ok", value=ctx.target_mean, unit=f"{ctx.market_ccy}/share",
                   intrinsic_ps=ctx.target_mean, note=f"{source} consensus mean target.")
    return _mk(spec, status="na", missing=["analyst consensus (Finnhub key)"])


def _rule_72(ctx: Ctx, spec):
    g = ctx.g_high
    if not g:
        return _mk(spec, status="na", missing=["growth rate"])
    return _mk(spec, status="ok", value=72 / (g * 100), unit="years",
               note=f"Years to double earnings at {g:.0%} ≈ {72/(g*100):.0f}.")


def _exit_multiple_tv(ctx: Ctx, spec):
    if ctx.ebitda is None:
        return _mk(spec, status="na", missing=["EBITDA", "exit multiple"])
    return _mk(spec, status="partial", value=None, missing=["peer exit multiple"],
               note="Assign a peer EV/EBITDA to terminal EBITDA to get exit-multiple TV.")


def _gordon_tv(ctx: Ctx, spec):
    fcf = ctx.free_cash_flow()
    if fcf is None or not ctx.wacc or ctx.wacc <= ctx.g_term:
        return _mk(spec, status="na", missing=["FCF", "WACC>g"])
    tv = fcf * (1 + ctx.g_term) / (ctx.wacc - ctx.g_term)
    return _mk(spec, status="partial", value=tv, unit=f"{ctx.currency}m",
               note=f"Terminal value = FCF(1+g)/(WACC−g), g={ctx.g_term:.1%}; {ctx.fcf_note()}.")


def _fcff_perpetuity(ctx: Ctx, spec):
    if ctx.ebit is None or not ctx.wacc or ctx.wacc <= ctx.g_term:
        return _mk(spec, status="na", missing=["FCFF", "WACC>g"])
    fcff = ctx.ebit * (1 - ctx.tax_rate) + (ctx.dna or 0) - (ctx.capex if ctx.capex is not None else (ctx.dna or 0))
    ev = fcff * (1 + ctx.g_term) / (ctx.wacc - ctx.g_term)
    equity = ev - (ctx.net_debt or 0)
    return _mk(spec, status="partial", value=equity, unit=f"{ctx.currency}m", intrinsic_ps=ctx.per_share(equity),
               note="Single-stage FCFF perpetuity.")


def _sotp(ctx, spec):
    return _mk(spec, status="na", missing=["segment multiples/reserves"],
              note="Segment data is in the reports; assign per-segment multiples or run per-segment DCF.")


def _efficient_price(ctx: Ctx, spec):
    if ctx.price is None:
        return _mk(spec, status="na", missing=["market price"])
    return _mk(spec, status="ok", value=ctx.price, unit=f"{ctx.market_ccy}/share",
               note="Efficient-market view: today's price is the value.")


def _technical(ctx, spec):
    t = ctx.technical
    if not t:
        return _mk(spec, status="na", missing=["full daily price series"],
                   note="Needs sufficient dated daily closes on/before the valuation date.")
    signals = []
    if t.get("sma_50") is not None:
        signals.append("above 50-day SMA" if t["close"] >= t["sma_50"] else "below 50-day SMA")
    if t.get("sma_200") is not None:
        signals.append("above 200-day SMA" if t["close"] >= t["sma_200"] else "below 200-day SMA")
    if t.get("rsi_14") is not None:
        signals.append(f"RSI(14)={t['rsi_14']:.1f}")
    signals.append("MACD positive" if t["macd"] >= t["macd_signal"] else "MACD negative")
    score_inputs = [t["macd"] >= t["macd_signal"]]
    if t.get("sma_50") is not None:
        score_inputs.append(t["close"] >= t["sma_50"])
    if t.get("sma_200") is not None:
        score_inputs.append(t["close"] >= t["sma_200"])
    score = sum(score_inputs) / len(score_inputs)
    return _mk(spec, status="ok", value=score, unit="signal score",
               note=f"As of {t['as_of']} ({t['observations']} closes): " + "; ".join(signals) + ".")


def _quant(ctx, spec):
    model = ctx.factor_models.get("ff5")
    if not model:
        return _mk(spec, status="na", missing=["cross-sectional factor exposures"],
                   note="Needs sufficient aligned security and factor returns.")
    style = max(model["loadings"].items(), key=lambda item: abs(item[1]))
    return _mk(spec, status="partial", value=model["expected_return"], unit="factor expected return",
               note=(f"Free-data FF5 style proxy; dominant exposure {style[0]}={style[1]:.2f}, "
                     f"R²={model['r_squared']:.2f}. Not a proprietary Barra/Axioma cross-section."))


# --------------------------------------------------------------------------- #
#  Registry — all 65 in reference order                                        #
# --------------------------------------------------------------------------- #
SPEC: list[dict] = []


def _add(section, name, fn):
    SPEC.append({"id": len(SPEC) + 1, "section": section, "name": name, "fn": fn})


S1 = "1. Absolute / Intrinsic"
S2 = "2. Asset-Based & Floor"
S3 = "3. Relative (Multiples)"
S4 = "4. Justified Multiples"
S5 = "5. Economic-Profit"
S6 = "6. Discount Rates & Factor Models"
S7 = "7. Option-Based"
S8 = "8. Probabilistic / Reverse / Practical"
S9 = "9. Market-Based & Technical"

_add(S1, "Discounted Cash Flow (DCF) — FCFF", _fcff)
_add(S1, "Discounted Cash Flow (DCF) — FCFE", _fcfe)
_add(S1, "Dividend Discount Model — Gordon Growth", _ddm_gordon)
_add(S1, "Multi-Stage / Two-Stage DDM", _ddm_multi)
_add(S1, "H-Model", _h_model)
_add(S1, "Residual Income / Excess Return Model", _residual_income)
_add(S1, "Owner Earnings (Buffett) DCF", _owner_earnings)
_add(S1, "Capitalization of Earnings", _cap_earnings)
_add(S2, "Book Value / Adjusted Book Value", _book_value)
_add(S2, "Net Current Asset Value (NCAV) / Net-Net", _ncav)
_add(S2, "Liquidation Value (Orderly vs. Forced)", _liquidation)
_add(S2, "Replacement Cost / Reproduction Value", _replacement)
_add(S2, "Earnings Power Value (EPV)", _epv)
_add(S2, "Tobin's Q", _tobin_q)
_add(S3, "Trailing / Forward P/E", _pe)
_add(S3, "PEG Ratio", _peg)
_add(S3, "Price / Book (P/B)", _pb)
_add(S3, "Price / Sales (P/S)", _ps)
_add(S3, "EV / EBITDA", _ev_ebitda)
_add(S3, "EV/Sales, EV/EBIT, EV/FCF", _ev_multi)
_add(S3, "Price / Cash Flow (P/CF)", _pcf)
_add(S3, "Dividend Yield Comparison", _div_yield)
_add(S3, "Precedent Transactions", _precedent)
_add(S3, "Comparable Company Regression", _comps_regression)
_add(S3, "Free Cash Flow Yield", _fcf_yield)
_add(S4, "Justified P/E", _justified_pe)
_add(S4, "Justified P/B", _justified_pb)
_add(S4, "Justified P/S", _justified_ps)
_add(S4, "Justified Dividend Yield", _justified_dy)
_add(S4, "PEG-Based Fair Value", _peg_fair)
_add(S4, "Graham Number", _graham_number)
_add(S4, "Graham's Revised Intrinsic Value Formula", _graham_revised)
_add(S5, "Economic Value Added (EVA)", _eva)
_add(S5, "Discounted Economic Profit (McKinsey)", _disc_econ_profit)
_add(S5, "Market Value Added (MVA)", _mva)
_add(S5, "Return on Invested Capital vs. WACC Spread", _roic_wacc)
_add(S5, "Cash Flow Return on Investment (CFROI)", _cfroi)
_add(S5, "Discounted Abnormal Operating Earnings (ReOI)", _reoi)
_add(S5, "Modigliani–Miller with Taxes", _mm_taxes)
_add(S5, "Adjusted Present Value (APV)", _apv)
_add(S6, "Weighted Average Cost of Capital (WACC)", _wacc)
_add(S6, "Capital Asset Pricing Model (CAPM)", _capm)
_add(S6, "Fama–French 3-Factor", _factor_model("Fama–French 3"))
_add(S6, "Fama–French 5-Factor", _factor_model("Fama–French 5"))
_add(S6, "Carhart 4-Factor", _factor_model("Carhart 4"))
_add(S6, "Arbitrage Pricing Theory (APT)", _apt)
_add(S6, "Build-Up Method", _build_up)
_add(S7, "Black–Scholes (Equity as a Call on Assets)", _black_scholes)
_add(S7, "Merton Structural / Distance-to-Default", _merton)
_add(S7, "Binomial / Lattice Model", _na_with(["volatility", "lattice params"], "Needs a volatility term structure."))
_add(S7, "Real Options Valuation",
     _na_with(["project volatility", "option params"], "Needs project-level option parameters."))
_add(S8, "Monte Carlo Intrinsic Value", _monte_carlo)
_add(S8, "Scenario & Sensitivity Analysis", _scenario)
_add(S8, "Reverse DCF", _reverse_dcf)
_add(S8, "Implied Cost of Capital (ICC)", _icc)
_add(S8, "Dividend + Buyback (Total Shareholder Yield)", _total_yield)
_add(S8, "Analyst Consensus Price Targets", _analyst_targets)
_add(S8, "Rule of 72 / Growth Cross-Check", _rule_72)
_add(S8, "Exit-Multiple Terminal Value", _exit_multiple_tv)
_add(S8, "Gordon-Growth Terminal Value", _gordon_tv)
_add(S8, "FCFF Perpetuity (Single-Stage)", _fcff_perpetuity)
_add(S8, "Sum-of-the-Parts (SOTP)", _sotp)
_add(S9, "Efficient-Market Price", _efficient_price)
_add(S9, "Technical Analysis", _technical)
_add(S9, "Quantitative Factor / Style Models", _quant)

assert len(SPEC) == 65, f"expected 65 methods, got {len(SPEC)}"
