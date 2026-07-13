# strategy_v5_alpha_first_fit_frozen Full-Market Golden Parity

- economic_parity: `PASS`
- non_economic_audit_parity: `FAIL`
- frozen_baseline_metric_parity: `PASS`
- selected_set_hash: `3292063095b510bc2d8ef2ccb2618a3291304901bb717a320dd63156198712b0`
- fills_economic_hash: `3d3e4cd0ba4c9f9e5f962eeaccf0f3a6ecacf8920f1b53721ff0cebffdbe7a9f`
- trades_economic_hash: `cb61861b617a4a79dd932e372d167d1d363054a518364cf1784e2b52f4e6012f`

| Metric | Current | Frozen |
|---|---:|---:|
| trades | 105.0 | 105 |
| total_return | 0.206239 | 0.206239 |
| max_drawdown | -0.087793 | -0.087793 |
| sharpe | 1.940947 | 1.940947 |

| Artifact | Formal | Direct Kernel | V8 Frozen | Status |
|---|---|---|---|---|
| `selected_candidates` | `8313c9124ea1e49f9e21d26dc3ece2f4c3c405aaec6023a388c259ee3b423e6b` | `8313c9124ea1e49f9e21d26dc3ece2f4c3c405aaec6023a388c259ee3b423e6b` | `8313c9124ea1e49f9e21d26dc3ece2f4c3c405aaec6023a388c259ee3b423e6b` | PASS |
| `pending_orders` | `783147a52d1552e0f659a3b27caf93976627f2539061f6c3c58211358eeb1a97` | `783147a52d1552e0f659a3b27caf93976627f2539061f6c3c58211358eeb1a97` | `783147a52d1552e0f659a3b27caf93976627f2539061f6c3c58211358eeb1a97` | PASS |
| `economic_fills` | `8a96289ca43a8d2809a9c7596045cf1fc163841b9edee55b34f79bc5381081bc` | `8a96289ca43a8d2809a9c7596045cf1fc163841b9edee55b34f79bc5381081bc` | `8a96289ca43a8d2809a9c7596045cf1fc163841b9edee55b34f79bc5381081bc` | PASS |
| `economic_trades` | `f17e1b6531b927778a06b1f853edfd810dbc3eb8c5e2573219b3fc89a584730b` | `f17e1b6531b927778a06b1f853edfd810dbc3eb8c5e2573219b3fc89a584730b` | `f17e1b6531b927778a06b1f853edfd810dbc3eb8c5e2573219b3fc89a584730b` | PASS |
| `daily_equity` | `3492ad46be52ae166514109d6816dfc57806faac9f074bdd8337043575a3904c` | `3492ad46be52ae166514109d6816dfc57806faac9f074bdd8337043575a3904c` | `3492ad46be52ae166514109d6816dfc57806faac9f074bdd8337043575a3904c` | PASS |
| `funnel` | `15c310f0db883a11fea46edafa5fb747337289c7441459b4f32bd67963f5401e` | `15c310f0db883a11fea46edafa5fb747337289c7441459b4f32bd67963f5401e` | `5613d82ca1ecb62b0aa6f9350f67c58e3bbbb0b48d3c668dfbaa2dc3126a86d2` | FAIL |
| `discipline` | `67c6e3e0f4a18945faea36ce78539a272297bb09a9e9a61052f60861d16775d4` | `67c6e3e0f4a18945faea36ce78539a272297bb09a9e9a61052f60861d16775d4` | `8ee74ab93c43730480aa18ef4581836f0a091eada20e6fc1626aacc41a1a5d5e` | FAIL |
