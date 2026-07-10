# 缠论 520 金叉本地分析 skill

这是一个本地可执行的 A 股策略分析器，目标是把交易框架中的趋势、520 均线、量价、MACD/RSI、缠论买点和风控纪律转成可复盘的规则。

## 快速运行

```powershell
python -m chan520_skill analyze 600288 --date 2026-07-06
```

如需按同花顺截图常见的 `MACD(12,50,9)` 口径核对：

```powershell
python -m chan520_skill analyze 600288 --date 2026-07-07 --macd-slow 50
```

事件驱动回测：

```powershell
python -m chan520_skill backtest 600288 --start 2026-01-01 --end 2026-07-01 --split-date 2026-04-01
python -m chan520_skill backtest --basket "600288,300568,688106,002132,603638,301282,600203,300390" --start 2026-01-01 --end 2026-07-01 --split-date 2026-04-01
```

`backtest` 默认使用 V4 共享资金账户引擎：D 日收盘生成信号，D+1 开盘只使用 D 日已知状态成交；默认信号和成交均使用不复权价格，避免端点前复权引入历史信息。引擎支持注入单独审计过的信号序列，但成交、现金、佣金和整手始终使用不复权价格。默认风险预算 1%、首仓 15% 加一次 5%、单股 20%、行业 40%、现金保留 20%、四不做、保守目标位和延迟移动止损。当前板块门控默认关闭，仅保留行业 cap。

历史全市场回测必须传入时点股票池快照，禁止用当前股票列表替代历史股票池：

```powershell
python -m chan520_skill backtest --universe-snapshot data/universe_2024-01-01.csv --start 2024-01-01 --end 2026-07-01
```

部署给其他机器或通过 GitHub 分发时，见 [DEPLOY.md](DEPLOY.md)。

生成报告默认写入：

```text
reports/600288_2026-07-06.md
```

复跑旧观察池：

```powershell
python scripts/rescan_observe_candidates.py
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
- 实战优先：520 金叉后还要区分当天买点、近3日确认、涨停追高、5日线持有、回踩加仓和失败退出。

本工具只做策略分析和复盘辅助，不构成投资建议。

完整实战版规则见 [docs/practical_520_strategy.md](docs/practical_520_strategy.md)。
回测、市场状态、行业层、风险纪律、架构和测试说明见 [docs/backtest.md](docs/backtest.md)、[docs/regime.md](docs/regime.md)、[docs/sector.md](docs/sector.md)、[docs/risk.md](docs/risk.md)、[docs/architecture.md](docs/architecture.md)、[docs/testing.md](docs/testing.md)。
V4 固定研究篮子回测、敏感性实验和解释见 [reports/backtest/v4/README.md](reports/backtest/v4/README.md)。
