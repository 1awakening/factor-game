"""Offline tests on a synthetic market. Run: python -m tests.run_tests"""
from __future__ import annotations

import json
import math
import shutil
import tempfile
from pathlib import Path

import pandas as pd

from engine import config as C
from engine import metrics, run, scoring
from engine.synthetic import SyntheticProvider

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)


def load(root):
    return json.loads((root / "state" / "portfolio.json").read_text())


def nav_of(d):
    return d["cash_cad"] + sum(p["value_cad"] for p in d["positions"])


def use_provider(**kw):
    run.make_provider = lambda name, as_of: SyntheticProvider(as_of, n_us=500, n_ca=220, **kw)


def trades_on(root, date):
    t = pd.read_csv(root / "state" / "trades.csv")
    return t[t["date"] == date]


def test_metrics_unit():
    raw = {"market": "US", "currency": "USD", "fx_fin_to_trade": 1.0, "market_cap": 1000.0,
           "annual": {"revenue": [500, 400], "total_assets": [1000, 900], "net_income": [50, 40],
                      "cfo": [80, 60], "total_debt": [200], "cash": [100]},
           "quarterly": {"ebit": [20, 20, 20, 20], "ebitda": [30, 30, 30, 30], "cfo": [20, 20, 20, 20],
                         "capex": [-5, -5, -5, -5]},
           "fetch_ok": True}
    m = metrics.compute(raw, 1.4)
    check(abs(m["enterprise_value"] - 1100) < 1e-9, "EV = mcap + debt - cash")
    check(abs(m["ebit_ev"] - 80 / 1100) < 1e-12, "EBIT/EV uses TTM sum of 4 quarters")
    check(abs(m["fcf_ev"] - 60 / 1100) < 1e-12, "FCF = CFO - |capex|")
    check(abs(m["asset_growth"] - (1000 / 900 - 1)) < 1e-12, "asset growth from annual")
    check(abs(m["market_cap_cad"] - 1400) < 1e-9, "market cap converted to CAD")
    # currency mismatch: statements in USD for a CAD-traded stock
    raw2 = dict(raw, currency="CAD", market="CA", fx_fin_to_trade=1.4)
    m2 = metrics.compute(raw2, 1.4)
    check(abs(m2["enterprise_value"] - (1000 + 100 * 1.4)) < 1e-9, "EV converts statement debt/cash to trading ccy")
    check(abs(m2["ebit_ev"] - 80 * 1.4 / m2["enterprise_value"]) < 1e-12, "EBIT converted before dividing by EV")
    s = pd.Series(range(1, 300), dtype=float)
    mom = metrics.momentum(s, 252, 21)
    check(abs(mom - (s.iloc[-22] / s.iloc[-253] - 1)) < 1e-12, "12-1 momentum window")


def test_lifecycle():
    root = Path(tempfile.mkdtemp())
    use_provider()

    # ---------------- init
    check(run.main(["--mode", "auto", "--root", str(root), "--as-of", "2026-10-15"]) == 0, "init runs")
    d = load(root)
    check(d["inception_date"] == "2026-10-15", "inception date recorded")
    us = [p for p in d["positions"] if p["market"] == "US"]
    ca = [p for p in d["positions"] if p["market"] == "CA"]
    check(len(us) <= 20 and len(ca) <= 10, f"slot limits ({len(us)} US, {len(ca)} CA)")
    check(len(us) >= 15 and len(ca) >= 5, f"slots mostly filled ({len(us)} US, {len(ca)} CA)")
    for m, ps in (("US", us), ("CA", ca)):
        counts = pd.Series([p["sector"] for p in ps]).value_counts()
        check(counts.max() <= C.SECTOR_CAP[m], f"{m} sector cap respected ({counts.to_dict()})")
        check(not any(p["sector"] in C.EXCLUDED_SECTORS for p in ps), f"{m} no excluded sectors")
        check(all(p["entry_composite_pct"] >= C.BUY_MIN_PCTL for p in ps), f"{m} buys only from the top 15%")
    nav = nav_of(d)
    check(abs(nav + d["total_costs_cad"] - C.START_CAPITAL_CAD) < 0.01, "NAV + costs == starting capital")
    check(d["cash_cad"] > -0.01, f"cash not negative ({d['cash_cad']:.4f})")
    vals = [p["value_cad"] for p in d["positions"]]
    check(max(vals) - min(vals) < 0.01, "equal weights at init")
    target = vals[0]
    unfilled = C.TOTAL_SLOTS - len(vals)
    check(abs(d["cash_cad"] - unfilled * target) < 0.05, "cash = one target per unfilled slot")
    check(abs(sum(d["benchmark"]["sleeves"].values()) - C.START_CAPITAL_CAD) < 1e-6, "benchmark starts at capital")
    scores = pd.read_csv(root / "runs" / "2026-10-15" / "scores_US.csv")
    check(scores["excluded"].any() and (~scores["excluded"]).sum() > 100, "scores file written with exclusions")
    reasons = " ".join(scores["reasons"].dropna())
    for word in ("distress", "manipulation", "aggressive asset growth", "market cap under"):
        check(word in reasons, f"filter '{word}' fired at least once")

    # ---------------- idempotent auto rerun
    before = (root / "state" / "portfolio.json").read_text()
    check(run.main(["--mode", "auto", "--root", str(root), "--as-of", "2026-10-15"]) == 0, "rerun ok")
    check((root / "state" / "portfolio.json").read_text() == before, "same-day auto rerun changes nothing")

    # ---------------- monthly check with a holding that stops trading
    held_us = sorted(p["ticker"] for p in us)[0]
    use_provider(dead={held_us: "2026-11-20"})
    prov = run.make_provider("synthetic", "2026-12-15")
    d0 = load(root)
    check(run.main(["--mode", "auto", "--root", str(root), "--as-of", "2026-12-15"]) == 0, "December check runs")
    d1 = load(root)
    t = trades_on(root, "2026-12-15")
    check(set(t["action"]) <= {"SELL"}, "monthly check only sells")
    check(held_us in set(t["ticker"]), f"stale holding {held_us} sold")
    check(all(p["ticker"] != held_us for p in d1["positions"]), "stale holding removed")
    # mark-to-market arithmetic for one surviving US position
    px = prov.prices([p["ticker"] for p in d0["positions"]], "2026-10-01", "2026-12-15")
    fx = prov.fx_usdcad("2026-10-01", "2026-12-15")
    p0 = next(p for p in d0["positions"] if p["market"] == "US" and p["ticker"] != held_us
              and p["ticker"] not in set(t["ticker"]))
    p1 = next(p for p in d1["positions"] if p["ticker"] == p0["ticker"])
    s = px[p0["ticker"]].dropna()
    expect = p0["value_cad"] * s.iloc[-1] / s.asof(pd.Timestamp("2026-10-15")) * \
        fx.iloc[-1] / fx.asof(pd.Timestamp("2026-10-15"))
    check(abs(p1["value_cad"] - expect) < 1e-6, "US position marked by price ratio x FX ratio")
    check(abs(nav_of(d1) - (sum(p["value_cad"] for p in d1["positions"]) + d1["cash_cad"])) < 1e-9, "NAV identity")
    sold_value = t["value_cad"].sum()
    check(d1["cash_cad"] > d0["cash_cad"] + 0.99 * sold_value - 1, "sale proceeds added to cash")

    # ---------------- quarterly rebalance
    use_provider(dead={held_us: "2026-11-20"})
    check(run.main(["--mode", "auto", "--root", str(root), "--as-of", "2027-02-15"]) == 0, "February rebalance runs")
    d2 = load(root)
    t2 = trades_on(root, "2027-02-15")
    check(set(t2["mode"]) == {"rebalance"}, "auto picked rebalance in February")
    sc = pd.concat([pd.read_csv(root / "runs" / "2027-02-15" / f"scores_{m}.csv").assign(market=m)
                    for m in C.MARKETS])
    pct = dict(zip(sc["ticker"], sc["composite_pct"]))
    for _, r in t2[t2["action"] == "SELL"].iterrows():
        ok = ("rank fell" in r["reason"] and pct.get(r["ticker"], 1) < C.HOLD_MIN_PCTL) or \
             any(w in r["reason"] for w in run.FILTER_WORDS) or r["reason"] == "left the index"
        check(ok, f"sell justified: {r['ticker']} {r['reason']}")
    for p in d2["positions"]:
        if p["entry_date"] != "2027-02-15" and not p.get("flag"):
            check(pct.get(p["ticker"], 0) >= C.HOLD_MIN_PCTL, f"kept {p['ticker']} is in top 35%")
    for _, r in t2[t2["action"] == "BUY"].iterrows():
        check(r["composite_pct"] >= C.BUY_MIN_PCTL, f"buy {r['ticker']} in top 15%")
    vals = [p["value_cad"] for p in d2["positions"]]
    tgt = max(set(round(v, 2) for v in vals), key=[round(v, 2) for v in vals].count)
    check(all(abs(v - tgt) <= C.REBALANCE_SKIP_FRACTION * tgt + 0.01 for v in vals), "equal weight within skip band")
    check(d2["cash_cad"] > -0.05, f"cash not negative after rebalance ({d2['cash_cad']:.4f})")
    check(d2["last_rebalance"] == "2027-02-15", "last_rebalance updated")

    # ---------------- March: auto picks check
    check(run.main(["--mode", "auto", "--root", str(root), "--as-of", "2027-03-15"]) == 0, "March check runs")
    t3 = trades_on(root, "2027-03-15")
    check(t3.empty or set(t3["mode"]) == {"check"}, "auto picked check in March")
    navcsv = pd.read_csv(root / "state" / "nav.csv")
    check(list(navcsv["date"]) == ["2026-10-15", "2026-12-15", "2027-02-15", "2027-03-15"], "NAV history rows")
    check((root / "REPORT.md").exists(), "report written")
    h = pd.read_csv(root / "state" / "holdings.csv")
    d3 = load(root)
    check(len(h) == len(d3["positions"]), "holdings.csv matches portfolio")
    check(abs(h["value_cad"].sum() + d3["cash_cad"] - nav_of(d3)) < 0.01, "holdings.csv values sum to NAV")
    sm = pd.read_csv(root / "state" / "summary.csv")
    check(len(sm) == 1 and sm["next_run_date"].iloc[0] == "2027-04-15" and sm["next_run_type"].iloc[0] == "check",
          "summary.csv has next run (Apr 15 check)")
    from engine.report import next_run
    check(next_run("2026-10-10") == ("2026-10-15", "check"), "next run before the 15th")
    check(next_run("2027-04-15") == ("2027-05-15", "rebalance"), "next run on the 15th")
    print(f"lifecycle: final NAV C${navcsv['nav_cad'].iloc[-1]:,.2f} vs benchmark "
          f"C${navcsv['benchmark_cad'].iloc[-1]:,.2f}; trades {len(pd.read_csv(root / 'state' / 'trades.csv'))}")
    shutil.rmtree(root)


def test_data_failure_guard():
    root = Path(tempfile.mkdtemp())
    use_provider()
    run.main(["--mode", "init", "--root", str(root), "--as-of", "2026-10-15"])
    before = load(root)
    use_provider(fail_rate=0.6)
    run.main(["--mode", "rebalance", "--root", str(root), "--as-of", "2027-02-15"])
    after = load(root)
    summ = json.loads((root / "state" / "last_run.json").read_text())
    check(summ["status"] == "data_failure", "bad download aborts rebalance")
    check(sorted(p["ticker"] for p in after["positions"]) == sorted(p["ticker"] for p in before["positions"]),
          "no trades on data failure")
    # init on bad data must not create a portfolio
    root2 = Path(tempfile.mkdtemp())
    rc = run.main(["--mode", "init", "--root", str(root2), "--as-of", "2026-10-15"])
    check(rc == 1 and not (root2 / "state" / "portfolio.json").exists(), "init refuses to start on bad data")
    shutil.rmtree(root)
    shutil.rmtree(root2)


def test_holding_data_glitch_is_held():
    df = pd.DataFrame([{"ticker": "X", "market": "US", "sector": "Technology", "market_cap_cad": 5e9,
                        "fetch_ok": True, "enterprise_value": float("nan"), "n_value": 0, "n_quality": 0,
                        "momentum": 0.1, "altman_z": 3.0, "asset_growth": 0.05, "m_score": -2.5, "f_score": 5,
                        "fin_currency_assumed": False}])
    sdf, _ = scoring.score_market(df)
    reasons = sdf["reasons"].iloc[0]
    check(sdf["excluded"].iloc[0] and not reasons.startswith(run.FILTER_WORDS),
          "missing-data exclusion is not a hard sell reason")


if __name__ == "__main__":
    test_metrics_unit()
    test_holding_data_glitch_is_held()
    test_lifecycle()
    test_data_failure_guard()
    print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILURE(S)")
    raise SystemExit(1 if FAILS else 0)
