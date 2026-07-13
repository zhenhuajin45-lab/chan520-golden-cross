# Statistical Validation

- Parameter tuning in this phase: none.
- Future labels are offline research artifacts only.
- Current 2026 result remains retrospective research validation.
- Fixed-20d simplified replay is NOT COMPARABLE TO EXECUTION BACKTEST.
- Hard-pass labels: 581

## V5.2B Closeout

1. Selection overlap Jaccard: 0.241573.
2. First-fit-only vs ranked-only 20d proxy return: 0.044663 vs 0.084584.
3. Simplified replay percentiles against formal first-fit/ranked execution backtests are intentionally not reported.
4. Factor exposure: see first_fit_vs_ranked_features.csv for industry, score, sector heat, opening gap and ex-ante R:R differences.
5. Ranking score IC: see alpha_ic_summary.csv and alpha_quantile_returns.csv; no parameter changes were made.
6. Decile monotonicity: see alpha_monotonicity_report.md; current run is not monotonic for 5d/10d/20d.
7. Tie-break sensitivity: see tie_break_fixed_20d_simplified_replay.csv.
