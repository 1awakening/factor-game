"""Portfolio state, marking to market and trade execution. All values in CAD."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from . import config as C
from .util import clean


def _ts(d) -> pd.Timestamp:
    return pd.Timestamp(d).normalize()


def _asof(series: pd.Series, d) -> float:
    s = series.dropna()
    if s.empty:
        return float("nan")
    d = _ts(d)
    s = s[s.index <= d]
    return float(s.iloc[-1]) if not s.empty else float("nan")


def trade_cost(value_cad: float, market: str) -> float:
    rate = C.TRADE_COST + (C.FX_COST_US if market == "US" else 0.0)
    return abs(value_cad) * rate


class Portfolio:
    def __init__(self, data: dict):
        self.d = data

    # ------------------------------------------------------------ persistence
    @classmethod
    def new(cls, as_of: str) -> "Portfolio":
        return cls({
            "rules_version": C.RULES_VERSION,
            "inception_date": as_of,
            "start_capital_cad": C.START_CAPITAL_CAD,
            "cash_cad": C.START_CAPITAL_CAD,
            "positions": [],
            "benchmark": {
                "mark_date": as_of,
                "sleeves": {t: C.START_CAPITAL_CAD * w for t, w in C.BENCHMARK.items()},
            },
            "cutoffs": {},
            "last_rebalance": None,
            "last_run": None,
            "total_costs_cad": 0.0,
        })

    @classmethod
    def load(cls, path: Path) -> "Portfolio | None":
        return cls(json.loads(path.read_text())) if path.exists() else None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(clean(self.d), indent=2, sort_keys=False, default=str) + "\n")

    # ------------------------------------------------------------ accessors
    @property
    def positions(self) -> list[dict]:
        return self.d["positions"]

    @property
    def cash(self) -> float:
        return self.d["cash_cad"]

    def nav(self) -> float:
        return self.d["cash_cad"] + sum(p["value_cad"] for p in self.positions)

    def bench_value(self) -> float:
        return sum(self.d["benchmark"]["sleeves"].values())

    def held(self, market: str | None = None) -> list[dict]:
        return [p for p in self.positions if market is None or p["market"] == market]

    # ------------------------------------------------------------ marking
    def mark(self, prices: pd.DataFrame, fx: pd.Series, as_of: str) -> list[str]:
        """Revalue positions and benchmark from mark_date to as_of. Returns warnings."""
        warns = []
        fx_now = _asof(fx, as_of)
        for p in self.positions:
            t = p["ticker"]
            if t not in prices.columns or prices[t].dropna().empty:
                p["stale"] = True
                warns.append(f"{t}: no price data; value held at last mark")
                continue
            s = prices[t].dropna()
            p_then, p_now = _asof(s, p["mark_date"]), float(s.iloc[-1])
            if not (math.isfinite(p_then) and p_then > 0):
                p_then = float(s.iloc[0])
            factor = p_now / p_then
            if p["market"] == "US":
                fx_then = _asof(fx, p["mark_date"])
                factor *= fx_now / fx_then
            p["value_cad"] *= factor
            p["tr_index"] = p.get("tr_index", 1.0) * factor  # CAD total return since entry
            p["mark_date"] = as_of
            p["last_price_local"] = p_now
            p["last_price_date"] = s.index[-1].strftime("%Y-%m-%d")
            lag = len(pd.bdate_range(s.index[-1], _ts(as_of))) - 1
            p["stale"] = lag >= C.STALE_PRICE_TRADING_DAYS
            if p["stale"]:
                warns.append(f"{t}: last price {p['last_price_date']} ({lag} trading days old)")

        b = self.d["benchmark"]
        for t, v in list(b["sleeves"].items()):
            if t not in prices.columns or prices[t].dropna().empty:
                warns.append(f"benchmark {t}: no price data")
                continue
            s = prices[t].dropna()
            p_then = _asof(s, b["mark_date"])
            factor = float(s.iloc[-1]) / p_then if p_then > 0 else 1.0
            if C.BENCHMARK_MARKET[t] == "US":
                factor *= fx_now / _asof(fx, b["mark_date"])
            b["sleeves"][t] = v * factor
        total = sum(b["sleeves"].values())
        b["sleeves"] = {t: total * w for t, w in C.BENCHMARK.items()}  # monthly rebalance
        b["mark_date"] = as_of
        return warns

    # ------------------------------------------------------------ trading
    def sell(self, p: dict, reason: str, ctx: dict) -> dict:
        value = p["value_cad"]
        cost = trade_cost(value, p["market"])
        self.d["cash_cad"] += value - cost
        self.d["total_costs_cad"] += cost
        self.d["positions"] = [q for q in self.positions if q is not p]
        return self._trade(ctx, p, "SELL", value, cost, reason)

    def buy(self, row: dict, value: float, reason: str, ctx: dict) -> dict:
        cost = trade_cost(value, row["market"])
        self.d["cash_cad"] -= value + cost
        self.d["total_costs_cad"] += cost
        p = {
            "ticker": row["ticker"], "market": row["market"], "name": row.get("name"),
            "sector": row.get("sector"), "value_cad": value,
            "entry_date": ctx["date"], "entry_value_cad": value,
            "entry_composite_pct": row.get("composite_pct"),
            "composite_pct": row.get("composite_pct"),
            "mark_date": ctx["date"], "last_price_local": row.get("last_price"),
            "last_price_date": row.get("last_price_date"), "stale": False,
            "tr_index": 1.0,
        }
        self.positions.append(p)
        return self._trade(ctx, p, "BUY", value, cost, reason)

    def resize(self, p: dict, target: float, ctx: dict, force: bool = False) -> dict | None:
        delta = target - p["value_cad"]
        if delta == 0 or (not force and abs(delta) < C.REBALANCE_SKIP_FRACTION * target):
            return None
        cost = trade_cost(delta, p["market"])
        self.d["cash_cad"] -= delta + cost
        self.d["total_costs_cad"] += cost
        p["value_cad"] = target
        return self._trade(ctx, p, "ADD" if delta > 0 else "TRIM", abs(delta), cost, "equal-weight reset")

    def _trade(self, ctx, p, action, value, cost, reason) -> dict:
        return {
            "date": ctx["date"], "mode": ctx["mode"], "ticker": p["ticker"], "market": p["market"],
            "name": p.get("name"), "sector": p.get("sector"), "action": action,
            "value_cad": round(value, 2), "cost_cad": round(cost, 2),
            "composite_pct": p.get("composite_pct"), "reason": reason,
        }

    def equal_weight(self, ctx: dict, new_buys: list[dict]) -> list[dict]:
        """Buy new_buys and reset all positions to NAV/TOTAL_SLOTS, net of costs."""
        if not self.positions and not new_buys:
            return []
        nav = self.nav()
        target = nav / C.TOTAL_SLOTS
        # Solve for the target so that, after costs, every resized/new position is
        # exactly `target`, skipped positions keep their value, and the cash left over
        # equals one target per unfilled slot. A few fixed-point iterations converge.
        def solve(skip_ids, target):
            for _ in range(20):
                costs = sum(trade_cost(target, r["market"]) for r in new_buys)
                skipped_value = 0.0
                for p in self.positions:
                    if id(p) in skip_ids:
                        skipped_value += p["value_cad"]
                    else:
                        costs += trade_cost(target - p["value_cad"], p["market"])
                target = (nav - costs - skipped_value) / (C.TOTAL_SLOTS - len(skip_ids))
            return target

        # 1) find which positions are close enough to skip; 2) freeze that set and solve exactly.
        skip_ids: set = set()
        for _ in range(6):
            skip_ids = {id(p) for p in self.positions
                        if abs(target - p["value_cad"]) < C.REBALANCE_SKIP_FRACTION * target}
            target = solve(skip_ids, target)
        target = solve(skip_ids, target)

        trades = []
        # trims first (raise cash), then buys and adds
        for p in sorted(self.positions, key=lambda q: q["value_cad"] - target, reverse=True):
            if id(p) in skip_ids:
                continue
            t = self.resize(p, target, ctx, force=True)
            if t:
                trades.append(t)
        for r in new_buys:
            trades.append(self.buy(r, target, r["_reason"], ctx))
        return trades
