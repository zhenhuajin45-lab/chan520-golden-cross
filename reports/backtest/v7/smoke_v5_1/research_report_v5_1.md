# chan520 v5.1 Alpha Research Report

- Period: `2026-01-01` to `2026-03-31`
- Symbols loaded from SQLite store: `71`
- Strategy: `strategy_v5_alpha_ranked`
- Execution: close signal, next-session open fill, unadjusted GM prices.
- Data source: durable local SQLite store generated from GM historical caches.

## Summary Metrics

| Metric | Value |
| --- | ---: |
| trade_count | 3.000000 |
| total_return | 0.000067 |
| cagr | 0.000290 |
| max_drawdown | -0.026977 |
| sharpe | 0.034415 |
| calmar | 0.010743 |
| win_rate | 0.666667 |
| payoff_ratio | 0.502158 |
| profit_factor | 1.004315 |

## Alpha Source

- Golden cross is trend confirmation, not the direct entry optimizer.
- Entry requires trend pullback behavior plus renewed upside confirmation.
- Alpha score combines trend structure, relative strength, volume quality, volatility risk, and capped sector heat prior.

## Evidence Files

- `equity_curve_basket_2026-01-01_2026-03-31.csv`
- `trade_records_basket_2026-01-01_2026-03-31.csv`
- `drawdown_report_basket_2026-01-01_2026-03-31.csv`
- `sector_heat_basket_2026-01-01_2026-03-31.csv`
- `candidate_funnel_daily.csv`
- `candidate_selection_audit.csv`
- `signal_snapshots.csv`
- `trade_attribution.csv`
- `yearly_report_basket_2026-01-01_2026-03-31.csv`