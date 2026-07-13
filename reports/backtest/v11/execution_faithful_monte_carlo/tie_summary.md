# RANDOM_WITHIN_TIES Monte Carlo Summary

- Scope: conditional randomization over candidates that already pass the 2026 hard research gate.
- Execution: same prepared context, T+1 open fills, raw execution bars, SUMMARY capture, CORE metrics.
- Parameter tuning: none.

| Metric | Value |
|---|---:|
| seeds | 500 |
| unique selected sets | 496 |
| unique economic trade sets | 498 |
| avg selected overlap with ranked | 0.816495 |
| ranked total_return | 0.001747 |
| ranked percentile | 0.242000 |
| ranked one-sided p(random >= ranked) | 0.758000 |
| first-fit total_return | 0.206239 |
| first-fit percentile | 1.000000 |
| first-fit one-sided p(random >= first-fit) | 0.000000 |

| Distribution | min | p05 | median | mean | p95 | max | std |
|---|---:|---:|---:|---:|---:|---:|---:|
| total_return | -0.072553 | -0.028321 | 0.055140 | 0.055559 | 0.140790 | 0.181402 | 0.059067 |
| sharpe | -0.802572 | -0.248161 | 0.716111 | 0.697522 | 1.622251 | 2.103909 | 0.649775 |
| max_drawdown | -0.173882 | -0.135007 | -0.102653 | -0.105550 | -0.084964 | -0.083401 | 0.015879 |
| trade_count | 98.000000 | 100.000000 | 103.000000 | 103.164000 | 106.000000 | 109.000000 | 1.934193 |
