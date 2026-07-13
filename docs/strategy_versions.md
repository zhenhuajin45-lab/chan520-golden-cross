# Strategy Versions

The backtest keeps the pre-refactor rules as `strategy_v1_baseline` and adds
an explicit modular research path as `strategy_v2_modular`. The default remains
the baseline so historical V4 results remain comparable.

```powershell
python -m chan520_skill backtest --universe-snapshot reports/backtest/v4/universe_snapshot_fixed_30_2024-07-01.csv --start 2024-07-01 --end 2026-07-01 --strategy strategy_v1_baseline
python -m chan520_skill backtest --universe-snapshot reports/backtest/v4/universe_snapshot_fixed_30_2024-07-01.csv --start 2024-07-01 --end 2026-07-01 --strategy strategy_v2_modular
```

The modular path scores trend, pullback entry, momentum, volume, market state,
and risk penalty separately. It uses `BULL/NORMAL/BEAR` states based on the
index close versus `MA60` and the `MA60/MA120` relationship. Both versions use
the same D+1 execution engine, costs, position caps, and four-no/R:R gate.

Metrics reports include `calmar` and a calendar-year table with trades, return,
maximum drawdown, and win rate.

## GM local backtest

The optional GM adapter reads credentials only from `GM_TOKEN` and never writes
them to repository files. A 2026 HS300 run can be reproduced locally with:

```powershell
$env:GM_TOKEN = "<your-gm-token>"
python scripts/run_gm_backtest.py --universe hs300 --start 2026-01-05 --end 2026-07-09 --strategy strategy_v2_modular --output-dir reports/backtest/gm/2026_v3
```

The runner writes a point-in-time universe snapshot plus `trades`, `fills`, and
`metrics`. The local GM pickle cache is ignored by Git and is only for fast
reruns via `--from-cache`.
