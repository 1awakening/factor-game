"""Turn one company's normalized statements into the inputs the rules need.

Pure functions, no network. A raw record looks like:

    {
      "ticker": "AAPL", "market": "US", "name": "...", "sector": "Technology",
      "currency": "USD",            # currency the shares trade in
      "fin_currency": "USD",        # currency the statements are reported in
      "fin_currency_assumed": False,
      "fx_fin_to_trade": 1.0,       # multiply statement values by this
      "market_cap": 3.1e12,         # in trading currency
      "annual":    {item: [newest, older, ...]},
      "quarterly": {item: [newest, older, ...]},
      "fetch_ok": True, "error": None,
    }
"""
from __future__ import annotations

import math

import numpy as np

NAN = float("nan")


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return NAN
    return v if math.isfinite(v) else NAN


def _ok(*xs) -> bool:
    return all(math.isfinite(x) for x in xs)


def _get(series: dict, item: str, i: int) -> float:
    vals = series.get(item) or []
    return _f(vals[i]) if i < len(vals) else NAN


def ttm(raw: dict, item: str) -> float:
    """Sum of the last 4 quarters if all are present, else the latest annual value."""
    q = raw.get("quarterly", {}).get(item) or []
    if len(q) >= 4:
        vals = [_f(v) for v in q[:4]]
        if all(math.isfinite(v) for v in vals):
            return float(sum(vals))
    return _get(raw.get("annual", {}), item, 0)


def latest(raw: dict, item: str) -> float:
    """Latest balance-sheet value: newest quarter, else newest annual."""
    v = _get(raw.get("quarterly", {}), item, 0)
    return v if math.isfinite(v) else _get(raw.get("annual", {}), item, 0)


def annual(raw: dict, item: str, i: int) -> float:
    return _get(raw.get("annual", {}), item, i)


def _clip(x: float, lo: float, hi: float) -> float:
    return x if not math.isfinite(x) else min(max(x, lo), hi)


def _ratio(a: float, b: float) -> float:
    return a / b if _ok(a, b) and b != 0 else NAN


# ---------------------------------------------------------------- components

def gross_profit_ttm(raw: dict) -> float:
    gp = ttm(raw, "gross_profit")
    if math.isfinite(gp):
        return gp
    rev, cogs = ttm(raw, "revenue"), ttm(raw, "cogs")
    return rev - cogs if _ok(rev, cogs) else NAN


def ebit_ttm(raw: dict) -> float:
    v = ttm(raw, "ebit")
    return v if math.isfinite(v) else ttm(raw, "operating_income")


def ebitda_ttm(raw: dict) -> float:
    v = ttm(raw, "ebitda")
    if math.isfinite(v):
        return v
    e, d = ebit_ttm(raw), ttm(raw, "dep")
    return e + d if _ok(e, d) else NAN


def fcf_ttm(raw: dict) -> float:
    cfo, capex = ttm(raw, "cfo"), ttm(raw, "capex")
    if _ok(cfo, capex):
        return cfo - abs(capex)
    return ttm(raw, "fcf")


def shareholder_yield_amount(raw: dict) -> float:
    """Dividends + net buybacks + net debt paydown, trailing 12 months (statement currency).

    Missing line items count as zero, but only if the cash-flow statement exists at all.
    """
    if not math.isfinite(ttm(raw, "cfo")):
        return NAN

    def z(v):
        return v if math.isfinite(v) else 0.0

    div = abs(z(ttm(raw, "dividends_paid")))
    net_iss = ttm(raw, "net_stock_issuance")
    if math.isfinite(net_iss):
        buyback = -net_iss
    else:
        buyback = abs(z(ttm(raw, "repurchase"))) - abs(z(ttm(raw, "stock_issuance")))
    net_debt = ttm(raw, "net_debt_issuance")
    if math.isfinite(net_debt):
        paydown = -net_debt
    else:
        paydown = abs(z(ttm(raw, "debt_repayment"))) - abs(z(ttm(raw, "debt_issuance")))
    return div + buyback + paydown


def altman_z(raw: dict, mve_fin: float) -> float:
    """Original Altman Z. mve_fin = market cap in statement currency."""
    ta, tl = latest(raw, "total_assets"), latest(raw, "total_liab")
    ebit, sales = ebit_ttm(raw), ttm(raw, "revenue")
    if not _ok(ta, tl, ebit, sales, mve_fin) or ta <= 0 or tl <= 0:
        return NAN
    wc = latest(raw, "working_capital")
    if not math.isfinite(wc):
        ca, cl = latest(raw, "current_assets"), latest(raw, "current_liab")
        wc = ca - cl if _ok(ca, cl) else 0.0
    re = latest(raw, "retained_earnings")
    re = re if math.isfinite(re) else 0.0
    return 1.2 * wc / ta + 1.4 * re / ta + 3.3 * ebit / ta + 0.6 * mve_fin / tl + 1.0 * sales / ta


def asset_growth(raw: dict) -> float:
    t, p = annual(raw, "total_assets", 0), annual(raw, "total_assets", 1)
    return t / p - 1.0 if _ok(t, p) and p > 0 and t > 0 else NAN


def beneish_m(raw: dict) -> float:
    """Beneish (1999) 8-variable M-score from the two latest annual reports.

    Missing components are set to their neutral value (1 for indices, 0 for TATA).
    Each index is capped to [0.25, 4] and TATA to [-1, 1] so a single tiny
    denominator can't decide the result.
    """
    s_t, s_p = annual(raw, "revenue", 0), annual(raw, "revenue", 1)
    ta_t, ta_p = annual(raw, "total_assets", 0), annual(raw, "total_assets", 1)
    if not _ok(s_t, s_p, ta_t, ta_p) or min(s_t, s_p, ta_t, ta_p) <= 0:
        return NAN

    def a(item, i):
        return annual(raw, item, i)

    def idx(x):
        return _clip(x, 0.25, 4.0) if math.isfinite(x) else 1.0

    # DSRI
    dsri = _ratio(_ratio(a("receivables", 0), s_t), _ratio(a("receivables", 1), s_p))

    # GMI
    def gm(i, s):
        gp = a("gross_profit", i)
        if not math.isfinite(gp):
            cogs = a("cogs", i)
            gp = s - cogs if math.isfinite(cogs) else NAN
        return _ratio(gp, s)

    gm_t, gm_p = gm(0, s_t), gm(1, s_p)
    gmi = gm_p / gm_t if _ok(gm_t, gm_p) and gm_t > 0 else NAN

    # AQI
    def hard(i, ta):
        ca, ppe = a("current_assets", i), a("ppe", i)
        sec = a("securities", i)
        sec = sec if math.isfinite(sec) else 0.0
        if not _ok(ca, ppe):
            return NAN
        return 1.0 - (ca + ppe + sec) / ta

    h_t, h_p = hard(0, ta_t), hard(1, ta_p)
    aqi = h_t / h_p if _ok(h_t, h_p) and h_p > 0 else NAN

    sgi = s_t / s_p

    # DEPI
    def deprate(i):
        d, ppe = a("dep", i), a("ppe", i)
        return d / (d + ppe) if _ok(d, ppe) and (d + ppe) > 0 else NAN

    dr_t, dr_p = deprate(0), deprate(1)
    depi = dr_p / dr_t if _ok(dr_t, dr_p) and dr_t > 0 else NAN

    sgai = _ratio(_ratio(a("sga", 0), s_t), _ratio(a("sga", 1), s_p))

    # LVGI
    def lev(i, ta):
        cl, ltd = a("current_liab", i), a("lt_debt", i)
        ltd = ltd if math.isfinite(ltd) else 0.0
        return (cl + ltd) / ta if math.isfinite(cl) else NAN

    l_t, l_p = lev(0, ta_t), lev(1, ta_p)
    lvgi = l_t / l_p if _ok(l_t, l_p) and l_p > 0 else NAN

    ni, cfo = a("net_income", 0), a("cfo", 0)
    tata = _clip((ni - cfo) / ta_t, -1.0, 1.0) if _ok(ni, cfo) else 0.0

    return (-4.84 + 0.920 * idx(dsri) + 0.528 * idx(gmi) + 0.404 * idx(aqi)
            + 0.892 * idx(sgi) + 0.115 * idx(depi) - 0.172 * idx(sgai)
            + 4.679 * tata - 0.327 * idx(lvgi))


def piotroski_f(raw: dict) -> int:
    """Piotroski (2000) F-score from the two latest annual reports. Missing tests score 0."""
    def a(item, i):
        return annual(raw, item, i)

    ni_t, ni_p = a("net_income", 0), a("net_income", 1)
    ta_t, ta_p = a("total_assets", 0), a("total_assets", 1)
    cfo_t = a("cfo", 0)
    roa_t, roa_p = _ratio(ni_t, ta_t), _ratio(ni_p, ta_p)

    def lev(i, ta):
        ltd = a("lt_debt", i)
        ltd = ltd if math.isfinite(ltd) else 0.0
        return _ratio(ltd, ta)

    lev_t, lev_p = lev(0, ta_t), lev(1, ta_p)
    cr_t = _ratio(a("current_assets", 0), a("current_liab", 0))
    cr_p = _ratio(a("current_assets", 1), a("current_liab", 1))
    sh_t, sh_p = a("shares", 0), a("shares", 1)

    def gm(i):
        s = a("revenue", i)
        gp = a("gross_profit", i)
        if not math.isfinite(gp):
            cogs = a("cogs", i)
            gp = s - cogs if _ok(s, cogs) else NAN
        return _ratio(gp, s)

    at_t = _ratio(a("revenue", 0), ta_t)
    at_p = _ratio(a("revenue", 1), ta_p)

    tests = [
        _ok(roa_t) and roa_t > 0,
        _ok(cfo_t) and cfo_t > 0,
        _ok(roa_t, roa_p) and roa_t > roa_p,
        _ok(cfo_t, ta_t, roa_t) and ta_t > 0 and cfo_t / ta_t > roa_t,
        _ok(lev_t, lev_p) and (lev_t < lev_p or (lev_t == 0 and lev_p == 0)),
        _ok(cr_t, cr_p) and cr_t > cr_p,
        _ok(sh_t, sh_p) and sh_t <= sh_p,
        _ok(gm(0), gm(1)) and gm(0) > gm(1),
        _ok(at_t, at_p) and at_t > at_p,
    ]
    return int(sum(bool(t) for t in tests))


def roa_volatility(raw: dict) -> float:
    vals = []
    for i in range(5):
        ni, ta = annual(raw, "net_income", i), annual(raw, "total_assets", i)
        if _ok(ni, ta) and ta > 0:
            vals.append(ni / ta)
    return float(np.std(vals, ddof=1)) if len(vals) >= 3 else NAN


# ---------------------------------------------------------------- main entry

def compute(raw: dict, usdcad: float) -> dict:
    """All rule inputs for one company. Monetary values in trading currency."""
    fx = _f(raw.get("fx_fin_to_trade", 1.0))
    mcap = _f(raw.get("market_cap"))
    ccy = raw.get("currency") or ("USD" if raw.get("market") == "US" else "CAD")
    to_cad = usdcad if ccy == "USD" else (1.0 if ccy == "CAD" else NAN)

    out = {
        "ticker": raw.get("ticker"),
        "market": raw.get("market"),
        "name": raw.get("name"),
        "sector": raw.get("sector"),
        "currency": ccy,
        "fin_currency": raw.get("fin_currency"),
        "fin_currency_assumed": bool(raw.get("fin_currency_assumed", False)),
        "fetch_ok": bool(raw.get("fetch_ok", False)),
        "error": raw.get("error"),
        "market_cap": mcap,
        "market_cap_cad": mcap * to_cad if _ok(mcap, to_cad) else NAN,
    }

    debt = latest(raw, "total_debt")
    cash = latest(raw, "cash")
    ta = latest(raw, "total_assets")
    debt0 = debt if math.isfinite(debt) else 0.0
    cash0 = cash if math.isfinite(cash) else 0.0

    ev = NAN
    if _ok(mcap, fx):
        ev = mcap + (debt0 - cash0) * fx
        if ev <= 0:
            ev = NAN
    out["enterprise_value"] = ev

    ebit, ebitda, fcf = ebit_ttm(raw), ebitda_ttm(raw), fcf_ttm(raw)
    shy_amt = shareholder_yield_amount(raw)
    out["ebit_ev"] = ebit * fx / ev if _ok(ebit, fx, ev) else NAN
    out["ebitda_ev"] = ebitda * fx / ev if _ok(ebitda, fx, ev) else NAN
    out["fcf_ev"] = fcf * fx / ev if _ok(fcf, fx, ev) else NAN
    out["shareholder_yield"] = shy_amt * fx / mcap if _ok(shy_amt, fx, mcap) and mcap > 0 else NAN

    ta_ok = math.isfinite(ta) and ta > 0
    out["gp_assets"] = gross_profit_ttm(raw) / ta if ta_ok and math.isfinite(gross_profit_ttm(raw)) else NAN
    cfo = ttm(raw, "cfo")
    out["cfo_assets"] = cfo / ta if ta_ok and math.isfinite(cfo) else NAN
    out["debt_assets"] = debt0 / ta if ta_ok else NAN
    out["roa_vol"] = roa_volatility(raw)

    mve_fin = mcap / fx if _ok(mcap, fx) and fx != 0 else NAN
    out["altman_z"] = altman_z(raw, mve_fin)
    out["asset_growth"] = asset_growth(raw)
    out["m_score"] = beneish_m(raw)
    out["f_score"] = piotroski_f(raw)

    out["n_value"] = sum(math.isfinite(out[k]) for k in ("ebit_ev", "ebitda_ev", "fcf_ev", "shareholder_yield"))
    out["n_quality"] = sum(math.isfinite(out[k]) for k in ("gp_assets", "cfo_assets", "debt_assets", "roa_vol"))
    return out


def momentum(series, lookback: int, skip: int) -> float:
    """Total return from `lookback` to `skip` trading days ago. series: adjusted closes, oldest first."""
    s = series.dropna()
    if len(s) < lookback + 1:
        return NAN
    p_old, p_recent = _f(s.iloc[-(lookback + 1)]), _f(s.iloc[-(skip + 1)])
    return p_recent / p_old - 1.0 if _ok(p_old, p_recent) and p_old > 0 else NAN
