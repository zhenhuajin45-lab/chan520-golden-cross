# Conditional Randomization Report

## Conclusion Boundary

This evidence tests whether ranked selection adds value after the 2026 hard-pass research gate. It is not a long-term alpha proof and does not tune strategy parameters.

## Data Contract

- SQLite SHA256: `3f4fe7f4c4aefd2a6e6abca98ce9b9a4a081da5c2a62eba4e75feae8ffb7f943`
- Prepared context hash: `ace066960d482e7963b6be31f2aa23d3f4555fb3cfdeda451cf3e7fbfb0b4fba`
- Candidate config hash: `a3dfbba6052ac81ef58a3aa83dcf8f4a956bb258dfabaf6de94c6695f37d8c89`
- Ordered symbol hash: `652ebc7a396f855514e3cdcec1c5b8b83bd5c486b0866e715a368df3f5585240`
- Eligible universe hash: `0b72f7119d6e98d1309d90f0dd75f599f5ed2aa82b73c9b1accde9a42fb51463`
- Sector map hash: `3662d9de0dd2d4faa803d5ff0e247d88f6bcdfbc88c3292353c21be009ef843e`
- Index rows hash: `7d2952c4280997df088b94b0cac6b057d35b19201ca4ea18ae1483ceec774a90`
- Capture: `SUMMARY` / `CORE`

## Ranked vs Random

- Ranked total_return percentile against RANDOM: `0.076000`
- One-sided p(random >= ranked): `0.924000`
- RANDOM median total_return: `0.134514`

## Tie-Bucket Sensitivity

- Ranked total_return percentile against RANDOM_WITHIN_TIES: `0.242000`
- Tie total_return std: `0.059067`
- Unique tie selected sets: `496`

## Runtime

- prepare_seconds: `684.88`
- total_seconds: `1547.04`
- peak_process_rss_mb: `6255.66`
- working_set_peak_mb: `7107.86`
- memory_backend: `psutil 7.2.2`
