# CHANGELOG

## 2026-07-08 - Review v1 iteration

### P0-1 回测与期望值

- 新增 `chan520_skill/backtest.py`，实现事件驱动 D+1 回测。
- 支持 `next_open` 和 `next_vwap_proxy` 成交口径。
- 建模佣金、最低佣金、卖出印花税、沪市过户费、双边滑点。
- 输出 trades CSV 和 metrics Markdown，指标包含 total_return、CAGR、expectancy、win_rate、payoff_ratio、profit_factor、max_drawdown、Sharpe、trade_count、avg_holding_days、exposure、IS/OOS 交易数和期望值。
- 新增 `python -m chan520_skill backtest` CLI。
- 新增 `tests/test_backtest.py` 覆盖无前视、涨停拒买、成本核算、期望值/胜率/盈亏比。

### P0-2 市场 regime

- 新增 `chan520_skill/regime.py`，支持上证指数、创业板指、沪深300 regime 计算。
- `analyze` 默认接入 regime，`--no-regime` 可关闭，`--regime-index` 可切换指数。
- 当 regime 不通过时，入选/观察结论降级，并在 defects 标注原因。
- 新增 `tests/test_regime.py` 覆盖 trend_up/range/down 和 verdict 降级。

### P1-1 A 股微观结构

- 新增 `chan520_skill/microstructure.py`，按主板、创业板、科创板、北交所、ST 识别涨跌停阈值。
- 回测买入日涨停封板跳过买入，卖出日跌停封板顺延。
- `analyze` 新增 `--min-history`，历史数据不足时显式加入次新/样本不足缺陷。
- 新增 `tests/test_microstructure.py` 和 `tests/test_strategy_guards.py`。

### P1-2 数据质量与覆盖率

- 新增 `chan520_skill/quality.py`，检查最少 bar 数、日期递增/重复、OHLC 合法性。
- `analyze` 入口调用数据质量校验。
- `scanner` 增加 success/failed/suspended/insufficient/coverage_below_threshold 统计。
- `scanner` 增加腾讯历史失败后回退 `auto_history`。
- `powershell` 网络兜底仅在 Windows 调用。
- 新增 `tests/test_data_quality.py`。

### P2 口径与可维护性

- `volume_ratio` 保留兼容，同时新增 `volume_expansion` 口径；默认 `prior_only`，即今日量 / 前5日均量。
- `analyze` 新增 `--vol-base {prior_only,incl_today}`。
- `RuleResult` 新增结构化 `tags`，追高/扩展状态不再依赖中文文案子串。
- `_stop_hint` 新增 `--stop-mode {pct,atr}` 和 `--atr-k`，ATR 止损开始落地。
- 回测默认使用后复权历史数据，实时分析仍保持前复权默认。

### 文档与证据

- 新增 `docs/backtest.md`、`docs/regime.md`、`docs/microstructure.md`。
- 更新 `README.md` 和 `DEPLOY.md`。
- 提交回测证据：
  - `reports/backtest/trades_600288_2026-01-01_2026-07-01.csv`
  - `reports/backtest/metrics_600288_2026-01-01_2026-07-01.md`
  - `reports/backtest/trades_basket_2026-01-01_2026-07-01.csv`
  - `reports/backtest/metrics_basket_2026-01-01_2026-07-01.md`

### 测试

- `python -m pytest -q`：15 passed。
- `python -m compileall -q chan520_skill scripts tests`：通过。

### 已知限制

- 全市场 100 只、近 2 年组合回测证据本轮未提交。原因是当前回测直接逐日复用完整 `analyze`，性能偏慢；已先提交 8 只跨主板/创业板/科创板的小篮子证据。下一轮应把指标预计算、信号函数纯化后再跑大样本。
- 组合层 equity curve 当前用交易退出日累计净值近似，非逐日组合市值曲线。
- `scan_market` 已增加覆盖率统计字段，但 `suspended/insufficient` 细分仍较粗，后续应把异常类型从 `_scan_one` 显式返回。
- 评分权重仍是启发式常量，尚未完成独立 `scoring.py` 参数化与统计标定。
- 本项目仍定位为筛选、复盘和评审系统；回测证据显示当前样本期篮子期望值为负，不应直接用于实盘下单。
