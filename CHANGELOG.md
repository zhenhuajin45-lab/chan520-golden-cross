# CHANGELOG

## 2026-07-08 - Review v3 credible backtest iteration

### 总结

第三轮目标是让回测成为公平检验，而不是让 EV 好看。本轮修复仓位塌缩、真实大盘层、真实信号、板块层退化、移动止损过紧和统计口径不足的问题。修复后 30 只、2 年样本只产生 3 笔标准入选交易，样本不足 100 笔，因此不对策略 EV 下结论。

### M 仓位规模

- `allowed_new_position_shares` 删除“不足 1 手兜底买 1 手”路径。
- 首仓现在取风险预算、首仓资金上限、单股 cap、行业 cap、总仓位 cap、现金保留的最小整手。
- 新增 `RiskConfig.first_tranche_pct=0.15`。
- 新增 `tests/test_sizing_realized_risk.py`。

### R 止损与目标位

- `trailing_stop` 增加 `trail_activation=4%`，盈利未达到阈值前不抬到成本价。
- 目标位改为结构化目标：前摆动高、量度升幅、`entry + 3*ATR` 中较高者。
- 更新退出纪律测试。

### N 金字塔加仓

- 组合回测支持 `add` 加仓单。
- 已盈利、趋势未破、突破新高或回踩均线止跌时才允许加仓。
- 加仓会更新持仓均价、股数、止损和 `pyramid_stage`。
- 新增 `tests/test_pyramiding.py`。

### O 真实指数 Regime

- 组合回测默认使用 `000300` 沪深300指数 regime。
- 停用篮子宽度作为大盘层门控来源。
- 指数历史优先东方财富，失败回退腾讯指数日线。
- 新增 `--regime-index`。
- 新增 `tests/test_backtest_regime.py`。

### P 板块层收窄

- 未接入真实行业成分前，组合回测默认 `use_sector=False`，不再宣称完整三层共振。
- 行业映射仍用于单行业 40% 风险 cap。
- `--use-sector` 可显式启用当前代理行业宽度门控。

### Q 真实信号

- 组合回测入场判定改为真实 `analyze()` 的 `verdict == "入选"`。
- `_portfolio_signal` 不再作为入场来源。
- 为性能仅对标准 520 必要条件日期调用完整 `analyze()`；该预过滤不会改变“入选”集合。

### S 不变量测试

- 新增组合级无前视测试：篡改中间日之后 K 线，已发生入场字段不变。
- cap 测试新增可压到 cap 的场景。
- 涨停拒买、风险预算、加仓和指数 regime 均有离线测试。

### T 新证据

- 新增 `reports/backtest/v3/trades_basket_2024-07-01_2026-07-01.csv`。
- 新增 `reports/backtest/v3/metrics_basket_2024-07-01_2026-07-01.md`。
- 新 metrics 包含 `trimmed_expectancy_5pct`、`median_trade_pnl`、`expectancy_ci95_low/high`、`sample_sufficient`、`avg_entry_risk_pct`、`max_entry_risk_pct`、`pyramid_adds`。
- 第三轮证据：30 只、2024-07-01 至 2026-07-01、交易 3 笔、`expectancy=-132.95`、`sample_sufficient=0`。样本不足，不下 EV 结论。
- 第二轮 15 只证据保留为历史文件，但因仓位塌缩为 1 手，已废弃为策略判断依据。

### 工程文档

- 新增 [docs/architecture.md](docs/architecture.md)。
- 新增 [docs/testing.md](docs/testing.md)。
- 新增 [assumptions.md](assumptions.md)。

### 已知限制

- 尚未完成完整 `decide()` 纯函数与 `PaperRunner` 双运行器。
- 尚未提交离线 30/100 股票真实快照夹具；v3 证据仍依赖公开接口实时拉取。
- 真实行业成分和行业指数未接入，板块门控默认关闭。
- 30 只样本只有 3 笔标准交易，必须扩大池子或单独研究观察级信号，才能评估统计边际。

## 2026-07-08 - Review v2 discipline iteration

### 核心目标

第二轮评审指出第一版虽然有事件驱动和基础风控，但仍偏“简易版策略”。本轮把 520 策略升级为共享账户、行业共振、风险预算和退出纪律共同约束的实战版组合回测框架。

### 评审矩阵

| 评审项 | 落地状态 | 代码/文档 |
| --- | --- | --- |
| 共享资金账户组合回测 | 已落地 | `portfolio_backtest_symbols` |
| D+1 开盘前成交复核 | 已落地 | 涨跌停、现金、单股、行业、总仓位 cap 均在成交前复核 |
| 单笔风险 1% | 已落地 | `RiskConfig.risk_per_trade` |
| 单股上限 20% | 已落地 | `RiskConfig.max_position_pct` |
| 单行业上限 40% | 已落地 | `RiskConfig.max_sector_pct` |
| regime 总仓位 80/50/30 | 已落地 | `RiskConfig.regime_exposure` |
| 现金保留 20% | 已落地 | `RiskConfig.cash_reserve_pct` |
| 金字塔 5/15/30 分档 | 已落地 | `RiskConfig.pyramid_steps`，当前新仓默认首档 |
| 四不做 | 已落地 | `entry_filters.apply_four_no_entry` |
| R:R >= 2 | 已落地 | `EntryFilterConfig.min_rr` |
| 行业层/三层共振 | 已落地基础版 | `sector_state_from_members`，行业映射为静态表 |
| 移动止损 | 已落地 | `trailing_stop` |
| 时间止损 | 已落地 | `time_stop_trigger` |
| 3 天不创新高退出 | 已落地 | `_exit_by_discipline` |
| 跌破 MA20 退出 | 已落地 | `_exit_by_discipline` |
| 最大回撤/单日/单周熔断 | 已落地基础版 | `update_account_risk` |
| 15+ 标的、5+ 行业、2 年证据 | 已落地 | `reports/backtest/*_2024-07-01_2026-07-01.*` |
| 100 只全市场抽样 | 未完成 | 需先补行业归属缓存和批量行情稳定性 |

### 新增模块

- `chan520_skill/risk.py`：风险预算、仓位 cap、regime 仓位、现金保留、金字塔分档、移动止损、时间止损、账户熔断。
- `chan520_skill/entry_filters.py`：四不做、止损距离、急涨急跌、盈亏比和盈亏平衡胜率。
- `chan520_skill/sector.py`：行业映射、行业宽度、行业 60 日方向和行业门控。

### 回测升级

- `backtest_symbols` 默认委托共享账户组合引擎。
- 历史行情先取腾讯后复权，失败回退东方财富后复权。
- 指标预计算，避免逐日完整调用 `analyze`。
- 同一天多只股票触发时，先预占计划现金/行业/总仓位；D+1 实际成交前再按开盘价复核所有 cap。
- metrics 输出新增纪律合规段：`max_symbol_pct`、`max_sector_pct`、`max_exposure`、`avg_entry_rr`、`breakeven_win_rate`、拒绝次数和 regime 平均暴露。

### 文档

- 新增 [docs/risk.md](docs/risk.md)。
- 新增 [docs/sector.md](docs/sector.md)。
- 更新 [docs/backtest.md](docs/backtest.md)。
- 更新 [DEPLOY.md](DEPLOY.md) 和 [README.md](README.md)。

### 测试

- 新增 `tests/test_risk.py`。
- 新增 `tests/test_entry_filters.py`。
- 新增 `tests/test_sector.py`。
- 新增 `tests/test_portfolio_backtest.py`。
- 新增 `tests/test_exit_discipline.py`。

### 已知限制

- 行业映射仍是静态表，未接入真实全市场行业归属缓存。
- 行业共振使用篮子内成员宽度代理，尚未使用完整行业指数或行业成分。
- 金字塔分档已进入仓位函数，但当前组合回测只做首仓，未实现盈利后 15%/30% 动态加仓。
- 100 只全市场抽样未提交；公开接口批量拉取、行业映射和缓存层还需加固后再做。
- 本项目仍是本地分析和复盘工具，不是自动下单系统。

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
