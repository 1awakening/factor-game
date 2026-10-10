# Factor game

A virtual-money test of a rules-based stock system: rank US and Canadian stocks on **value + quality + momentum**, skip ones that look distressed or likely to be manipulating earnings, hold 30 of the best, and trade only on a fixed schedule.

- **Latest results:** [REPORT.md](REPORT.md)
- **The rules:** [RULES.md](RULES.md) (frozen; changes are versioned)
- **Started with:** C$30,000 of virtual money. Nothing here is real money or investment advice.

## How it runs

GitHub Actions runs `engine/` on the **15th of every month** after the market close:

| Month | What happens |
|---|---|
| Feb, May, Aug, Nov | Full re-ranking of the S&P 500 and S&P/TSX Composite; sells, buys and an equal-weight reset |
| Every other month | Values the portfolio and sells only holdings that trip a distress, manipulation or asset-growth filter |

Each run commits its results back here, so every trade is a dated entry in the history:

| Path | Contents |
|---|---|
| `state/portfolio.json` | Current holdings, cash, benchmark and filter cutoffs |
| `state/nav.csv` | Portfolio and benchmark value after every run |
| `state/trades.csv` | Every trade, with its cost and the rule that triggered it |
| `runs/<date>/` | That run's full scoring tables (`scores_US.csv`, `scores_CA.csv`) or monthly checks, plus a summary |

## Running it by hand

Actions tab → **factor-game** → **Run workflow** → pick a mode. `auto` does whatever the calendar says; `init` starts a new game and refuses to overwrite an existing one.

Locally:

```bash
pip install -r requirements.txt
python -m tests.run_tests                 # offline tests on a synthetic market
python -m engine.run --mode auto          # live run (needs internet)
```

## Data

Prices and financial statements come from Yahoo Finance through the unofficial `yfinance` library, which is meant for personal research. Index membership comes from Wikipedia and USD/CAD rates from the Bank of Canada. Yahoo's statements are as currently reported, not as originally published.
