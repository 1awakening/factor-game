"""Index membership from Wikipedia, converted to Yahoo Finance tickers."""
from __future__ import annotations

import io
import re

import pandas as pd
import requests

UA = {"User-Agent": "factor-game/1.0 (personal research project; https://github.com/1awakening/factor-game)"}

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
TSX_COMPOSITE_URL = "https://en.wikipedia.org/wiki/S%26P/TSX_Composite_Index"
TSX60_URL = "https://en.wikipedia.org/wiki/S%26P/TSX_60"

# Wikipedia uses GICS sector names; Yahoo uses its own. Normalize to Yahoo's.
GICS_TO_YAHOO = {
    "information technology": "Technology",
    "health care": "Healthcare",
    "financials": "Financial Services",
    "consumer discretionary": "Consumer Cyclical",
    "consumer staples": "Consumer Defensive",
    "materials": "Basic Materials",
    "communication services": "Communication Services",
    "telecommunication services": "Communication Services",
    "telecommunications": "Communication Services",
    "industrials": "Industrials",
    "energy": "Energy",
    "utilities": "Utilities",
    "real estate": "Real Estate",
}


def normalize_sector(s) -> str | None:
    if not isinstance(s, str) or not s.strip():
        return None
    key = s.strip().lower()
    return GICS_TO_YAHOO.get(key, s.strip())


def _tables(url: str) -> list[pd.DataFrame]:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return pd.read_html(io.StringIO(r.text))


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [" ".join(str(x) for x in c if "Unnamed" not in str(x)).strip() for c in df.columns]
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _pick(df: pd.DataFrame, *patterns: str) -> str | None:
    for pat in patterns:
        for c in df.columns:
            if re.search(pat, c, re.I):
                return c
    return None


def sp500() -> pd.DataFrame:
    for t in _tables(SP500_URL):
        t = _flatten(t)
        sym, sec = _pick(t, r"^symbol$", r"ticker"), _pick(t, r"gics sector", r"sector")
        name = _pick(t, r"security", r"company")
        if sym and sec and len(t) > 400:
            out = pd.DataFrame({
                "ticker": t[sym].astype(str).str.strip().str.replace(".", "-", regex=False),
                "name": t[name].astype(str) if name else None,
                "sector_hint": t[sec].map(normalize_sector),
            })
            return out.drop_duplicates("ticker").reset_index(drop=True)
    raise RuntimeError("S&P 500 table not found on Wikipedia")


def _tsx_ticker(raw: str) -> str | None:
    s = str(raw).strip().upper()
    s = re.sub(r"^(TSX|TSE)\s*:\s*", "", s)
    s = s.split()[0] if s else s
    if not re.fullmatch(r"[A-Z0-9]+(\.[A-Z0-9]+)?", s):
        return None
    return s.replace(".", "-") + ".TO"


def _tsx_from(url: str, min_rows: int) -> pd.DataFrame:
    for t in _tables(url):
        t = _flatten(t)
        sym = _pick(t, r"^ticker", r"symbol", r"ticker")
        if not sym or len(t) < min_rows:
            continue
        sec = _pick(t, r"gics sector", r"^sector", r"sector")
        name = _pick(t, r"company", r"name", r"constituent")
        out = pd.DataFrame({
            "ticker": t[sym].map(_tsx_ticker),
            "name": t[name].astype(str) if name else None,
            "sector_hint": t[sec].map(normalize_sector) if sec else None,
        }).dropna(subset=["ticker"])
        if len(out) >= min_rows:
            return out.drop_duplicates("ticker").reset_index(drop=True)
    raise RuntimeError(f"TSX table not found at {url}")


def tsx() -> tuple[pd.DataFrame, str]:
    try:
        return _tsx_from(TSX_COMPOSITE_URL, 150), "S&P/TSX Composite"
    except Exception:
        return _tsx_from(TSX60_URL, 50), "S&P/TSX 60 (Composite list unavailable)"
