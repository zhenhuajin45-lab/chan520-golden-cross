# 测试说明

第三轮新增/强化的测试：

- `tests/test_sizing_realized_risk.py`：验证仓位不再塌缩为 1 手，单笔风险按预算和 cap 计算。
- `tests/test_pyramiding.py`：验证盈利趋势中的加仓信号，以及亏损/止损下方不加仓。
- `tests/test_backtest_regime.py`：验证组合 regime 使用指数序列，而非篮子宽度。
- `tests/test_portfolio_backtest.py`：组合级 D+1、cap、涨停拒买和无前视入场字段测试。

仍未完成：

- 离线 30/100 股票真实快照夹具。
- `PaperRunner` 与回测 runner 同源委托清单测试。
- 覆盖率门槛和一键 CI 脚本。
