# Controlled Comparison 2026

| Variant | Selection Policy | Trades | Total Return | Max DD | Sharpe | Win Rate | Payoff | Supported |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| frozen_v5_2026 | input-order first-fit | 105 | 20.62% | -8.78% | 1.94 | 40.00% | 2.18 | no |
| ranked_all_2026 | deterministic ranked candidates | 103 | 0.17% | -10.79% | 0.11 | 34.95% | 1.87 | no |

The comparison is intentionally not interpreted as parameter optimization. It isolates the selection policy change. Removing input-order bias materially reduced 2026 performance on this snapshot, which means the frozen V5 result likely contained order-selection luck. The ranked result is therefore the more credible research baseline, even though it is weaker.
