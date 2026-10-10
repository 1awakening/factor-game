"""Exclusion filters and percentile scores within one market."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import config as C

VALUE_COLS = ["ebit_ev", "ebitda_ev", "fcf_ev", "shareholder_yield"]
QUALITY_HIGH = ["gp_assets", "cfo_assets"]
QUALITY_LOW = ["debt_assets", "roa_vol"]  # lower is better


def _isnum(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def missing_reasons(row) -> list[str]:
    r = []
    if not _isnum(row.get("enterprise_value")):
        r.append("no enterprise value")
    if row.get("n_value", 0) < 2:
        r.append("too few value inputs")
    if row.get("n_quality", 0) < 2:
        r.append("too few quality inputs")
    if not _isnum(row.get("momentum")):
        r.append("under 12 months of prices")
    if row.get("fin_currency_assumed") and row.get("market") == "CA":
        r.append("statement currency unknown")
    return r


def filter_reasons(row, cutoffs: dict) -> list[str]:
    """Distress, manipulation and asset-growth tests against given cutoffs."""
    r = []
    z, zc = row.get("altman_z"), cutoffs.get("altman_z_p10")
    if _isnum(z) and _isnum(zc) and float(z) < float(zc):
        r.append(f"distress (Altman Z {float(z):.2f} < cutoff {float(zc):.2f})")
    m = row.get("m_score")
    if _isnum(m) and float(m) > C.MSCORE_MAX:
        r.append(f"manipulation risk (M-score {float(m):.2f} > {C.MSCORE_MAX})")
    g, gc = row.get("asset_growth"), cutoffs.get("asset_growth_p90")
    if _isnum(g) and _isnum(gc) and float(g) > float(gc):
        r.append(f"aggressive asset growth ({float(g):.0%} > cutoff {float(gc):.0%})")
    return r


def score_market(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """df: one row per stock (output of metrics.compute plus 'momentum').

    Returns (scored df, cutoffs). Adds columns: stage, excluded, reasons,
    value, quality, mom_rank, composite, composite_pct.
    """
    df = df.copy().reset_index(drop=True)
    for col in VALUE_COLS + QUALITY_HIGH + QUALITY_LOW + ["momentum", "altman_z", "asset_growth", "m_score",
                                                         "market_cap_cad", "enterprise_value"]:
        if col not in df:
            df[col] = np.nan
    for col, default in (("f_score", 0), ("n_value", 0), ("n_quality", 0), ("sector", None), ("fetch_ok", True)):
        if col not in df:
            df[col] = default
    reasons = [[] for _ in range(len(df))]

    sector_bad = df["sector"].isin(C.EXCLUDED_SECTORS)
    size_bad = ~(pd.to_numeric(df["market_cap_cad"], errors="coerce") >= C.MIN_MARKET_CAP_CAD)
    fetch_bad = ~df["fetch_ok"].astype(bool)
    for i in range(len(df)):
        if fetch_bad.iat[i]:
            reasons[i].append("data download failed")
        elif sector_bad.iat[i]:
            reasons[i].append(f"excluded sector ({df.at[i, 'sector']})")
        elif size_bad.iat[i]:
            reasons[i].append("market cap under C$1B")

    base = ~(sector_bad | size_bad | fetch_bad)
    z = pd.to_numeric(df.loc[base, "altman_z"], errors="coerce").dropna()
    g = pd.to_numeric(df.loc[base, "asset_growth"], errors="coerce").dropna()
    cutoffs = {
        "altman_z_p10": float(z.quantile(C.DISTRESS_PCTL)) if len(z) else None,
        "asset_growth_p90": float(g.quantile(C.ASSET_GROWTH_PCTL)) if len(g) else None,
        "base_count": int(base.sum()),
    }

    for i in np.flatnonzero(base.to_numpy()):
        row = df.iloc[i].to_dict()
        reasons[i].extend(missing_reasons(row))
        reasons[i].extend(filter_reasons(row, cutoffs))

    df["reasons"] = ["; ".join(r) for r in reasons]
    df["excluded"] = [bool(r) for r in reasons]
    elig = ~df["excluded"]

    def pr(col, higher=True):
        s = pd.to_numeric(df.loc[elig, col], errors="coerce")
        return (s if higher else -s).rank(pct=True, method="average")

    vranks = pd.concat([pr(c) for c in VALUE_COLS], axis=1)
    qranks = pd.concat([pr(c) for c in QUALITY_HIGH] + [pr(c, higher=False) for c in QUALITY_LOW], axis=1)
    df["value"] = vranks.mean(axis=1, skipna=True)
    df["quality"] = qranks.mean(axis=1, skipna=True)
    df["mom_rank"] = pr("momentum")
    comp = df.loc[elig, ["value", "quality", "mom_rank"]].mean(axis=1, skipna=False)
    df["composite"] = comp
    df["composite_pct"] = comp.rank(pct=True, method="average")
    cutoffs["eligible_count"] = int(elig.sum())
    return df, cutoffs


def ordered_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """Eligible stocks, best first (composite, then F-score)."""
    e = df[~df["excluded"] & df["composite_pct"].notna()].copy()
    return e.sort_values(["composite_pct", "f_score"], ascending=[False, False])
