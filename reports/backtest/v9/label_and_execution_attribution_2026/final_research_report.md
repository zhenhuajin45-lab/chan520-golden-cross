# V5.2B Label Integrity and Stateful Counterfactual Validation

- Parameter tuning: none.
- Alpha weights, thresholds, MA, ATR, R:R, exit and position parameters were not changed.
- Forward labels use D close signal, D+1 raw open entry reference, and market-calendar holding horizons.
- Incomplete horizons are censored and excluded from return/IC calculations.
- Fixed-20d simplified replay is NOT COMPARABLE TO EXECUTION BACKTEST.
- Simplified replay uses fixed 20d label exit and fixed notional sizing; it is not an execution-faithful portfolio engine.

| Item | Value |
| --- | ---: |
| candidate_labels | 581 |
| complete_20d_labels | 487 |
| random_fixed_20d_simplified_replay_runs | 1000 |
| random_fixed_20d_simplified_median_total_return | 0.478157 |
| tie_break_runs | 500 |
| tie_break_unique_selected_sets | 20 |
