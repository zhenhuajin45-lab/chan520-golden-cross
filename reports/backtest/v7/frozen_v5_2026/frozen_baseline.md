# Frozen V5 First-Fit Baseline

This directory freezes the pre-V5.1 first-fit evidence used as the control baseline.

- Strategy mode: `strategy_v5_alpha_first_fit_frozen`
- Selection policy: input-order first-fit; kept only for controlled comparison.
- Period: `2026-01-01` to `2026-07-09`
- Source: `reports/backtest/v6/store_all_2026`

V5.1 ranked selection must be compared against this baseline on the same local SQLite snapshot before making performance claims.
