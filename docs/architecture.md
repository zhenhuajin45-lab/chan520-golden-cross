# 架构说明

## 当前路径

当前仓库仍以本地研究和复盘为目标，没有实盘下单路径。

主要执行路径：

```text
CLI backtest -> run_backtest -> backtest_symbols -> portfolio_backtest_symbols -> _write_trades/_write_metrics
```

最终副作用只有写入 `reports/backtest/**` 下的 CSV 和 Markdown。

## 第三轮同源改造

第三轮已删除组合回测专用 `_portfolio_signal` 作为入场来源，组合入场改为真实 `analyze()` 的 `verdict == "入选"`。为了性能，只先做必要条件过滤；过滤不可能把真实“入选”过滤掉，因为标准入选必须具备近 3 日 520 金叉、站上 MA5/MA20、MACD 零轴上金叉、60 日涨幅、量能与不过热条件。

尚未完成工程规范中的完整 `decide()` 纯函数和 `PaperRunner`。当前结论只覆盖回测研究路径，不声明生产执行就绪。
