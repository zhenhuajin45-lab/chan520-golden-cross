# 风险与仓位纪律

## 默认数字

| 纪律 | 默认值 | 代码位置 |
| --- | ---: | --- |
| 单笔风险 | 1% | `RiskConfig.risk_per_trade` |
| 首仓资金上限 | 15% | `RiskConfig.first_tranche_pct` |
| 单股上限 | 20% | `RiskConfig.max_position_pct` |
| 单行业上限 | 40% | `RiskConfig.max_sector_pct` |
| trend_up 总仓位 | 80% | `RiskConfig.regime_exposure` |
| range 总仓位 | 50% | `RiskConfig.regime_exposure` |
| down 总仓位 | 30% | `RiskConfig.regime_exposure` |
| 最低现金 | 20% | `RiskConfig.cash_reserve_pct` |
| 金字塔分档 | 5% / 15% / 30% | `RiskConfig.pyramid_steps` |
| ATR 移动止损 | 2 * ATR14 | `RiskConfig.atr_k` |
| 移动止损激活 | 盈利 4% 后 | `RiskConfig.trail_activation` |
| 结构目标 ATR | 3 * ATR14 | `RiskConfig.target_atr_k` |
| 时间止损 | 7 天 | `RiskConfig.time_stop_days` |
| 最大回撤熔断 | 15% | `RiskConfig.max_dd_stop` |
| 单日亏损停手 | 1.5% | `RiskConfig.daily_loss_stop` |
| 单周亏损减半 | 4% | `RiskConfig.weekly_loss_stop` |

## 建仓公式

```text
shares = floor(equity * risk_per_trade / (entry - stop) / lot) * lot
```

之后再同时套用首仓资金上限、单股、单行业、总仓位、现金保留和整手约束。第三轮已删除“不足 1 手则兜底买 1 手”的路径；目标规模不足 1 手时直接跳过并计数。

## 金字塔

加仓只允许在已有盈利、价格高于当前止损、趋势未破时触发。当前实现支持：

- 突破近 20 日新高且温和放量。
- 回踩 5/10/20 日线附近止跌并放量收回。
- 下跌不补仓，止损下方不加仓。

`pyramid_steps=(0.05,0.15,0.30)` 作为后续加仓增量资金约束，`pyramid_stage` 随加仓递进。

## 四不做

默认 `entry_tier=standard`，只接受标准入选信号。入场前统一检查：

- 可做可不做不做：弱信号不入场。
- 急涨急跌不做。
- 止损距离超过 12% 不做。
- R:R < 2 不做。

## 卖出纪律

组合回测中触发以下任一项则 D+1 计划卖出：

- 价格跌破移动止损。
- 入场 7 天后仍未明显离开成本区。
- 持仓后连续 3 个 bar 不创新高。
- 跌破 MA20。

移动止损在盈利达到 `trail_activation=4%` 前不抬到成本价；激活后才使用 `MA10` 与 `close - 2*ATR14` 上移。
