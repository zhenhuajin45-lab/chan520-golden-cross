# 本地模拟盘复盘迭代与回溯验证（2026-07-17）

本文记录 2026-07-17 模拟盘复盘问题的实现、回放结果和能力边界，仅用于本地模拟盘研究。

## 已完成修改

- 未完成风险计划跨交易日沿用稳定计划 ID，保留 `armed_at`、`armed_trade_date`、首次确认报价和 `RISK_CONFIRMED` 状态。
- launchd 在 09:30-09:44 每两分钟运行一次开盘循环，每轮先风险卖出、后买入触发。
- 账户存在 `RISK_CANDIDATE` 或 `RISK_CONFIRMED` 时，新增买入以 `ACCOUNT_RISK_EXIT_PENDING` 阻断。
- 两个及以上主要指数跌幅达到 1.5% 时，新增买入以 `BROAD_MARKET_SHOCK_BLOCKED` 阻断。
- 同阶段 90 秒内重复任务返回 `SKIPPED_DUPLICATE`，不覆盖正式摘要；每次正式运行保留独立摘要。
- Codex 定时任务改为只审计 launchd 产物，不再重复调用本地模拟盘执行脚本。
- 观察池计划增加 `level_evidence_status=NOT_COMPUTED_WATCH_ONLY`，并输出具体价格几何阻断码，避免把未计算目标位误判成行情缺失。
- 核心计划增加 `execution_readiness` 和 `buy_entry_ready`，明确区分流程通过与允许新增买入。

## 中兴通讯开盘回放

输入使用 2026-07-17 已记录的模拟盘和分钟报价：成本 39.91 元、2,000 股、前一日峰值浮盈 3.758%、利润保护已武装。

| 路径 | 第一次风控 | 第二次风控/成交 | 成交价 | 触发类型 |
|---|---:|---:|---:|---|
| 旧调度 | 09:36 | 09:36 | 36.82 | 硬止损快速通道 |
| 新调度 | 09:30 确认 | 09:32 | 37.83 | MA5 失效与利润保护二阶段确认 |

新路径成交价改善 1.01 元/股；计入两次卖出费用差异后，模拟账户净现金改善约 2,018.49 元。该结果验证的是执行时点改进，不代表所有开盘跳空都能按分钟价成交。

## 验证结果

- 受影响模块定向测试：29 项通过。
- 全量测试：184 项通过。
- `py_compile`：风险扫描、风险执行、买入触发、每日编排、launchd 安装器和核心计划生成器全部通过。
- 今日计划复跑：`status=PASS`、`execution_readiness=RISK_ONLY`、`buy_entry_ready=false`、可执行买入 0。
- 今日盘中 dry-run：风险扫描、风险执行、买入触发、工作台导出和飞书 dry-run 全部返回 0。
- launchd 已重新加载：周一 09:30/32/34/36/38/40/42/44/51 的开盘及后续时点已写入实际 plist。
- 工作台 `http://127.0.0.1:8768/` 与账户数据接口均返回 HTTP 200。

## 保持关闭的边界

本轮只增强本地 SQLite 模拟盘执行与审计。正式 V5/GM open-close kernel 仍未完成回放、对账、断网、部分成交和异常演练，因此 `auto_open_close_kernel_ready=false`、`gm_adapter_shadow_ready=false`、`shadow_readiness=false` 必须继续保持。
