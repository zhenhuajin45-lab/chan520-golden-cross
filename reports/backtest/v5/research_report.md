# Chan520 V5 Research Report

## Scope

- Fixed-snapshot data: the existing fixed 30-stock snapshot `universe_snapshot_fixed_30_2024-07-01.csv`.
- GM data: point-in-time HS300 constituents from 掘金 at `2026-01-05`, filtered for non-ST, not suspended, listed, not delisted, and industry-mapped.
- Fixed-snapshot period: `2024-07-01` to `2026-07-01`.
- GM period: `2026-01-05` to `2026-07-09`.
- Fixed-snapshot split: `2025-07-01`; GM split: `2026-04-01`.
- Execution: D close signal, D+1 open fill, unadjusted execution prices, 5 bps slippage, configured A-share fees.
- This is a research comparison, not a full-market or production-performance claim. The GM run is broader than the 30-stock smoke set but is still HS300, not full A-share.

## Version Comparison

| Version | Universe | Trades | Total Return | CAGR | Max Drawdown | Sharpe | Calmar | Conclusion |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `strategy_v1_baseline` | 30 stocks | 0 | 0.00% | 0.00% | 0.00% | 0.00 | 0.00 | No execution sample |
| `strategy_v2_modular` | 600288 only | 1 | 0.80% | 0.40% | -0.74% | 0.41 | 0.54 | Positive but statistically unusable |
| `strategy_v2_modular` | GM HS300, 298 stocks | 79 | 7.40% | 15.13% | -7.01% | 0.90 | 2.16 | Broader 2026 sample, still below sample and win-rate gates |

The baseline result is available at
`reports/backtest/v5/baseline/metrics_basket_2024-07-01_2026-07-01.md`.
The modular single-stock smoke backtest is available at
`reports/backtest/v5/modular_600288/metrics_600288_2024-07-01_2026-07-01.md`.
The GM HS300 2026 run is available at
`reports/backtest/gm/2026_v3/hs300/metrics_basket_2026-01-05_2026-07-09.md`.

## GM HS300 2026 Result

The local GM adapter fetched and cached 298 eligible HS300 constituents as of
`2026-01-05`, then ran the local causal portfolio engine from cache without
persisting credentials. The raw GM pickle cache is intentionally ignored by
Git; the committed evidence is the universe snapshot, trades, fills, and
metrics.

Key metrics:

- Trades: `79`; fills: `171`; average holding days: `7.37`.
- Total return: `7.40%`; CAGR: `15.13%`; Sharpe: `0.90`; Calmar: `2.16`.
- Max drawdown: `-7.01%`; average exposure: `31.50%`; max exposure: `80.70%`.
- Win rate: `41.77%`; payoff ratio: `1.83`; profit factor: `1.31`.
- Expectancy: `93.63`, but 95% bootstrap CI is `[-110.77, 303.07]`, so the expectancy is not statistically stable.
- In-sample/OOS split at `2026-04-01`: `40` / `39` trades; OOS expectancy dropped from `157.73` to `27.89`.

Acceptance status against the V4/V5 targets:

| Gate | Target | GM HS300 2026 | Status |
| --- | ---: | ---: | --- |
| Trade count | `>300` | `79` | FAIL |
| Win rate | `>45%` | `41.77%` | FAIL |
| Payoff ratio | `>1.3` | `1.83` | PASS |
| Max drawdown | `<25%` | `7.01%` | PASS |
| Causal execution | D close signal, D+1 open fill | implemented | PASS |
| Data breadth | more than 30 stocks | 298 eligible HS300 names | PASS |

The result is useful because it proves the framework can leave the 30-stock
toy basket and produce a real 2026 cross-section. It is not yet an accepted
strategy because trade count and win rate still miss the stated gates.

## Modular Signal

The modular strategy produced 172 candidate decisions for 600288, with one
trade surviving the market, risk, and R:R gates:

- Entry: `2025-10-28`, price `15.46773`, 900 shares.
- Exit: `2025-11-07`, price `16.37181`, `three_bars_no_high`.
- Net PnL: `796.01813`.
- Entry R:R: `2.752608`.

## Annual Statistics

The metrics writer now emits a calendar-year table with `trades`, `return`,
`max_drawdown`, and `win_rate`. The modular smoke run had one 2025 trade and
zero trades in 2024 and 2026. The baseline had zero trades in all three years.

## Limitations

1. The fixed snapshot still has survivor-bias and historical metadata limits.
2. The modular 30-stock run was not accepted because the current full-basket
   execution path exceeded the practical runtime window; no partial result is
   reported as a full-basket result.
3. One trade cannot validate expectancy, win rate, payoff, or drawdown targets.
4. The GM HS300 run uses point-in-time index membership on the start date; it is
   broader than the 30-stock set but not a fully dynamic all-A universe.
5. Dynamic all-A universe, indicator-cache performance, and walk-forward testing
   remain the next required engineering stages.
