# chan520 V5.1 Selection Credibility Report

## Scope

V5.1 fixes research credibility issues rather than tuning strategy parameters.

- Candidate selection now collects all daily hard-pass signals before ranking.
- Ranking is deterministic: `alpha_total + sector_bonus`, then `entry_score`, then `relative_strength_score`, then code ascending.
- The old input-order first-fit behavior is frozen under `strategy_v5_alpha_first_fit_frozen`.
- Statistical support now requires trade count, independent clusters, positive CI lower bound, and enough years.

## Evidence

- Frozen first-fit baseline: `reports/backtest/v7/frozen_v5_2026/`
- Ranked smoke validation: `reports/backtest/v7/smoke_v5_1/`
- Controlled comparison: `reports/backtest/v7/controlled_comparison_2026.md`
- New evidence files: `candidate_funnel_daily.csv`, `candidate_selection_audit.csv`, `signal_snapshots.csv`, `trade_attribution.csv`, `alpha_decile_analysis.csv`, `alpha_ic_report.md`, `sector_data_audit.md`.

## Full 2026 Ranked Result

The full ranked run uses the local SQLite GM store, all available non-ST dynamic-universe A-share symbols, `2026-01-01` to `2026-07-09`.

- Usable symbols: 5,247.
- Trades: 103.
- Total return: 0.17%.
- CAGR: 0.35%.
- Max drawdown: -10.79%.
- Sharpe: 0.11.
- Win rate: 34.95%.
- Payoff ratio: 1.87.
- Profit factor: 1.00.
- Candidate hard-pass count: 32,309.
- Selected candidate count: 109.
- Capacity rejections: 19,236.
- Statistical support: false.

Interpretation: V5.1 removes input-order selection bias and produces enough trades to pass the raw `trade_count >= 100` gate, but the result is not a validated Alpha claim because the CI95 expectancy lower bound is negative and independent clusters are only 25.

Compared with the frozen first-fit baseline, ranked selection reduces 2026 total return from 20.62% to 0.17%. This is a credibility repair: it shows the older result was materially dependent on input-order selection luck.

## Smoke Result

The smoke run uses the local SQLite GM store, `2026-01-01` to `2026-03-31`, `--max-symbols 80`.

- Loaded bars after SQL pushdown: 46,661 rows.
- Usable symbols after history filter: 71.
- Trades: 3.
- CAGR: 0.03%.
- Max drawdown: -2.70%.
- Statistical support: false.

This is a pipeline validation only, not a performance claim.

## Next Required Work

Compare `ranked_all_2026` against `frozen_v5_2026` in a dedicated comparison script and improve the entry/risk model only after attribution shows where expectancy is lost.

```powershell
$env:PYTHONPATH='.'
python scripts\gm_alpha_store.py run --store data\gm_alpha\chan520_alpha.sqlite --start 2026-01-01 --end 2026-07-09 --output-dir reports\backtest\v7\ranked_all_2026
```
