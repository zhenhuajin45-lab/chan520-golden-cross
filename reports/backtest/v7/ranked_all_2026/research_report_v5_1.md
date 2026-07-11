# chan520 v5.1 Alpha Research Report

- Period: `2026-01-01` to `2026-07-09`
- Symbols loaded from SQLite store: `5247`
- Strategy: `strategy_v5_alpha_ranked`
- Execution: close signal, next-session open fill, unadjusted GM prices.
- Data source: durable local SQLite store generated from GM historical caches.

## Summary Metrics

| Metric | Value |
| --- | ---: |
| trade_count | 103.000000 |
| total_return | 0.001747 |
| cagr | 0.003452 |
| max_drawdown | -0.107914 |
| sharpe | 0.110857 |
| calmar | 0.031992 |
| win_rate | 0.349515 |
| payoff_ratio | 1.870365 |
| profit_factor | 1.004972 |

## Alpha Source

- Golden cross is trend confirmation, not the direct entry optimizer.
- Entry requires trend pullback behavior plus renewed upside confirmation.
- Alpha score combines trend structure, relative strength, volume quality, volatility risk, and capped sector heat prior.

## Evidence Files

- `equity_curve_basket_2026-01-01_2026-07-09.csv`
- `trade_records_basket_2026-01-01_2026-07-09.csv`
- `drawdown_report_basket_2026-01-01_2026-07-09.csv`
- `sector_heat_basket_2026-01-01_2026-07-09.csv`
- `candidate_funnel_daily.csv`
- `candidate_selection_audit.csv`
- `signal_snapshots.csv`
- `trade_attribution.csv`
- `yearly_report_basket_2026-01-01_2026-07-09.csv`