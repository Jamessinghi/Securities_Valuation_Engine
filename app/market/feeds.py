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
* Equity-risk-premium                            : Damodaran long-run static fallback.
* Fama-French / momentum factors                 : Ken French via pandas-datareader.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import settings
from ..data import canonicalize

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

_STOOQ_MARKETS = {
    "ASX": (".AU", "^AOR"),
    "NASDAQ": (".US", "^NDQ"),
    "NYSE": (".US", "^SPX"),
    "LSE": (".UK", "^UKX"),
}

_EODHD_EXCHANGES = {
    "ASX": "AU", "NASDAQ": "US", "NYSE": "US", "LSE": "LSE", "TSX": "TO",
    "HKEX": "HK", "NSE": "NSE",
}

# Free static fallback for the mature-market equity risk premium (Damodaran,
# long-run). Used only if the live fetch fails; flagged in notes.
_ERP_FALLBACK = 0.045
_CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "market"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_VERSION = 2


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
    raw_close: float | None = None
    adjusted_close: float | None = None
    price_type: str | None = None
    shares_outstanding: float | None = None
    market_cap: float | None = None
    beta: float | None = None
    beta_raw: float | None = None
    beta_adjusted: float | None = None
    hist_vol: float | None = None       # annualised, from daily log returns
    risk_free: float | None = None      # decimal (e.g. 0.041)
    erp: float | None = None            # decimal
    target_mean: float | None = None
    target_high: float | None = None
    target_low: float | None = None
    recommendation: str | None = None
    forward_eps: float | None = None
    buyback_yield: float | None = None
    technical: dict | None = None       # indicators computed from dated closes
    factor_models: dict | None = None   # stock-return regressions + expected returns
    ff_factors: dict | None = None      # latest Fama-French factor row
    macro_model: dict | None = None     # APT-style monthly macro regression
    sources: dict = field(default_factory=dict)
    canonical_inputs: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d.pop("_returns", None)
        return d


def _price_and_stats(md: MarketData, as_of: date) -> None:
    try:
        import numpy as np
        import yfinance as yf
    except Exception:
        md.notes.append("yfinance/numpy not installed — no price/beta/vol.")
        return
    try:
        # Five years supports a more stable weekly beta while retaining daily
        # observations for volatility and technical indicators.
        start = as_of - timedelta(days=1900)
        end = as_of + timedelta(days=3)
        hist = yf.download(md.symbol, start=start.isoformat(), end=end.isoformat(),
                           progress=False, auto_adjust=False, actions=True)
        if hist is None or hist.empty:
            md.notes.append(f"No Yahoo price history for {md.symbol}.")
            md.sources["price"] = False
            return
        # 'Close' may be a single- or multi-column frame depending on yfinance.
        closes = hist["Adj Close"] if "Adj Close" in hist else hist["Close"]
        if hasattr(closes, "columns"):
            closes = closes.iloc[:, 0]
        closes = closes.dropna()
        upto = closes[closes.index.date <= as_of]
        if upto.empty:
            md.notes.append(f"No trading day on/before {as_of} for {md.symbol}.")
            return
        raw = hist["Close"]
        if hasattr(raw, "columns"):
            raw = raw.iloc[:, 0]
        raw_upto = raw.dropna()[raw.dropna().index.date <= as_of]
        md.raw_close = float(raw_upto.iloc[-1]) if not raw_upto.empty else None
        md.adjusted_close = float(upto.iloc[-1])
        md.price = md.raw_close
        md.price_type = "raw_close"
        md.as_of = str(upto.index[-1].date())
        md.sources["price"] = "Yahoo Finance"
        md.canonical_inputs["price"] = canonicalize(
            md.price, currency=md.currency, units="currency_per_share", source="Yahoo Finance",
            source_type="market_feed", confidence="high", as_of_date=md.as_of)
        md.canonical_inputs["adjusted_close"] = canonicalize(
            md.adjusted_close, currency=md.currency, units="adjusted_currency_per_share",
            source="Yahoo Finance", source_type="market_feed", confidence="high", as_of_date=md.as_of)

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
            weekly_stock = closes.resample("W-FRI").last().pct_change(fill_method=None).dropna()
            weekly_market = iclose.resample("W-FRI").last().pct_change(fill_method=None).dropna()
            j = weekly_stock.to_frame("s").join(weekly_market.to_frame("m"), how="inner").dropna()
            if len(j) > 30 and j["m"].var() > 0:
                md.beta_raw = float(j["s"].cov(j["m"]) / j["m"].var())
                md.beta_adjusted = 0.67 * md.beta_raw + 0.33
                md.beta = md.beta_adjusted
                md.sources["beta"] = True

        # Indicators are calculated only from observations known on/before the
        # valuation date. This avoids look-ahead bias in historical valuations.
        px = upto.astype(float)
        if len(px) >= 26:
            delta = px.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss.replace(0, float("nan"))
            ema12 = px.ewm(span=12, adjust=False).mean()
            ema26 = px.ewm(span=26, adjust=False).mean()
            macd = ema12 - ema26
            md.technical = {
                "close": float(px.iloc[-1]),
                "sma_20": float(px.tail(20).mean()),
                "sma_50": float(px.tail(50).mean()) if len(px) >= 50 else None,
                "sma_200": float(px.tail(200).mean()) if len(px) >= 200 else None,
                "rsi_14": (
                    float((100 - 100 / (1 + rs)).iloc[-1])
                    if not rs.empty and not np.isnan(rs.iloc[-1]) else None
                ),
                "macd": float(macd.iloc[-1]),
                "macd_signal": float(macd.ewm(span=9, adjust=False).mean().iloc[-1]),
                "observations": int(len(px)),
                "as_of": str(px.index[-1].date()),
            }
            md.sources["technical"] = True

        # Keep a private, non-serialised return series for the factor regression.
        md._returns = rets
    except Exception as e:  # pragma: no cover - network dependent
        md.notes.append(f"Price/beta fetch failed: {type(e).__name__}.")


def _stooq_fallback(md: MarketData, market: str, ticker: str, as_of: date) -> None:
    """Best-effort keyless backup for price, volatility, and beta."""
    if md.price is not None or market not in _STOOQ_MARKETS:
        return
    try:
        import numpy as np
        import pandas_datareader.data as web

        suffix, benchmark = _STOOQ_MARKETS[market]
        start = as_of - timedelta(days=800)
        end = as_of + timedelta(days=1)
        stock = web.DataReader(f"{ticker}{suffix}", "stooq", start, end).sort_index()
        index = web.DataReader(benchmark, "stooq", start, end).sort_index()
        stock = stock[stock.index.date <= as_of]
        index = index[index.index.date <= as_of]
        if stock.empty:
            return
        closes = stock["Close"].dropna()
        md.price = float(closes.iloc[-1])
        md.as_of = str(closes.index[-1].date())
        md.sources["price"] = "Stooq"
        rets = np.log(closes / closes.shift(1)).dropna()
        if len(rets) > 30:
            md.hist_vol = float(rets.std() * (252 ** 0.5))
            md.sources["hist_vol"] = "Stooq"
        if not index.empty:
            idx_rets = np.log(index["Close"] / index["Close"].shift(1)).dropna()
            joined = rets.rename("s").to_frame().join(idx_rets.rename("m").to_frame()).dropna()
            if len(joined) > 30 and joined["m"].var() > 0:
                md.beta = float(joined["s"].cov(joined["m"]) / joined["m"].var())
                md.sources["beta"] = "Stooq"
        md._returns = rets
    except Exception as exc:  # pragma: no cover - network dependent
        md.notes.append(f"Stooq fallback failed: {type(exc).__name__}.")


def _eodhd_fallback(md: MarketData, market: str, ticker: str, as_of: date) -> None:
    """Optional free EODHD fallback for recent global end-of-day prices."""
    if md.price is not None or not settings.eodhd_api_key or as_of < date.today() - timedelta(days=366):
        return
    exchange = _EODHD_EXCHANGES.get(market)
    if not exchange:
        return
    try:
        import requests

        start = as_of - timedelta(days=10)
        response = requests.get(
            f"https://eodhd.com/api/eod/{ticker}.{exchange}",
            params={"api_token": settings.eodhd_api_key, "fmt": "json", "from": start.isoformat(),
                    "to": as_of.isoformat(), "period": "d", "order": "a"}, timeout=15)
        if not response.ok:
            return
        rows = [row for row in response.json() if row.get("date", "9999-12-31") <= as_of.isoformat()]
        if not rows:
            return
        row = max(rows, key=lambda item: item["date"])
        md.raw_close = float(row["close"])
        md.adjusted_close = float(row.get("adjusted_close") or row["close"])
        md.price = md.raw_close
        md.price_type = "raw_close"
        md.as_of = row["date"]
        md.sources["price"] = "EODHD"
        md.canonical_inputs["price"] = canonicalize(
            md.price, currency=md.currency, units="currency_per_share", source="EODHD",
            source_type="market_feed", confidence="high", as_of_date=md.as_of)
        md.canonical_inputs["adjusted_close"] = canonicalize(
            md.adjusted_close, currency=md.currency, units="adjusted_currency_per_share", source="EODHD",
            source_type="market_feed", confidence="high", as_of_date=md.as_of)
    except Exception as exc:  # pragma: no cover - provider/network dependent
        md.notes.append(f"EODHD fallback failed: {type(exc).__name__}.")


def _shares_and_targets(md: MarketData, as_of: date) -> None:
    # Shares outstanding via yfinance fast_info; consensus via Finnhub.
    current_snapshot = date.today() - timedelta(days=7) <= as_of <= date.today() + timedelta(days=1)
    try:
        import yfinance as yf
        tk = yf.Ticker(md.symbol)
        fi = getattr(tk, "fast_info", {}) or {}
        so = fi.get("shares") or fi.get("sharesOutstanding")
        if so and current_snapshot:
            md.shares_outstanding = float(so)
            if md.price:
                md.market_cap = md.price * md.shares_outstanding
            md.sources["shares"] = True
        if current_snapshot:
            targets = getattr(tk, "analyst_price_targets", None) or {}
            md.target_mean = targets.get("mean") or targets.get("current") or md.target_mean
            md.target_high = targets.get("high") or md.target_high
            md.target_low = targets.get("low") or md.target_low
            if md.target_mean:
                md.sources["consensus_yahoo"] = True
            estimates = getattr(tk, "earnings_estimate", None)
            if estimates is not None and not getattr(estimates, "empty", True):
                for period in ("+1y", "0y", "+1q", "0q"):
                    if period in estimates.index and "avg" in estimates.columns:
                        val = estimates.loc[period, "avg"]
                        if val == val:
                            md.forward_eps = float(val)
                            md.sources["forward_eps"] = True
                            break

        # Repurchases are stored as a negative financing cash flow. Convert the
        # latest annual magnitude into a yield using the valuation-date market cap.
        cf = getattr(tk, "cashflow", None)
        if cf is not None and not getattr(cf, "empty", True) and md.market_cap:
            for label in ("Repurchase Of Capital Stock", "Repurchase Of Stock"):
                if label in cf.index:
                    vals = cf.loc[label].dropna()
                    dated = [col for col in vals.index if getattr(col, "date", lambda: date.max)() <= as_of]
                    vals = vals.loc[dated] if dated else vals.iloc[0:0]
                    if not vals.empty:
                        md.buyback_yield = abs(float(vals.iloc[0])) / md.market_cap
                        md.sources["buybacks"] = True
                        break
    except Exception:
        md.notes.append("Shares outstanding unavailable from Yahoo.")

    if not current_snapshot:
        md.sources["consensus"] = False
        md.notes.append(
            "Historical analyst consensus unavailable without a point-in-time dataset; current data not used."
        )
        return
    if not settings.finnhub_api_key:
        md.sources["consensus"] = False
        md.notes.append("FINNHUB_API_KEY not set — analyst consensus skipped.")
        return
    try:
        import requests
        # Preserve exchange suffixes for international securities. Stripping
        # '.AX' can silently turn an ASX ticker into a different global symbol.
        base = md.symbol
        r = requests.get("https://finnhub.io/api/v1/stock/price-target",
                         params={"symbol": base, "token": settings.finnhub_api_key}, timeout=10)
        finnhub_data = False
        if r.ok:
            j = r.json()
            md.target_mean = j.get("targetMean") or md.target_mean
            md.target_high = j.get("targetHigh") or md.target_high
            md.target_low = j.get("targetLow") or md.target_low
            finnhub_data = bool(j.get("targetMean"))
        rr = requests.get("https://finnhub.io/api/v1/stock/recommendation",
                          params={"symbol": base, "token": settings.finnhub_api_key}, timeout=10)
        if rr.ok and rr.json():
            top = rr.json()[0]
            recs = {k: top.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
            md.recommendation = max(recs, key=recs.get)
            finnhub_data = True
        md.sources["consensus"] = finnhub_data
        if not finnhub_data and not md.target_mean:
            md.notes.append(f"Finnhub free tier returned no consensus for {md.symbol}.")
    except Exception:
        md.notes.append("Finnhub consensus fetch failed.")
        md.sources["consensus"] = False


def _risk_free(md: MarketData, as_of: date) -> None:
    series = "IRLTLT01AUM156N" if md.currency == "AUD" else "DGS10"
    if not settings.fred_api_key:
        if md.currency == "AUD" and _rba_risk_free(md, as_of):
            return
        md.sources["risk_free"] = False
        md.notes.append("FRED_API_KEY not set and official fallback unavailable.")
        return
    try:
        import requests
        r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": series, "api_key": settings.fred_api_key,
                                 "file_type": "json", "observation_end": as_of.isoformat(),
                                 "sort_order": "desc", "limit": 10}, timeout=10)
        if r.ok:
            obs = r.json().get("observations", [])
            valid = next((o for o in obs if o.get("value") not in (".", "", None)), None)
            if valid:
                md.risk_free = float(valid["value"]) / 100.0
                md.sources["risk_free"] = True
                md.sources["risk_free_as_of"] = valid.get("date")
                md.canonical_inputs["risk_free"] = canonicalize(
                    md.risk_free, currency=None, units="decimal_rate", source=f"FRED:{series}",
                    source_type="macro_feed", confidence="high", as_of_date=valid.get("date"))
    except Exception:
        md.notes.append("FRED risk-free fetch failed.")
        md.sources["risk_free"] = False
    if md.risk_free is None and md.currency == "AUD":
        _rba_risk_free(md, as_of)


def _rba_risk_free(md: MarketData, as_of: date) -> bool:
    """Official keyless Australian 10-year government-bond fallback."""
    try:
        import csv
        from io import StringIO

        import requests

        response = requests.get("https://www.rba.gov.au/statistics/tables/csv/f2-data.csv", timeout=20)
        response.raise_for_status()
        candidates = []
        for row in csv.reader(StringIO(response.text)):
            if len(row) < 5:
                continue
            try:
                observed = date.fromisoformat(row[0])
                value = float(row[4]) / 100.0
            except (ValueError, TypeError):
                try:
                    observed = datetime.strptime(row[0], "%d-%b-%Y").date()
                except ValueError:
                    continue
                try:
                    value = float(row[4]) / 100.0
                except ValueError:
                    continue
            if observed <= as_of:
                candidates.append((observed, value))
        if not candidates:
            return False
        observed, value = max(candidates, key=lambda item: item[0])
        md.risk_free = value
        md.sources["risk_free"] = "RBA F2"
        md.sources["risk_free_as_of"] = observed.isoformat()
        md.canonical_inputs["risk_free"] = canonicalize(
            value, currency=None, units="decimal_rate", source="RBA:F2 Australian Government 10-year bond",
            source_type="official_macro_feed", confidence="high", as_of_date=observed)
        return True
    except Exception as exc:  # pragma: no cover - provider/network dependent
        md.notes.append(f"RBA risk-free fallback failed: {type(exc).__name__}.")
        return False


def _erp(md: MarketData, as_of: date) -> None:
    """Load Damodaran's free mature-market ERP for current valuations."""
    live = False
    if as_of >= date.today() - timedelta(days=31):
        try:
            from io import BytesIO

            import pandas as pd
            import requests

            response = requests.get("https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctryprem.xlsx", timeout=20)
            response.raise_for_status()
            frame = pd.read_excel(BytesIO(response.content))
            country = next((column for column in frame if "country" in str(column).lower()), None)
            erp_col = next((column for column in frame if "total equity risk premium" in str(column).lower()), None)
            if country and erp_col:
                row = frame[frame[country].astype(str).str.contains("mature market", case=False, na=False)]
                if not row.empty:
                    value = float(row.iloc[0][erp_col])
                    md.erp = value / 100.0 if value > 1 else value
                    live = 0 < md.erp < 0.25
        except Exception as exc:  # pragma: no cover - network/provider format
            md.notes.append(f"Damodaran ERP fetch failed: {type(exc).__name__}.")
    if not live:
        md.erp = _ERP_FALLBACK
        md.notes.append("ERP uses 4.5% mature-market fallback; no point-in-time free ERP was available.")
    md.sources["erp"] = "Damodaran" if live else "Damodaran fallback"
    md.canonical_inputs["erp"] = canonicalize(
        md.erp, currency=None, units="decimal_rate", source="Damodaran mature-market ERP",
        source_type="reference_dataset", confidence="high" if live else "medium", as_of_date=as_of,
        is_estimated=not live)


def _ff_factors(md: MarketData) -> None:
    try:
        from io import BytesIO, StringIO
        from zipfile import ZipFile

        import numpy as np
        import pandas as pd
        import pandas_datareader.data as web
        import requests

        ff5 = web.DataReader("F-F_Research_Data_5_Factors_2x3_daily", "famafrench")[0]
        # pandas-datareader currently mis-parses the daily momentum archive.
        # Read the same official Ken French ZIP directly and keep only YYYYMMDD rows.
        response = requests.get(
            "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
            "F-F_Momentum_Factor_daily_CSV.zip",
            timeout=20,
        )
        response.raise_for_status()
        with ZipFile(BytesIO(response.content)) as archive:
            raw = archive.read(archive.namelist()[0]).decode("utf-8", errors="replace")
        lines = raw.splitlines()
        header = next(i for i, line in enumerate(lines) if line.replace(" ", "").startswith(",Mom"))
        data_lines = [lines[header]] + [
            line for line in lines[header + 1:]
            if line.split(",", 1)[0].strip().isdigit() and len(line.split(",", 1)[0].strip()) == 8
        ]
        mom = pd.read_csv(StringIO("\n".join(data_lines)), index_col=0)
        mom.index = pd.to_datetime(mom.index.astype(str), format="%Y%m%d")
        mom.columns = [column.strip() for column in mom.columns]
        mom = mom[["Mom"]]
        ff5.index = ff5.index.to_timestamp() if isinstance(ff5.index, pd.PeriodIndex) else pd.to_datetime(ff5.index)
        ff5.index = pd.to_datetime(ff5.index).as_unit("ns").normalize()
        mom.index = pd.to_datetime(mom.index).as_unit("ns").normalize()
        factors = ff5.join(mom, how="left") / 100.0
        stock = getattr(md, "_returns", None)
        if stock is not None and len(stock):
            stock_cutoff = pd.to_datetime(stock.index).max().tz_localize(None).normalize()
            factors = factors[factors.index <= stock_cutoff]
        row = factors.iloc[-1]
        md.ff_factors = {k: float(v) for k, v in row.items()}
        md.sources["ff_factors"] = True
        if stock is not None and len(stock) >= 60:
            stock = stock.copy()
            stock.index = pd.to_datetime(stock.index).as_unit("ns").normalize()
            joined = stock.rename("stock").to_frame().join(factors, how="inner").dropna()
            model_cols = {
                "ff3": ["Mkt-RF", "SMB", "HML"],
                "ff5": ["Mkt-RF", "SMB", "HML", "RMW", "CMA"],
                "carhart4": ["Mkt-RF", "SMB", "HML", "Mom"],
            }
            models = {}
            for name, cols in model_cols.items() if len(joined) >= 60 else ():
                if not all(c in joined.columns for c in cols):
                    continue
                y = joined["stock"].to_numpy() - joined["RF"].to_numpy()
                x = joined[cols].to_numpy()
                design = np.column_stack([np.ones(len(x)), x])
                coef, *_ = np.linalg.lstsq(design, y, rcond=None)
                fitted = design @ coef
                ss_res = float(np.sum((y - fitted) ** 2))
                ss_tot = float(np.sum((y - y.mean()) ** 2))
                expected_daily = float(joined["RF"].mean() + np.dot(coef[1:], joined[cols].mean()))
                models[name] = {
                    "alpha_annual": float(coef[0] * 252),
                    "loadings": {c: float(v) for c, v in zip(cols, coef[1:], strict=True)},
                    "expected_return": float((1 + expected_daily) ** 252 - 1),
                    "r_squared": 1 - ss_res / ss_tot if ss_tot else None,
                    "observations": int(len(joined)),
                }
            md.factor_models = models or None
            if models:
                md.sources["factor_regressions"] = True
    except Exception as exc:
        md.sources["ff_factors"] = False
        md.notes.append(f"Fama-French factors unavailable: {type(exc).__name__}: {str(exc)[:120]}.")


def _macro_factors(md: MarketData, as_of: date) -> None:
    """Fit an APT-style regression using point-in-time FRED macro series."""
    stock = getattr(md, "_returns", None)
    if stock is None or len(stock) < 250 or not settings.fred_api_key:
        md.sources["macro_factors"] = False
        return
    try:
        import numpy as np
        import pandas as pd
        import requests

        series_ids = {"oil": "DCOILWTICO", "inflation": "CPIAUCSL", "production": "INDPRO",
                      "term_spread": "T10Y2Y"}
        frame = pd.DataFrame()
        start = as_of - timedelta(days=3000)
        for name, series_id in series_ids.items():
            response = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": series_id, "api_key": settings.fred_api_key, "file_type": "json",
                        "observation_start": start.isoformat(), "observation_end": as_of.isoformat()}, timeout=15)
            if not response.ok:
                continue
            values = {row["date"]: float(row["value"]) for row in response.json().get("observations", [])
                      if row.get("value") not in (None, "", ".")}
            if values:
                frame[name] = pd.Series(values, dtype=float)
        if len(frame.columns) < 3:
            return
        frame.index = pd.to_datetime(frame.index)
        monthly = frame.resample("ME").last().ffill()
        for name in ("oil", "inflation", "production"):
            if name in monthly:
                monthly[name] = monthly[name].pct_change(fill_method=None)
        if "term_spread" in monthly:
            monthly["term_spread"] = monthly["term_spread"] / 100.0
        stock_monthly = stock.copy()
        stock_monthly.index = pd.to_datetime(stock_monthly.index).tz_localize(None)
        stock_monthly = stock_monthly.resample("ME").sum().rename("stock")
        joined = stock_monthly.to_frame().join(monthly, how="inner").dropna()
        if len(joined) < 36:
            return
        columns = list(monthly.columns)
        design = np.column_stack([np.ones(len(joined)), joined[columns].to_numpy()])
        y = joined["stock"].to_numpy()
        coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
        fitted = design @ coefficients
        residual = float(np.sum((y - fitted) ** 2))
        total = float(np.sum((y - y.mean()) ** 2))
        expected_monthly = float(coefficients[0] + np.dot(coefficients[1:], joined[columns].mean()))
        md.macro_model = {
            "expected_return": float((1 + expected_monthly) ** 12 - 1),
            "alpha_annual": float(coefficients[0] * 12),
            "loadings": {name: float(value) for name, value in zip(columns, coefficients[1:], strict=True)},
            "r_squared": 1 - residual / total if total else None,
            "observations": int(len(joined)), "as_of": as_of.isoformat(),
        }
        md.sources["macro_factors"] = "FRED point-in-time monthly regression"
    except Exception as exc:  # pragma: no cover - provider/network dependent
        md.sources["macro_factors"] = False
        md.notes.append(f"Macro-factor regression unavailable: {type(exc).__name__}.")


# Static fallback FX (units of target per 1 unit of source) used only when the
# live rate can't be fetched; always flagged in notes by the caller.
_FX_FALLBACK = {("USD", "AUD"): 1.52, ("AUD", "USD"): 0.66, ("GBP", "AUD"): 1.95,
                ("EUR", "AUD"): 1.63, ("NZD", "AUD"): 0.92, ("CAD", "AUD"): 1.12}


def fx_rate(from_ccy: str, to_ccy: str, as_of: date) -> tuple[float | None, bool]:
    """Return (rate, is_live) to convert 1 ``from_ccy`` into ``to_ccy``.

    Uses Yahoo's ``{FROM}{TO}=X`` daily close on/just before ``as_of``; if that
    fails it inverts ``{TO}{FROM}=X``; if that also fails it uses a static
    fallback (``is_live=False``). Same-currency returns ``(1.0, True)``.
    """
    if not from_ccy or not to_ccy or from_ccy == to_ccy:
        return 1.0, True
    try:
        from datetime import timedelta

        import yfinance as yf
        start = (as_of - timedelta(days=10)).isoformat()
        end = (as_of + timedelta(days=3)).isoformat()
        for sym, invert in ((f"{from_ccy}{to_ccy}=X", False), (f"{to_ccy}{from_ccy}=X", True)):
            h = yf.download(sym, start=start, end=end, progress=False, auto_adjust=True)
            if h is None or h.empty:
                continue
            close = h["Close"]
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
            close = close.dropna()
            upto = close[close.index.date <= as_of]
            val = float((upto if not upto.empty else close).iloc[-1])
            if val > 0:
                return (1.0 / val if invert else val), True
    except Exception:
        pass
    fb = _FX_FALLBACK.get((from_ccy, to_ccy))
    if fb is None and (to_ccy, from_ccy) in _FX_FALLBACK:
        fb = 1.0 / _FX_FALLBACK[(to_ccy, from_ccy)]
    return fb, False


def market_snapshot(market: str, ticker: str, as_of: date) -> MarketData:
    cached = _load_cache(market, ticker, as_of)
    if cached is not None:
        return cached
    symbol, index, ccy = parse_symbol(market, ticker)
    md = MarketData(symbol=symbol, index_symbol=index, currency=ccy)
    _price_and_stats(md, as_of)
    _stooq_fallback(md, market.upper().strip(), ticker.upper().strip(), as_of)
    _eodhd_fallback(md, market.upper().strip(), ticker.upper().strip(), as_of)
    _shares_and_targets(md, as_of)
    _risk_free(md, as_of)
    _erp(md, as_of)
    _ff_factors(md)
    _macro_factors(md, as_of)
    _canonicalize_outputs(md, as_of)
    # Private working data should never leak into API responses.
    if hasattr(md, "_returns"):
        del md._returns
    _save_cache(market, ticker, as_of, md)
    return md


def _canonicalize_outputs(md: MarketData, requested_as_of: date) -> None:
    """Expose every calculation feed through the same audit schema."""
    definitions = {
        "beta": (md.beta, None, "ratio", "Blume-adjusted Yahoo five-year weekly beta",
                 "derived_market", "high", False),
        "beta_raw": (md.beta_raw, None, "ratio", "Yahoo five-year weekly regression",
                     "derived_market", "high", False),
        "hist_vol": (md.hist_vol, None, "decimal_rate", "Yahoo/Stooq history", "derived_market", "high", False),
        "target_mean": (md.target_mean, md.currency, "currency_per_share", "Finnhub/Yahoo consensus",
                        "analyst_feed", "medium", False),
        "forward_eps": (md.forward_eps, md.currency, "currency_per_share", "Yahoo estimates",
                        "analyst_feed", "medium", False),
        "ff_factors": (md.ff_factors, None, "daily_factor_returns", "Ken French Data Library",
                       "factor_dataset", "high", False),
        "factor_models": (md.factor_models, None, "regression_results", "Ken French + price regression",
                          "derived_factor", "high", False),
        "macro_model": (md.macro_model, None, "regression_results", "FRED macro + price regression",
                        "derived_factor", "high", False),
    }
    for key, (value, currency, units, source, source_type, confidence, estimated) in definitions.items():
        if value is not None and key not in md.canonical_inputs:
            md.canonical_inputs[key] = canonicalize(
                value, currency=currency, units=units, source=source, source_type=source_type,
                confidence=confidence, as_of_date=md.as_of or requested_as_of, is_estimated=estimated)


def _cache_path(market: str, ticker: str, as_of: date) -> Path:
    safe = f"{market.upper()}_{ticker.upper()}_{as_of.isoformat()}".replace("/", "_")
    return _CACHE_DIR / f"v{_CACHE_VERSION}_{safe}.json"


def _load_cache(market: str, ticker: str, as_of: date) -> MarketData | None:
    path = _cache_path(market, ticker, as_of)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        if payload.get("price") is None and payload.get("risk_free") is None:
            return None
        allowed = MarketData.__dataclass_fields__.keys()
        return MarketData(**{key: value for key, value in payload.items() if key in allowed})
    except (OSError, ValueError, TypeError):
        return None


def _save_cache(market: str, ticker: str, as_of: date, md: MarketData) -> None:
    if md.price is None and md.risk_free is None:
        return
    try:
        _cache_path(market, ticker, as_of).write_text(json.dumps(md.to_dict(), indent=2, sort_keys=True))
    except OSError:
        md.notes.append("Market cache write failed.")
