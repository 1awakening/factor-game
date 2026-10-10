"""Entry point: python -m engine.run --mode auto|init|check|rebalance"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path

import pandas as pd

from . import config as C
from . import metrics, report, scoring
from .portfolio import Portfolio

log = logging.getLogger("factor-game")

FILTER_WORDS = ("distress", "manipulation", "aggressive asset growth", "excluded sector", "market cap under")


def _fmt_pct(v) -> str:
    return f"{v:.0%}" if isinstance(v, (int, float)) and math.isfinite(v) else "n/a"


def resolve_mode(mode: str, pf: Portfolio | None, as_of: str) -> str:
    if mode != "auto":
        return mode
    if pf is None:
        return "init"
    d = pd.Timestamp(as_of)
    last = pf.d.get("last_rebalance")
    same_month = last is not None and pd.Timestamp(last).strftime("%Y-%m") == d.strftime("%Y-%m")
    if d.month in C.REBALANCE_MONTHS and not same_month:
        return "rebalance"
    return "check"


def make_provider(name: str, as_of: str):
    if name == "synthetic":
        from .synthetic import SyntheticProvider
        return SyntheticProvider(as_of)
    from .data_yf import YahooProvider
    return YahooProvider()


# --------------------------------------------------------------------------- runs

def score_universe(provider, pf: Portfolio | None, as_of: str, usdcad: float, summary: dict):
    """Fetch and score both markets. Returns (scored dict, full-universe dict, prices, fetch failure rate)."""
    universes, prefiltered = {}, {}
    for m in C.MARKETS:
        u, label = provider.universe(m)
        universes[m] = u
        keep = ~u["sector_hint"].isin(C.EXCLUDED_SECTORS) if "sector_hint" in u else True
        prefiltered[m] = u[keep].reset_index(drop=True)
        summary["universe"][m] = {"source": label, "members": int(len(u)),
                                  "after_sector_prefilter": int(len(prefiltered[m]))}
        log.info("%s universe: %s, %d members, %d after sector prefilter", m, label, len(u), len(prefiltered[m]))

    tickers = set(C.BENCHMARK)
    for m in C.MARKETS:
        tickers |= set(prefiltered[m]["ticker"])
    if pf:
        tickers |= {p["ticker"] for p in pf.positions}
    start = (pd.Timestamp(as_of) - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    log.info("downloading prices for %d tickers", len(tickers))
    prices = provider.prices(sorted(tickers), start, as_of)

    scored, n_fail, n_total = {}, 0, 0
    for m in C.MARKETS:
        rows = []
        u = prefiltered[m]
        for i, r in u.iterrows():
            t = r["ticker"]
            s = prices[t].dropna() if t in prices.columns else pd.Series(dtype=float)
            last_price = float(s.iloc[-1]) if len(s) else None
            rec = provider.fundamentals(t, m, r.get("sector_hint"), r.get("name"), last_price)
            row = metrics.compute(rec, usdcad)
            row["momentum"] = metrics.momentum(s, C.MOM_LOOKBACK, C.MOM_SKIP)
            row["last_price"] = last_price
            row["last_price_date"] = s.index[-1].strftime("%Y-%m-%d") if len(s) else None
            rows.append(row)
            n_total += 1
            n_fail += 0 if rec["fetch_ok"] else 1
            if (i + 1) % 50 == 0:
                log.info("%s: %d/%d fetched", m, i + 1, len(u))
        df = pd.DataFrame(rows)
        sdf, cut = scoring.score_market(df)
        scored[m] = (sdf, cut)
        summary["universe"][m]["fetched"] = int(df["fetch_ok"].sum())
        summary["universe"][m]["eligible"] = cut["eligible_count"]
        counts = {}
        for reasons in sdf.loc[sdf["excluded"], "reasons"]:
            for part in reasons.split("; "):
                key = part.split(" (")[0]
                counts[key] = counts.get(key, 0) + 1
        summary["universe"][m]["exclusions"] = counts
    fail_rate = n_fail / n_total if n_total else 1.0
    return scored, universes, prices, fail_rate


def do_rebalance(provider, pf: Portfolio | None, as_of: str, mode: str, root: Path, summary: dict):
    fx_start = (pd.Timestamp(as_of) - pd.Timedelta(days=430)).strftime("%Y-%m-%d")
    fx = provider.fx_usdcad(fx_start, as_of)
    if fx.dropna().empty:
        raise RuntimeError("no USD/CAD exchange rate available")
    usdcad = float(fx.dropna().iloc[-1])
    summary["usdcad"] = usdcad

    scored, universes, prices, fail_rate = score_universe(provider, pf, as_of, usdcad, summary)
    summary["fetch_failure_rate"] = round(fail_rate, 4)

    if pf is None:
        pf = Portfolio.new(as_of)
    else:
        summary["warnings"] += pf.mark(prices, fx, as_of)

    ctx = {"date": as_of, "mode": mode}
    if fail_rate > C.MAX_FETCH_FAILURE:
        summary["status"] = "data_failure"
        summary["message"] = (f"{fail_rate:.0%} of the universe failed to download (limit "
                              f"{C.MAX_FETCH_FAILURE:.0%}). No trades were made; portfolio marked to market only.")
        return pf, [], usdcad

    for m in C.MARKETS:
        pf.d["cutoffs"][m] = scored[m][1]
        sdf = scored[m][0]
        out = root / "runs" / as_of / f"scores_{m}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        cols = ["ticker", "name", "sector", "market_cap_cad", "enterprise_value", "ebit_ev", "ebitda_ev", "fcf_ev",
                "shareholder_yield", "gp_assets", "cfo_assets", "debt_assets", "roa_vol", "momentum",
                "altman_z", "m_score", "asset_growth", "f_score", "value", "quality", "mom_rank", "composite_pct",
                "excluded", "reasons", "fetch_ok", "fin_currency", "error"]
        sdf.sort_values("composite_pct", ascending=False, na_position="last")[
            [c for c in cols if c in sdf.columns]].to_csv(out, index=False, float_format="%.6g")

    trades = []
    # ---- sells
    for p in list(pf.positions):
        m = p["market"]
        sdf = scored[m][0]
        hit = sdf[sdf["ticker"] == p["ticker"]]
        if hit.empty:
            in_index = p["ticker"] in set(universes[m]["ticker"])
            if in_index:
                hint = universes[m].loc[universes[m]["ticker"] == p["ticker"], "sector_hint"].iloc[0]
                trades.append(pf.sell(p, f"excluded sector ({hint})", ctx))
            else:
                trades.append(pf.sell(p, "left the index", ctx))
            continue
        row = hit.iloc[0].to_dict()
        if not row["fetch_ok"]:
            p["flag"] = "data download failed this run; held"
            summary["warnings"].append(f"{p['ticker']}: data download failed; held, not re-ranked")
            continue
        if row["excluded"]:
            hard = [r for r in row["reasons"].split("; ") if r.startswith(FILTER_WORDS)]
            if hard:
                trades.append(pf.sell(p, "; ".join(hard), ctx))
            else:
                p["flag"] = f"incomplete data ({row['reasons']}); held"
                summary["warnings"].append(f"{p['ticker']}: {row['reasons']}; held")
            continue
        p["composite_pct"] = row["composite_pct"]
        p.pop("flag", None)
        if row["composite_pct"] < C.HOLD_MIN_PCTL:
            trades.append(pf.sell(p, f"rank fell to {_fmt_pct(row['composite_pct'])} "
                                     f"(hold threshold {C.HOLD_MIN_PCTL:.0%})", ctx))

    # ---- buys
    new_buys = []
    held = {p["ticker"] for p in pf.positions}
    for m in C.MARKETS:
        slots = C.SLOTS[m] - len(pf.held(m))
        sector_n = {}
        for p in pf.held(m):
            sector_n[p.get("sector")] = sector_n.get(p.get("sector"), 0) + 1
        for _, r in scoring.ordered_candidates(scored[m][0]).iterrows():
            if slots <= 0 or r["composite_pct"] < C.BUY_MIN_PCTL:
                break
            if r["ticker"] in held or sector_n.get(r["sector"], 0) >= C.SECTOR_CAP[m]:
                continue
            row = r.to_dict()
            row["_reason"] = (f"new buy: composite {_fmt_pct(r['composite_pct'])} (value {_fmt_pct(r['value'])}, "
                              f"quality {_fmt_pct(r['quality'])}, momentum {_fmt_pct(r['mom_rank'])})")
            new_buys.append(row)
            held.add(r["ticker"])
            sector_n[r["sector"]] = sector_n.get(r["sector"], 0) + 1
            slots -= 1
        if slots > 0:
            summary["warnings"].append(f"{m}: {slots} slot(s) left in cash (not enough eligible stocks in the top 15%)")

    trades += pf.equal_weight(ctx, new_buys)
    pf.d["last_rebalance"] = as_of
    return pf, trades, usdcad


def do_check(provider, pf: Portfolio, as_of: str, root: Path, summary: dict):
    ctx = {"date": as_of, "mode": "check"}
    marks = [p["mark_date"] for p in pf.positions] + [pf.d["benchmark"]["mark_date"]]
    start = (pd.Timestamp(min(marks)) - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    fx = provider.fx_usdcad(start, as_of)
    if fx.dropna().empty:
        raise RuntimeError("no USD/CAD exchange rate available")
    usdcad = float(fx.dropna().iloc[-1])
    summary["usdcad"] = usdcad
    tickers = sorted({p["ticker"] for p in pf.positions} | set(C.BENCHMARK))
    prices = provider.prices(tickers, start, as_of)
    summary["warnings"] += pf.mark(prices, fx, as_of)

    trades, checks = [], []
    for p in list(pf.positions):
        if p.get("stale"):
            trades.append(pf.sell(p, f"stopped trading (last price {p.get('last_price_date')})", ctx))
            checks.append({"ticker": p["ticker"], "result": "sold", "detail": "stopped trading"})
            continue
        rec = provider.fundamentals(p["ticker"], p["market"], p.get("sector"), p.get("name"),
                                    p.get("last_price_local"))
        if not rec["fetch_ok"]:
            p["flag"] = "data download failed this run; held"
            summary["warnings"].append(f"{p['ticker']}: data download failed; held")
            checks.append({"ticker": p["ticker"], "result": "held (no data)", "detail": rec.get("error")})
            continue
        row = metrics.compute(rec, usdcad)
        reasons = scoring.filter_reasons(row, pf.d["cutoffs"].get(p["market"], {}))
        checks.append({"ticker": p["ticker"], "altman_z": row["altman_z"], "m_score": row["m_score"],
                       "asset_growth": row["asset_growth"],
                       "result": "sold" if reasons else "passed", "detail": "; ".join(reasons)})
        if reasons:
            trades.append(pf.sell(p, "; ".join(reasons), ctx))
        else:
            p.pop("flag", None)
    out = root / "runs" / as_of / "checks.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(checks).to_csv(out, index=False, float_format="%.4g")
    return pf, trades, usdcad


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "init", "check", "rebalance"])
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD (default: today in Toronto)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--provider", default="yahoo", choices=["yahoo", "synthetic"])
    ap.add_argument("--force", action="store_true", help="allow init over an existing portfolio")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    root = Path(args.root)
    as_of = args.as_of or pd.Timestamp.now(tz="America/Toronto").strftime("%Y-%m-%d")
    state_path = root / "state" / "portfolio.json"
    pf = Portfolio.load(state_path)

    if args.mode == "auto" and pf is not None and pf.d.get("last_run") == as_of:
        log.info("already ran for %s; nothing to do", as_of)
        return 0
    mode = resolve_mode(args.mode, pf, as_of)
    if mode == "init" and pf is not None and not args.force:
        log.error("portfolio already exists; use --force to start over")
        return 2
    if mode in ("check", "rebalance") and pf is None:
        log.error("no portfolio yet; run init first")
        return 2
    if mode == "init":
        pf = None

    summary = {"date": as_of, "mode": mode, "status": "ok", "rules_version": C.RULES_VERSION,
               "warnings": [], "universe": {}}
    log.info("run %s mode=%s", as_of, mode)
    provider = make_provider(args.provider, as_of)

    try:
        if mode == "check":
            pf, trades, usdcad = do_check(provider, pf, as_of, root, summary)
        else:
            pf, trades, usdcad = do_rebalance(provider, pf, as_of, mode, root, summary)
    except Exception as e:  # never write a half-updated portfolio
        log.exception("run failed")
        summary["status"] = "error"
        summary["message"] = f"Run failed before any state was saved: {type(e).__name__}: {e}"
        report.write_json(root / "runs" / as_of / "summary.json", summary)
        report.write_json(root / "state" / "last_run.json", summary)
        return 1

    if mode == "init" and summary["status"] == "data_failure":
        # don't start the game on bad data; the next init attempt starts clean
        report.write_json(root / "runs" / as_of / "summary.json", summary)
        report.write_json(root / "state" / "last_run.json", summary)
        log.error(summary["message"])
        return 1

    pf.d["last_run"] = as_of
    nav, bench = pf.nav(), pf.bench_value()
    summary.update({
        "nav_cad": nav, "benchmark_cad": bench, "cash_cad": pf.cash,
        "return_since_start": nav / pf.d["start_capital_cad"] - 1,
        "benchmark_return_since_start": bench / pf.d["start_capital_cad"] - 1,
        "n_positions": {m: len(pf.held(m)) for m in C.MARKETS},
        "trades": len(trades), "inception_date": pf.d["inception_date"],
    })
    pf.save(state_path)
    report.append_csv(root / "state" / "nav.csv", report.NAV_FIELDS, [{
        "date": as_of, "mode": mode, "nav_cad": nav, "cash_cad": pf.cash, "benchmark_cad": bench,
        "n_us": len(pf.held("US")), "n_ca": len(pf.held("CA")),
        "total_costs_cad": pf.d.get("total_costs_cad", 0.0), "usdcad": usdcad,
    }])
    report.append_csv(root / "state" / "trades.csv", report.TRADE_FIELDS, trades)
    report.write_json(root / "runs" / as_of / "summary.json", {**summary, "trade_list": trades})
    report.write_json(root / "state" / "last_run.json", {**summary, "trade_list": trades})
    report.write_report(root, pf, summary, trades)
    log.info("done: NAV C$%.2f, benchmark C$%.2f, %d trades, status %s", nav, bench, len(trades), summary["status"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
