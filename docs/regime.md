# 市场 Regime 过滤

## 对应评审项

- P0-2：市场级别过滤，避免趋势策略在震荡/下跌市大量误触发。

## 判定口径

默认指数为 `000001` 上证指数。也可指定：

- `399006` 创业板指
- `000300` 沪深300

Regime 使用指数自身日线：

- 收盘价相对 MA20/MA60。
- 60 日涨幅。
- 短期斜率代理。

输出三类：

| regime | 含义 | 交易门控 |
| --- | --- | --- |
| trend_up | 指数站上 MA20/MA60，60日涨幅为正且斜率不弱 | 允许原判定 |
| range | 震荡修复或方向不清 | 降级 |
| down | 指数弱于 MA60 且 60日跌幅较大 | 降级 |

## 接入方式

`analyze` 默认启用 regime：

```powershell
python -m chan520_skill analyze 600288 --date 2026-07-07
```

关闭：

```powershell
python -m chan520_skill analyze 600288 --date 2026-07-07 --no-regime
```

更换指数：

```powershell
python -m chan520_skill analyze 600288 --date 2026-07-07 --regime-index 000300
```

## 降级规则

- `入选` 降为 `观察`。
- `观察*` 降为 `回避/减仓观察`。
- 报告 `defects` 中写入市场状态明细。

该规则偏保守，目的是把材料中“策略必须契合市场风格”的要求落到可执行门控。
