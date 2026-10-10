# Factor game — rulebook

**Version 1 · frozen at inception.** Virtual money only. These rules are the whole strategy: the engine in `engine/` implements them exactly, and nothing else decides a trade.

Every parameter below also lives in `engine/config.py`. If the two ever disagree, that is a bug.

## 1. Capital and currency

- Starting capital: **C$30,000** on the inception date.
- Everything is reported in Canadian dollars.

## 2. Universe

- **US:** current members of the S&P 500 (Wikipedia list, read at each run).
- **Canada:** current members of the S&P/TSX Composite (Wikipedia list; falls back to the S&P/TSX 60 if the Composite list can't be read).
- **Excluded sectors:** Financial Services, Utilities, Real Estate. Their accounting ratios don't compare with other companies'.
- **Minimum size:** market cap of at least C$1 billion.
- The two markets are scored separately. A US stock is only ever compared with US stocks, a Canadian one with Canadian stocks.

## 3. Exclusion filters

A stock can't be bought, and a holding is sold, if any of these apply:

| Filter | Rule |
|---|---|
| Distress | Altman Z-score in the bottom 10% of its market's universe |
| Earnings-manipulation risk | Beneish 8-variable M-score above −1.78 |
| Aggressive asset growth | Year-over-year total-asset growth in the top 10% of its market's universe |
| Missing data | No positive enterprise value, fewer than 2 of 4 value inputs, fewer than 2 of 4 quality inputs, or under 12 months of prices |

Missing data only blocks new buys. A holding whose data fails to download or comes back incomplete is **held and flagged**, never sold on a data glitch.

Each M-score component is capped (indices to 0.25–4, total accruals to ±1) so that one tiny denominator can't decide the result. Missing components count as neutral.

## 4. Scores

All scores are percentile ranks within the stock's own market (0 = worst, 1 = best), computed only among stocks that pass the filters.

- **Value** = average rank of:
  - EBIT / enterprise value
  - EBITDA / enterprise value
  - free cash flow / enterprise value
  - shareholder yield = (dividends + net share buybacks + net debt paydown) / market cap
- **Quality** = average rank of:
  - gross profit / total assets
  - operating cash flow / total assets
  - low total debt / total assets
  - low year-to-year variability of return on assets (up to 4 annual reports)
- **Momentum** = total return from 12 months ago to 1 month ago.
- **Composite** = average of value, quality and momentum, re-ranked. Ties are broken by Piotroski F-score.

Income and cash-flow items are trailing 12 months (sum of the last 4 quarters; the latest annual report if the quarters are incomplete). Balance-sheet items are the latest available. Enterprise value = market cap + total debt − cash. Statements reported in a different currency from the share price are converted at the current exchange rate.

## 5. Portfolio

- **30 positions: 20 US and 10 Canadian**, equal-weighted (so roughly two-thirds US, one-third Canada).
- **Sector cap:** at most 5 US positions and 3 Canadian positions in any one sector.
- Fractional shares are allowed.

## 6. Quarterly rebalance — 15th of February, May, August and November

1. Sell a holding if **any** of these is true:
   - its composite rank is below the **65th percentile** (it has dropped out of the top 35%);
   - it trips an exclusion filter;
   - it is no longer in the universe.
2. Fill open slots with the highest-ranked eligible stocks not already held, **only from the top 15%** (85th percentile and up), respecting the sector caps. A slot that can't be filled stays in cash until the next rebalance.
3. Reset every position to equal weight (1/30 of total value). Adjustments smaller than 5% of the target size are skipped.

The gap between the buy threshold (top 15%) and the hold threshold (top 35%) is deliberate. It keeps turnover low and stops the portfolio from trading on noise.

## 7. Monthly check — 15th of every other month

1. Value the portfolio and the benchmark.
2. Sell any holding that:
   - trips the distress or asset-growth filter, judged against the cutoffs recorded at the last rebalance;
   - has an M-score above −1.78; or
   - has stopped trading for 10 or more trading days.
3. Proceeds stay in cash until the next rebalance. **No other trades. A price move is never a reason to sell.**

## 8. Costs and accounting

- **Trading cost:** 0.10% of every trade's value, plus 0.20% for currency conversion on US trades.
- **Returns:** total return, with dividends reinvested through adjusted prices.
- **Currency:** US positions are converted to Canadian dollars at the Bank of Canada daily USD/CAD rate.
- **Not modeled:** cash earns nothing, and taxes are ignored.

## 9. Benchmark

Two-thirds S&P 500 (SPY, converted to CAD) plus one-third S&P/TSX Composite (XIC.TO). Total return, rebalanced monthly, starting at C$30,000 on the inception date.

## 10. Data

- **Sources:**
  - Yahoo Finance via the `yfinance` library: prices and financial statements. It's unofficial and meant for personal research, and its numbers are as currently reported, not as originally published.
  - Wikipedia: index membership lists.
  - Bank of Canada: exchange rates.
- **Failure guard:** if more than 30% of the universe fails to download in a rebalance run, the run stops without trading and logs the failure.

## 11. Changing the rules

The rules are frozen. A change needs a new version number, a date and a reason in the changelog below. It applies from the next run onward and never rewrites past results.

## Changelog

- **v1** — inception. Initial rules.
