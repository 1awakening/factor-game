"""Live data: Yahoo Finance (yfinance) for prices and statements, Bank of Canada for USD/CAD."""
from __future__ import annotations

import logging
import math
import time

import numpy as np
import pandas as pd
import requests

from . import universe

log = logging.getLogger("factor-game")

INCOME_ROWS = {
    "revenue": ["Total Revenue", "Operating Revenue"],
    "cogs": ["Cost Of Revenue", "Reconciled Cost Of Revenue"],
    "gross_profit": ["Gross Profit"],
    "operating_income": ["Operating Income", "Total Operating Income As Reported"],
    "ebit": ["EBIT"],
    "ebitda": ["EBITDA", "Normalized EBITDA"],
    "net_income": ["Net Income Continuous Operations", "Net Income From Continuing Operation Net Minority Interest",
                   "Net Income", "Net Income Common Stockholders"],
    "sga": ["Selling General And Administration"],
    "dep": ["Reconciled Depreciation", "Depreciation And Amortization In Income Statement",
            "Depreciation Amortization Depletion Income Statement"],
}
BALANCE_ROWS = {
    "total_assets": ["Total Assets"],
    "current_assets": ["Current Assets"],
    "current_liab": ["Current Liabilities"],
    "receivables": ["Accounts Receivable", "Receivables", "Gross Accounts Receivable"],
    "inventory": ["Inventory"],
    "ppe": ["Net PPE"],
    "securities": ["Other Short Term Investments", "Available For Sale Securities"],
    "total_debt": ["Total Debt"],
    "lt_debt": ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"],
    "cash": ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
    "retained_earnings": ["Retained Earnings"],
    "total_liab": ["Total Liabilities Net Minority Interest"],
    "working_capital": ["Working Capital"],
    "shares": ["Ordinary Shares Number", "Share Issued"],
}
CASHFLOW_ROWS = {
    "cfo": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
    "capex": ["Capital Expenditure"],
    "fcf": ["Free Cash Flow"],
    "dividends_paid": ["Cash Dividends Paid", "Common Stock Dividend Paid"],
    "repurchase": ["Repurchase Of Capital Stock", "Common Stock Payments"],
    "stock_issuance": ["Issuance Of Capital Stock", "Common Stock Issuance"],
    "net_stock_issuance": ["Net Common Stock Issuance"],
    "debt_repayment": ["Repayment Of Debt"],
    "debt_issuance": ["Issuance Of Debt"],
    "net_debt_issuance": ["Net Issuance Payments Of Debt"],
}
# Depreciation for the M-score is taken from the cash-flow statement when the income
# statement lacks it.
CF_DEP = ["Depreciation And Amortization", "Depreciation Amortization Depletion"]

UA = universe.UA


def _yf():
    import yfinance as yf  # imported lazily so offline tests don't need it
    return yf


def _is_rate_limit(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}"
    return "RateLimit" in s or "Too Many Requests" in s or "429" in s


def _extract(df, rows: dict, n: int = 5) -> dict:
    out = {}
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return out
    try:
        cols = sorted(df.columns, key=lambda c: pd.Timestamp(c), reverse=True)
        df = df[cols]
    except Exception:
        pass
    for key, labels in rows.items():
        for lab in labels:
            if lab in df.index:
                vals = pd.to_numeric(df.loc[lab], errors="coerce")
                if isinstance(vals, pd.DataFrame):
                    vals = vals.iloc[0]
                if vals.notna().any():
                    out[key] = [float(v) if pd.notna(v) else float("nan") for v in vals.tolist()[:n]]
                    break
    return out


class YahooProvider:
    def __init__(self, pause: float = 0.35):
        self.pause = pause
        self._fx_cache: dict = {}
        self._usdcad: pd.Series | None = None
        yf = _yf()
        try:
            yf.set_tz_cache_location("/tmp/yf-tz-cache")
        except Exception:
            pass

    # ------------------------------------------------------------ universe
    def universe(self, market: str) -> tuple[pd.DataFrame, str]:
        if market == "US":
            return universe.sp500(), "S&P 500"
        return universe.tsx()

    # ------------------------------------------------------------ FX
    def fx_usdcad(self, start: str, end: str) -> pd.Series:
        url = ("https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
               f"?start_date={start}&end_date={end}")
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            obs = r.json().get("observations", [])
            s = pd.Series({pd.Timestamp(o["d"]): float(o["FXUSDCAD"]["v"])
                           for o in obs if "FXUSDCAD" in o}).sort_index()
            if len(s):
                self._usdcad = s
                return s
        except Exception as e:
            log.warning("Bank of Canada FX failed (%s); falling back to Yahoo CAD=X", e)
        px = self.prices(["CAD=X"], start, end)
        s = px["CAD=X"].dropna() if "CAD=X" in px else pd.Series(dtype=float)
        self._usdcad = s
        return s

    def fx_rate(self, frm: str, to: str) -> float:
        """Latest rate converting 1 unit of `frm` into `to`."""
        if not frm or not to or frm == to:
            return 1.0
        if (frm, to) in self._fx_cache:
            return self._fx_cache[(frm, to)]
        rate = float("nan")
        if {frm, to} == {"USD", "CAD"} and self._usdcad is not None and len(self._usdcad):
            usdcad = float(self._usdcad.iloc[-1])
            rate = usdcad if frm == "USD" else 1.0 / usdcad
        else:
            sym = f"{frm}{to}=X"
            end = pd.Timestamp.today().strftime("%Y-%m-%d")
            start = (pd.Timestamp.today() - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
            px = self.prices([sym], start, end)
            if sym in px and px[sym].dropna().size:
                rate = float(px[sym].dropna().iloc[-1])
        self._fx_cache[(frm, to)] = rate
        return rate

    # ------------------------------------------------------------ prices
    def prices(self, tickers, start: str, end: str) -> pd.DataFrame:
        yf = _yf()
        tickers = sorted(set(tickers))
        end_excl = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        frames = []
        for i in range(0, len(tickers), 80):
            chunk = tickers[i:i + 80]
            df = None
            for attempt in range(4):
                try:
                    df = yf.download(chunk, start=start, end=end_excl, auto_adjust=True,
                                     progress=False, threads=True, group_by="column")
                    break
                except Exception as e:
                    wait = 60 if _is_rate_limit(e) else 5 * (attempt + 1)
                    log.warning("price download failed (%s); retrying in %ss", e, wait)
                    time.sleep(wait)
            if df is None or df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                lvl0 = df.columns.get_level_values(0)
                close = df["Close"] if "Close" in lvl0 else df.xs("Close", axis=1, level=1)
            else:
                close = df[["Close"]].rename(columns={"Close": chunk[0]})
            frames.append(close)
            time.sleep(1.0)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, axis=1)
        out = out.loc[:, ~out.columns.duplicated()]
        idx = pd.to_datetime(out.index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        out.index = idx.normalize()
        return out.sort_index()

    # ------------------------------------------------------------ fundamentals
    def fundamentals(self, ticker: str, market: str, sector_hint=None, name_hint=None,
                     last_price=None) -> dict:
        yf = _yf()
        rec = {"ticker": ticker, "market": market, "name": name_hint, "sector": sector_hint,
               "currency": "USD" if market == "US" else "CAD", "fin_currency": None,
               "fin_currency_assumed": False, "market_cap": float("nan"),
               "annual": {}, "quarterly": {}, "fetch_ok": False, "error": None}
        last_err = None
        for attempt in range(4):
            try:
                t = yf.Ticker(ticker)
                info = {}
                try:
                    info = t.info or {}
                except Exception as e:  # info is the most fragile endpoint; statements may still work
                    if _is_rate_limit(e):
                        raise
                    last_err = f"info: {e}"
                a, q = {}, {}
                for rows, a_df, q_df in (
                    (INCOME_ROWS, t.income_stmt, t.quarterly_income_stmt),
                    (BALANCE_ROWS, t.balance_sheet, t.quarterly_balance_sheet),
                    (CASHFLOW_ROWS, t.cashflow, t.quarterly_cashflow),
                ):
                    a.update(_extract(a_df, rows))
                    q.update(_extract(q_df, rows))
                    if rows is CASHFLOW_ROWS:
                        if "dep" not in a:
                            a.update({k: v for k, v in _extract(a_df, {"dep": CF_DEP}).items()})
                        if "dep" not in q:
                            q.update({k: v for k, v in _extract(q_df, {"dep": CF_DEP}).items()})
                if not a and not q:
                    raise RuntimeError("no statements returned")
                rec["annual"], rec["quarterly"] = a, q
                rec["name"] = info.get("longName") or info.get("shortName") or name_hint
                ysec = info.get("sector")
                rec["sector"] = ysec or sector_hint
                if info.get("currency"):
                    rec["currency"] = info["currency"]
                fin = info.get("financialCurrency")
                if fin:
                    rec["fin_currency"] = fin
                else:
                    rec["fin_currency"] = rec["currency"]
                    rec["fin_currency_assumed"] = True
                mcap = info.get("marketCap")
                if not mcap:
                    sh = (q.get("shares") or a.get("shares") or [float("nan")])[0]
                    if last_price and sh and math.isfinite(sh):
                        mcap = float(last_price) * float(sh)
                rec["market_cap"] = float(mcap) if mcap else float("nan")
                rec["fetch_ok"] = True
                rec["error"] = last_err
                break
            except Exception as e:
                last_err = str(e)
                wait = 60 if _is_rate_limit(e) else 3 * (attempt + 1)
                log.warning("%s: fundamentals failed (%s); retry in %ss", ticker, e, wait)
                time.sleep(wait)
        if not rec["fetch_ok"]:
            rec["error"] = last_err
        rec["fx_fin_to_trade"] = self.fx_rate(rec["fin_currency"] or rec["currency"], rec["currency"])
        time.sleep(self.pause)
        return rec
