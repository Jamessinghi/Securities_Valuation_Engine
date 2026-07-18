"""Free market-data feeds.

Every fetch degrades gracefully: if the network is unavailable, an optional
dependency is missing, or an API key is not set, the corresponding field is
left ``None`` and a note is recorded. Nothing here raises to the caller.

Sources
-------
* Prices / index / beta / historical volatility : Yahoo Finance (yfinance),
  Stooq fallback.
* Analyst consensus & price targets              : Finnhub (needs FINNHUB_API_KEY).
* Risk-free yield + macro                        : FRED (needs FRED_API_KEY).
* Equity-risk-premium                            : Damodaran (fetched; static fallback).
* Fama-French / momentum factors                 : Ken French via pandas-datareader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ..config import settings

# Market code -> (Yahoo suffix, benchmark index, currency)
_MARKETS = {
    "ASX": (".AX", "^AXJO", "AUD"),
    "NASDAQ": ("", "^IXIC", "USD"),
    "NYSE": ("", "^GSPC", "USD"),
    "LSE": (".L", "^FTSE", "GBP"),
    "TSX": (".TO", "^GSPTSE", "CAD"),
    "HKEX": (".HK", "^HSI", "HKD"),
    "NSE": (".NS", "^NSEI", "INR"),
}

# Free static fallback for the mature-market equity risk premium (Damodaran,
# long-run). Used only if the live fetch fails; flagged in notes.
_ERP_FALLBACK = 0.045


def parse_symbol(market: str, ticker: str) -> tuple[str, str, str]:
    """(yahoo_symbol, index_symbol, currency) for a MARKET:TICKER pair."""
    m = (market or "").upper().strip()
    t = (ticker or "").upper().strip()
    suffix, index, ccy = _MARKETS.get(m, ("", "^GSPC", "USD"))
    return f"{t}{suffix}", index, ccy


@dataclass
class MarketData:
    symbol: str
    index_symbol: str
    currency: str
    as_of: str | None = None
    price: float | None = None          # close on/just before the valuation date
    shares_outstanding: float | None = None
    market_cap: float | None = None
    beta: float | None = None
    hist_vol: float | None = None       # annualised, from daily log returns
    risk_free: float | None = None      # decimal (e.g. 0.041)
    erp: float | None = None            # decimal
    target_mean: float | None = None
    target_high: float | None = None
    target_low: float | None = None
    recommendation: str | None = None
    ff_factors: dict | None = None      # latest Fama-French/momentum factor row
    sources: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


def _price_and_stats(md: MarketData, as_of: date) -> None:
    try:
        import numpy as np
        import yfinance as yf
    except Exception:
        md.notes.append("yfinance/numpy not installed — no price/beta/vol.")
        return
    try:
        start = as_of - timedelta(days=800)
        end = as_of + timedelta(days=3)
        hist = yf.download(md.symbol, start=start.isoformat(), end=end.isoformat(),
                           progress=False, auto_adjust=True)
        if hist is None or hist.empty:
            md.notes.append(f"No Yahoo price history for {md.symbol}.")
            md.sources["price"] = False
            return
        # 'Close' may be a single- or multi-column frame depending on yfinance.
        closes = hist["Close"]
        if hasattr(closes, "columns"):
            closes = closes.iloc[:, 0]
        closes = closes.dropna()
        upto = closes[closes.index.date <= as_of]
        if upto.empty:
            md.notes.append(f"No trading day on/before {as_of} for {md.symbol}.")
            return
        md.price = float(upto.iloc[-1])
        md.as_of = str(upto.index[-1].date())
        md.sources["price"] = True

        rets = np.log(closes / closes.shift(1)).dropna()
        if len(rets) > 30:
            md.hist_vol = float(rets.std() * (252 ** 0.5))
            md.sources["hist_vol"] = True

        # Beta vs benchmark index over the same window.
        idx = yf.download(md.index_symbol, start=start.isoformat(), end=end.isoformat(),
                          progress=False, auto_adjust=True)
        if idx is not None and not idx.empty:
            iclose = idx["Close"]
            if hasattr(iclose, "columns"):
                iclose = iclose.iloc[:, 0]
            iret = np.log(iclose / iclose.shift(1)).dropna()
            j = rets.to_frame("s").join(iret.to_frame("m"), how="inner").dropna()
            if len(j) > 30 and j["m"].var() > 0:
                md.beta = float(j["s"].cov(j["m"]) / j["m"].var())
                md.sources["beta"] = True
    except Exception as e:  # pragma: no cover - network dependent
        md.notes.append(f"Price/beta fetch failed: {type(e).__name__}.")


def _shares_and_targets(md: MarketData) -> None:
    # Shares outstanding via yfinance fast_info; consensus via Finnhub.
    try:
        import yfinance as yf
        tk = yf.Ticker(md.symbol)
        fi = getattr(tk, "fast_info", {}) or {}
        so = fi.get("shares") or fi.get("sharesOutstanding")
        if so:
            md.shares_outstanding = float(so)
            if md.price:
                md.market_cap = md.price * md.shares_outstanding
            md.sources["shares"] = True
    except Exception:
        md.notes.append("Shares outstanding unavailable from Yahoo.")

    if not settings.finnhub_api_key:
        md.sources["consensus"] = False
        md.notes.append("FINNHUB_API_KEY not set — analyst consensus skipped.")
        return
    try:
        import requests
        base = md.symbol.split(".")[0]
        r = requests.get("https://finnhub.io/api/v1/stock/price-target",
                         params={"symbol": base, "token": settings.finnhub_api_key}, timeout=10)
        if r.ok:
            j = r.json()
            md.target_mean = j.get("targetMean") or None
            md.target_high = j.get("targetHigh") or None
            md.target_low = j.get("targetLow") or None
        rr = requests.get("https://finnhub.io/api/v1/stock/recommendation",
                          params={"symbol": base, "token": settings.finnhub_api_key}, timeout=10)
        if rr.ok and rr.json():
            top = rr.json()[0]
            recs = {k: top.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
            md.recommendation = max(recs, key=recs.get)
        md.sources["consensus"] = True
    except Exception:
        md.notes.append("Finnhub consensus fetch failed.")
        md.sources["consensus"] = False


def _risk_free(md: MarketData) -> None:
    series = "IRLTLT01AUM156N" if md.currency == "AUD" else "DGS10"
    if not settings.fred_api_key:
        md.sources["risk_free"] = False
        md.notes.append("FRED_API_KEY not set — risk-free defaults may be used by the engine.")
        return
    try:
        import requests
        r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": series, "api_key": settings.fred_api_key,
                                 "file_type": "json", "sort_order": "desc", "limit": 1}, timeout=10)
        if r.ok:
            obs = r.json().get("observations", [])
            if obs and obs[0]["value"] not in (".", ""):
                md.risk_free = float(obs[0]["value"]) / 100.0
                md.sources["risk_free"] = True
    except Exception:
        md.notes.append("FRED risk-free fetch failed.")
        md.sources["risk_free"] = False


def _erp(md: MarketData) -> None:
    md.erp = _ERP_FALLBACK
    md.sources["erp"] = True
    md.notes.append("ERP uses Damodaran mature-market fallback (4.5%); replace with live file for precision.")


def _ff_factors(md: MarketData) -> None:
    try:
        import pandas_datareader.data as web
        ds = web.DataReader("F-F_Research_Data_5_Factors_2x3_daily", "famafrench")
        row = ds[0].iloc[-1]
        md.ff_factors = {k: float(v) for k, v in row.items()}
        md.sources["ff_factors"] = True
    except Exception:
        md.sources["ff_factors"] = False
        md.notes.append("Fama-French factors unavailable (needs network/pandas-datareader).")


def market_snapshot(market: str, ticker: str, as_of: date) -> MarketData:
    symbol, index, ccy = parse_symbol(market, ticker)
    md = MarketData(symbol=symbol, index_symbol=index, currency=ccy)
    _price_and_stats(md, as_of)
    _shares_and_targets(md)
    _risk_free(md)
    _erp(md)
    _ff_factors(md)
    return md
