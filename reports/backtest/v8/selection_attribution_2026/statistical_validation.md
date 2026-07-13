# Statistical Validation

- Parameter tuning in this phase: none.
- Future labels are offline research artifacts only.
- Current 2026 result remains retrospective research validation.
- Hard-pass labels: 581

## V5.2A Answers

1. Selection overlap Jaccard: 0.241573.
2. First-fit-only vs ranked-only 20d proxy return: 0.032510 vs 0.062751.
3. First-fit random percentile: 0.832000; ranked percentile: 1.000000.
4. Factor exposure: see first_fit_vs_ranked_features.csv for industry, score, sector heat, opening gap and ex-ante R:R differences.
5. Ranking score IC: see alpha_ic_5d.csv, alpha_ic_10d.csv and alpha_ic_20d.csv; no parameter changes were made.
6. Decile monotonicity: see alpha_monotonicity_report.md; current run is not monotonic for 5d/10d/20d.
7. Tie-break sensitivity: see tie_bucket_randomization.csv; effective_ranking_resolution records whether score resolution is insufficient.
