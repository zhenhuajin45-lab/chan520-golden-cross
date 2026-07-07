# 缠论 520 金叉本地分析 skill

这是一个本地可执行的 A 股策略分析器，目标是把交易框架中的趋势、520 均线、量价、MACD/RSI、缠论买点和风控纪律转成可复盘的规则。

## 快速运行

```powershell
python -m chan520_skill analyze 600288 --date 2026-07-06
```

部署给其他机器或通过 GitHub 分发时，见 [DEPLOY.md](DEPLOY.md)。

生成报告默认写入：

```text
reports/600288_2026-07-06.md
```

## 数据源

默认 `--source auto`：先使用东方财富历史 K 线接口，失败时自动切换腾讯历史 K 线。

```text
https://push2his.eastmoney.com/api/qt/stock/kline/get
```

腾讯历史和实时接口：

```text
https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
https://qt.gtimg.cn/q=sh600288
```

## 策略蒸馏原则

- 大周期先定方向：60 日涨幅、MA60、周线 MA60、MA250 和均线多头结构。
- 中周期找结构：MA5 上穿 MA20 的 520 金叉、MA5/MA10/MA20/MA60 排列、MACD 是否同步金叉。
- 小周期看量价：放量、突破 MA60、斜率、RSI 健康度。
- 缠论买点做结构补充：一买、二买、三买用可计算的近似条件落地，避免主观画线。
- 风控优先：止损位、盈亏比、仓位等级和确认失败条件必须随报告输出。

本工具只做策略分析和复盘辅助，不构成投资建议。
