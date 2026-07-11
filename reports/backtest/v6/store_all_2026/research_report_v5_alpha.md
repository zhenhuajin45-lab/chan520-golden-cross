# chan520 v5 Alpha Research Report

- Period: `2026-01-01` to `2026-07-09`
- Symbols loaded from SQLite store: `5247`
- Strategy: `strategy_v5_alpha`
- Execution: close signal, next-session open fill, unadjusted GM prices.
- Data source: durable local SQLite store generated from GM historical caches.

## Summary Metrics

| Metric | Value |
| --- | ---: |
| trade_count | 105.000000 |
| total_return | 0.206239 |
| cagr | 0.448025 |
| max_drawdown | -0.087793 |
| sharpe | 1.940947 |
| calmar | 5.103173 |
| win_rate | 0.400000 |
| payoff_ratio | 2.183245 |
| profit_factor | 1.455497 |

## Alpha Source

- Golden cross is trend confirmation, not the direct entry optimizer.
- Entry requires trend pullback behavior plus renewed upside confirmation.
- Alpha score combines trend structure, relative strength, volume quality, volatility risk, and capped sector heat prior.

## Evidence Files

- `equity_curve_basket_2026-01-01_2026-07-09.csv`
- `trade_records_basket_2026-01-01_2026-07-09.csv`
- `drawdown_report_basket_2026-01-01_2026-07-09.csv`
- `sector_heat_basket_2026-01-01_2026-07-09.csv`
- `yearly_report_basket_2026-01-01_2026-07-09.csv`