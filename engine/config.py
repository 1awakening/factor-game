"""Frozen parameters. Must match RULES.md exactly."""

RULES_VERSION = 1

START_CAPITAL_CAD = 30_000.0

MARKETS = ("US", "CA")
SLOTS = {"US": 20, "CA": 10}
TOTAL_SLOTS = sum(SLOTS.values())
SECTOR_CAP = {"US": 5, "CA": 3}

EXCLUDED_SECTORS = {"Financial Services", "Utilities", "Real Estate"}
MIN_MARKET_CAP_CAD = 1_000_000_000.0

# Exclusion filters
DISTRESS_PCTL = 0.10          # bottom 10% Altman Z is excluded
ASSET_GROWTH_PCTL = 0.90      # top 10% asset growth is excluded
MSCORE_MAX = -1.78            # M-score above this is excluded

# Rank thresholds (composite percentile, 0..1, higher = better)
BUY_MIN_PCTL = 0.85           # buy only from the top 15%
HOLD_MIN_PCTL = 0.65          # sell when below the top 35%

# Rebalance
REBALANCE_MONTHS = {2, 5, 8, 11}
REBALANCE_SKIP_FRACTION = 0.05   # skip weight adjustments < 5% of target size

# Monthly check
STALE_PRICE_TRADING_DAYS = 10

# Costs (fractions of trade value)
TRADE_COST = 0.0010
FX_COST_US = 0.0020

# Benchmark
BENCHMARK = {"SPY": 2 / 3, "XIC.TO": 1 / 3}
BENCHMARK_MARKET = {"SPY": "US", "XIC.TO": "CA"}

# Data guard
MAX_FETCH_FAILURE = 0.30

# Momentum window (trading days)
MOM_LOOKBACK = 252
MOM_SKIP = 21
