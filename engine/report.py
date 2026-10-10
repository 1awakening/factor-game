"""Human-readable REPORT.md and machine-readable outputs."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import pandas as pd

from . import config as C
from .util import clean

NAV_FIELDS = ["date", "mode", "nav_cad", "cash_cad", "benchmark_cad", "n_us", "n_ca", "total_costs_cad", "usdcad"]
TRADE_FIELDS = ["date", "mode", "ticker", "market", "name", "sector", "action", "value_cad", "cost_cad",
                "composite_pct", "reason"]


def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: _fmt(r.get(k)) for k in fields})


def _fmt(v):
    if isinstance(v, float):
        return "" if not math.isfinite(v) else round(v, 6)
    return v


def money(v) -> str:
    return f"C${v:,.0f}"


def pct(v) -> str:
    return "—" if v is None or (isinstance(v, float) and not math.isfinite(v)) else f"{v:+.1%}"


def write_report(root: Path, pf, summary: dict, trades: list[dict]) -> None:
    d = pf.d
    start = d["start_capital_cad"]
    nav, bench = pf.nav(), pf.bench_value()
    lines = [
        "# Factor game — latest report",
        "",
        f"**As of {summary['date']}** · run type: {summary['mode']} · status: {summary['status']} · "
        f"rules v{d['rules_version']} ([RULES.md](RULES.md)) · started {d['inception_date']} with {money(start)}",
        "",
        "| | Value | Since start |",
        "|---|---:|---:|",
        f"| Portfolio | {money(nav)} | {pct(nav / start - 1)} |",
        f"| Benchmark (⅔ S&P 500 + ⅓ TSX) | {money(bench)} | {pct(bench / start - 1)} |",
        f"| Cash | {money(pf.cash)} | |",
        f"| Trading costs paid | {money(d.get('total_costs_cad', 0.0))} | |",
        "",
    ]
    if summary.get("message"):
        lines += [f"> {summary['message']}", ""]

    lines += ["## Holdings", "", "| Ticker | Company | Market | Sector | Value | Return since entry | Composite rank | Entered |",
              "|---|---|---|---|---:|---:|---:|---|"]
    for p in sorted(pf.positions, key=lambda q: (q["market"] != "US", -q["value_cad"])):
        cp = p.get("composite_pct")
        cp_s = f"{cp:.0%}" if isinstance(cp, (int, float)) and math.isfinite(cp) else "—"
        flag = " ⚠" if p.get("stale") or p.get("flag") else ""
        lines.append(f"| {p['ticker']}{flag} | {p.get('name') or ''} | {p['market']} | {p.get('sector') or ''} | "
                     f"{money(p['value_cad'])} | {pct(p.get('tr_index', 1.0) - 1)} | {cp_s} | {p['entry_date']} |")
    lines.append("")

    lines += ["## Trades this run", ""]
    if trades:
        lines += ["| Action | Ticker | Market | Value | Cost | Reason |", "|---|---|---|---:|---:|---|"]
        for t in trades:
            lines.append(f"| {t['action']} | {t['ticker']} | {t['market']} | {money(t['value_cad'])} | "
                         f"C${t['cost_cad']:,.2f} | {t['reason']} |")
    else:
        lines.append("None.")
    lines.append("")

    if summary.get("warnings"):
        lines += ["## Warnings", ""] + [f"- {w}" for w in summary["warnings"]] + [""]

    if summary.get("universe"):
        lines += ["## Universe this run", "", "| Market | Source | Members | Fetched | Passed filters |",
                  "|---|---|---:|---:|---:|"]
        for m, u in summary["universe"].items():
            lines.append(f"| {m} | {u.get('source', '')} | {u.get('members', '')} | {u.get('fetched', '')} | "
                         f"{u.get('eligible', '')} |")
        lines.append("")

    nav_path = root / "state" / "nav.csv"
    if nav_path.exists():
        hist = pd.read_csv(nav_path).tail(12)
        lines += ["## Value history", "", "| Date | Run | Portfolio | Benchmark |", "|---|---|---:|---:|"]
        for _, r in hist.iterrows():
            lines.append(f"| {r['date']} | {r['mode']} | {money(r['nav_cad'])} | {money(r['benchmark_cad'])} |")
        lines.append("")

    lines += ["---", "Virtual money only. Data: Yahoo Finance (unofficial), Wikipedia, Bank of Canada. "
              "Not investment advice."]
    (root / "REPORT.md").write_text("\n".join(lines) + "\n")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean(obj), indent=2, default=str) + "\n")
