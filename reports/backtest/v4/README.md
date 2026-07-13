# V4 Backtest Evidence

## Scope

This evidence uses `universe_snapshot_fixed_30_2024-07-01.csv`: a manually audited, fixed 30-stock research basket with a point-in-time start date and industry mapping. It is **not** a complete historical A-share universe, does not contain rolling constituent changes, and must not be used to claim full-market or production performance.

Period: `2024-07-01` to `2026-07-01`. Split date: `2025-07-01`. Signal and execution adjustments: `none`. Fill: next-session open with 5 bps slippage and configured A-share fees.

## Reproduction

Default V4 discipline (`R:R >= 2`):

```powershell
python -m chan520_skill backtest --universe-snapshot reports/backtest/v4/universe_snapshot_fixed_30_2024-07-01.csv --start 2024-07-01 --end 2026-07-01 --split-date 2025-07-01 --output-dir reports/backtest/v4
```

Sensitivity only, not the default strategy:

```powershell
python scripts/run_v4_rr_sensitivity.py --snapshot reports/backtest/v4/universe_snapshot_fixed_30_2024-07-01.csv --start 2024-07-01 --end 2026-07-01 --split-date 2025-07-01 --min-rr 1.5 --output-dir reports/backtest/v4/sensitivity_rr15
python scripts/run_v4_rr_sensitivity.py --snapshot reports/backtest/v4/universe_snapshot_fixed_30_2024-07-01.csv --start 2024-07-01 --end 2026-07-01 --split-date 2025-07-01 --min-rr 1.0 --output-dir reports/backtest/v4/sensitivity_rr10
```

## Results

| Variant | Standard 520 | Regime rejected | Four-no rejected | Trades | Total return | OOS trades | Conclusion |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Default `R:R >= 2` | 27 | 11 | 16, all insufficient R:R | 0 | 0.00% | 0 | No execution sample; do not infer EV. |
| Sensitivity `R:R >= 1.5` | 26 | 11 | 11, all insufficient R:R | 4 | +0.38% | 4 | Low sample; CI crosses zero. |
| Sensitivity `R:R >= 1.0` | 26 | 11 | 11, all insufficient R:R | 4 | +0.38% | 4 | Identical filled set to 1.5 in this basket. |

The 1.5 sensitivity has expectancy `+95.95` currency units, win rate `50%`, payoff ratio `2.43`, profit factor `2.43`, and bootstrap mean CI `[-190.03, 459.36]`. All four trades are OOS, so the result is not statistically sufficient and does not justify changing the default 2:1 rule.

The default result identifies the research question for the next iteration: validate whether the conservative first-target model is too restrictive using a large, rolling historical universe, not by lowering the default threshold after seeing this basket.
