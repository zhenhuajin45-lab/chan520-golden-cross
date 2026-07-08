# 风险与仓位纪律

## 默认数字

| 纪律 | 默认值 | 代码位置 |
| --- | ---: | --- |
| 单笔风险 | 1% | `RiskConfig.risk_per_trade` |
| 单股上限 | 20% | `RiskConfig.max_position_pct` |
| 单行业上限 | 40% | `RiskConfig.max_sector_pct` |
| trend_up 总仓位 | 80% | `RiskConfig.regime_exposure` |
| range 总仓位 | 50% | `RiskConfig.regime_exposure` |
| down 总仓位 | 30% | `RiskConfig.regime_exposure` |
| 最低现金 | 20% | `RiskConfig.cash_reserve_pct` |
| 金字塔分档 | 5% / 15% / 30% | `RiskConfig.pyramid_steps` |
| ATR 移动止损 | 2 * ATR14 | `RiskConfig.atr_k` |
| 时间止损 | 7 天 | `RiskConfig.time_stop_days` |
| 最大回撤熔断 | 15% | `RiskConfig.max_dd_stop` |
| 单日亏损停手 | 1.5% | `RiskConfig.daily_loss_stop` |
| 单周亏损减半 | 4% | `RiskConfig.weekly_loss_stop` |

## 建仓公式

```text
shares = floor(equity * risk_per_trade / (entry - stop) / lot) * lot
```

之后再同时套用单股、单行业、总仓位、现金保留和金字塔分档约束。

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
