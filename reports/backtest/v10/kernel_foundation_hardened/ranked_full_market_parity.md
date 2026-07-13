# strategy_v5_alpha_ranked Full-Market Golden Parity

- economic_parity: `PASS`
- non_economic_audit_parity: `FAIL`
- frozen_baseline_metric_parity: `PASS`
- selected_set_hash: `8ceddae1552359805d3244d2c49baff35599cc84e46003605aa45ebfcf4be9a9`
- fills_economic_hash: `f537cc15a16722d5feefad0a7ca4a7f7e96a9cd5521fdb16a6a4049b0b420098`
- trades_economic_hash: `dc25d193167b8cdc1ed664ee4e67fd3e15325e73f23aaffef9db8e005b1a835e`

| Metric | Current | Frozen |
|---|---:|---:|
| trades | 103.0 | 103 |
| total_return | 0.001747 | 0.001747 |
| max_drawdown | -0.107914 | -0.107914 |
| sharpe | 0.110857 | 0.110857 |

| Artifact | Formal | Direct Kernel | V8 Frozen | Status |
|---|---|---|---|---|
| `selected_candidates` | `cdac71db6b9cb9622faaa2df5daa45ed307d9cfe995145252bcc425f6bf2a5f4` | `cdac71db6b9cb9622faaa2df5daa45ed307d9cfe995145252bcc425f6bf2a5f4` | `cdac71db6b9cb9622faaa2df5daa45ed307d9cfe995145252bcc425f6bf2a5f4` | PASS |
| `pending_orders` | `309f8acd51364114a2e90887d2e2fd392cf68770e38ba3c91e1375e7a50fe7ab` | `309f8acd51364114a2e90887d2e2fd392cf68770e38ba3c91e1375e7a50fe7ab` | `309f8acd51364114a2e90887d2e2fd392cf68770e38ba3c91e1375e7a50fe7ab` | PASS |
| `economic_fills` | `65949b76089ec2a1f2dd943df060ab24a96a9086471170544e3216d51ca2f44c` | `65949b76089ec2a1f2dd943df060ab24a96a9086471170544e3216d51ca2f44c` | `65949b76089ec2a1f2dd943df060ab24a96a9086471170544e3216d51ca2f44c` | PASS |
| `economic_trades` | `1e31e2586b5dcd903666c15aaff9c68d89fe5a24aad796cb7e5865ab73084581` | `1e31e2586b5dcd903666c15aaff9c68d89fe5a24aad796cb7e5865ab73084581` | `1e31e2586b5dcd903666c15aaff9c68d89fe5a24aad796cb7e5865ab73084581` | PASS |
| `daily_equity` | `b55746bbbca025a599f3028d509b2d482d320b3bb9e125c25e0a19396aca44c4` | `b55746bbbca025a599f3028d509b2d482d320b3bb9e125c25e0a19396aca44c4` | `b55746bbbca025a599f3028d509b2d482d320b3bb9e125c25e0a19396aca44c4` | PASS |
| `funnel` | `bacded5957eb5ec937679485b134edfa802a4e39260a65831daab2f63ed1ae38` | `bacded5957eb5ec937679485b134edfa802a4e39260a65831daab2f63ed1ae38` | `327cae6bad16ee3a497ffcd5360206a0fa8357fac2fdbc1f2f2526eeb32f475f` | FAIL |
| `discipline` | `8d1865ea16ade46a3a3389c41c56bf26d1973b285ab63115aee3c9565ede1bf4` | `8d1865ea16ade46a3a3389c41c56bf26d1973b285ab63115aee3c9565ede1bf4` | `2caf06e43c6a0897d3a1be16d26d0cf906140e0015920b0c4b1e75523b96c50b` | FAIL |
