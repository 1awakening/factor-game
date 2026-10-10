"""Deterministic fake market for offline tests. Same interface as YahooProvider."""
from __future__ import annotations

import zlib

import numpy as np
import pandas as pd

SECTORS = ["Technology", "Healthcare", "Industrials", "Energy", "Basic Materials", "Consumer Cyclical",
           "Consumer Defensive", "Communication Services", "Financial Services", "Utilities", "Real Estate"]
DATES = pd.bdate_range("2024-01-01", "2028-12-29")


def _rng(key: str, salt: int = 0) -> np.random.Generator:
    return np.random.default_rng(zlib.crc32(key.encode()) + salt)


class SyntheticProvider:
    def __init__(self, as_of: str, n_us: int = 120, n_ca: int = 60, fail_rate: float = 0.03,
                 dead: dict | None = None):
        self.as_of = pd.Timestamp(as_of)
        self.fail_rate = fail_rate
        self.us = [f"U{i:03d}" for i in range(n_us)]
        self.ca = [f"C{i:03d}.TO" for i in range(n_ca)]
        # tickers that stop trading after a date
        self.dead = dead if dead is not None else {"U007": "2026-12-01"}
        self._px = self._make_prices(self.us + self.ca + ["SPY", "XIC.TO", "CAD=X"])

    # ------------------------------------------------------------ helpers
    def _make_prices(self, tickers) -> pd.DataFrame:
        cols = {}
        for t in tickers:
            r = _rng(t)
            if t == "CAD=X":
                steps = r.normal(0, 0.003, len(DATES))
                cols[t] = 1.38 * np.exp(np.cumsum(steps))
                continue
            mu, sig = r.normal(0.08, 0.10) / 252, r.uniform(0.15, 0.45) / np.sqrt(252)
            steps = r.normal(mu, sig, len(DATES))
            cols[t] = r.uniform(20, 300) * np.exp(np.cumsum(steps))
        df = pd.DataFrame(cols, index=DATES)
        for t, d in self.dead.items():
            if t in df:
                df.loc[df.index > pd.Timestamp(d), t] = np.nan
        return df

    def _sector(self, t: str) -> str:
        return SECTORS[zlib.crc32(t.encode()) % len(SECTORS)]

    # ------------------------------------------------------------ interface
    def universe(self, market: str):
        ts = self.us if market == "US" else self.ca
        df = pd.DataFrame({"ticker": ts, "name": [f"Company {t}" for t in ts],
                           "sector_hint": [self._sector(t) for t in ts]})
        return df, ("Synthetic US" if market == "US" else "Synthetic CA")

    def prices(self, tickers, start, end) -> pd.DataFrame:
        cols = [t for t in tickers if t in self._px.columns]
        end = min(pd.Timestamp(end), self.as_of)
        return self._px.loc[(self._px.index >= pd.Timestamp(start)) & (self._px.index <= end), cols]

    def fx_usdcad(self, start, end) -> pd.Series:
        s = self.prices(["CAD=X"], start, end)["CAD=X"]
        self._usdcad = float(s.dropna().iloc[-1])
        return s

    def fx_rate(self, frm, to) -> float:
        if not frm or frm == to:
            return 1.0
        if (frm, to) == ("USD", "CAD"):
            return self._usdcad
        if (frm, to) == ("CAD", "USD"):
            return 1 / self._usdcad
        return float("nan")

    def fundamentals(self, ticker, market, sector_hint=None, name_hint=None, last_price=None) -> dict:
        # failures vary by run date, like real network glitches
        r_fail = _rng(ticker, salt=int(self.as_of.strftime("%Y%m%d")))
        r = _rng(ticker, salt=1)
        currency = "USD" if market == "US" else "CAD"
        fin = currency
        if market == "CA" and r.random() < 0.15:
            fin = "USD"
        rec = {"ticker": ticker, "market": market, "name": name_hint or f"Company {ticker}",
               "sector": self._sector(ticker), "currency": currency, "fin_currency": fin,
               "fin_currency_assumed": False, "annual": {}, "quarterly": {}, "fetch_ok": True, "error": None}
        if r_fail.random() < self.fail_rate:
            rec.update(fetch_ok=False, error="synthetic download failure", market_cap=float("nan"))
            rec["fx_fin_to_trade"] = self.fx_rate(fin, currency)
            return rec

        g = r.normal(0.06, 0.12)                       # revenue growth
        if r.random() < 0.08:
            g = r.uniform(0.4, 1.0)                    # a few aggressive growers
        rev0 = float(np.exp(r.normal(np.log(8e9), 1.0)))
        gm = r.uniform(0.2, 0.7)
        om = r.normal(0.12, 0.08)
        at = r.uniform(0.4, 1.4)                        # asset turnover
        lev = r.uniform(0.0, 0.6)
        years = 5
        rev = [rev0 / (1 + g) ** i * np.exp(r.normal(0, 0.02)) for i in range(years)]
        ta = [v / at * np.exp(r.normal(0, 0.03)) for v in rev]
        if r.random() < 0.06:
            ta[0] *= r.uniform(1.5, 2.5)               # acquisition spike -> asset growth / distress
        a = {
            "revenue": rev,
            "cogs": [v * (1 - gm) for v in rev],
            "gross_profit": [v * gm for v in rev],
            "ebit": [v * (om + r.normal(0, 0.02)) for v in rev],
            "net_income": [v * (om * 0.7 + r.normal(0, 0.03)) for v in rev],
            "sga": [v * (gm - om) * 0.8 for v in rev],
            "dep": [v * 0.04 for v in rev],
            "total_assets": ta,
            "current_assets": [x * 0.35 for x in ta],
            "current_liab": [x * r.uniform(0.15, 0.4) for x in ta],
            "receivables": [v * 0.12 * np.exp(r.normal(0, 0.1)) for v in rev],
            "ppe": [x * 0.3 for x in ta],
            "total_debt": [x * lev for x in ta],
            "lt_debt": [x * lev * 0.8 for x in ta],
            "cash": [x * 0.08 for x in ta],
            "retained_earnings": [x * r.uniform(-0.2, 0.5) for x in ta],
            "total_liab": [x * (lev + 0.25) for x in ta],
            "shares": [1e9 * (1 + r.normal(0, 0.01)) ** i for i in range(years)],
            "cfo": [v * (om + 0.04 + r.normal(0, 0.03)) for v in rev],
            "capex": [-v * 0.05 for v in rev],
            "dividends_paid": [-v * max(om, 0) * 0.3 for v in rev],
            "repurchase": [-v * r.uniform(0, 0.05) for v in rev],
            "stock_issuance": [v * r.uniform(0, 0.01) for v in rev],
            "debt_repayment": [-v * 0.03 for v in rev],
            "debt_issuance": [v * r.uniform(0, 0.05) for v in rev],
        }
        a["ebitda"] = [e + d for e, d in zip(a["ebit"], a["dep"])]
        if r.random() < 0.05:                          # accrual-heavy reporter -> high M-score
            a["cfo"][0] = a["net_income"][0] - 0.3 * ta[0]
        q = {}
        flow = {"revenue", "cogs", "gross_profit", "ebit", "ebitda", "net_income", "sga", "dep", "cfo", "capex",
                "dividends_paid", "repurchase", "stock_issuance", "debt_repayment", "debt_issuance"}
        for k, v in a.items():
            q[k] = ([v[0] / 4 * np.exp(r.normal(0, 0.03)) for _ in range(5)] if k in flow else [v[0]] * 5)
        if r.random() < 0.1:
            q = {}                                     # some have no quarterly data -> annual fallback
        to_fin = self.fx_rate(currency, fin)
        if fin != currency:                            # statements reported in USD
            a = {k: [x * to_fin for x in v] for k, v in a.items()}
            q = {k: [x * to_fin for x in v] for k, v in q.items()}
        price = last_price or 100.0
        ev_mult = np.exp(r.normal(np.log(12), 0.5))
        ebitda_trade = a["ebitda"][0] / to_fin
        mcap = max(ebitda_trade * ev_mult - (a["total_debt"][0] - a["cash"][0]) / to_fin, 5e8)
        if r.random() < 0.05:
            mcap = r.uniform(2e8, 9e8)                 # below the size floor
        rec.update(annual=a, quarterly=q, market_cap=float(mcap))
        rec["fx_fin_to_trade"] = self.fx_rate(fin, currency)
        _ = price
        return rec
