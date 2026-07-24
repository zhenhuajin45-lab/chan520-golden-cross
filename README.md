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

## v5 Alpha 全市场研究框架

v5 将 520 金叉降级为趋势确认，新增市场状态、动态股票池、趋势回踩入场、Alpha 评分、ATR 风控、组合引擎和板块热度轻加分。GM 真实数据只用于构建本地数据底座；后续反复回测直接读取 SQLite。

```powershell
# 1. 用 GM 缓存构建本地 SQLite 数据库（数据库文件不提交 Git）
python scripts/gm_alpha_store.py build --cache-dir reports/backtest/v6/all --store data/gm_alpha/chan520_alpha.sqlite --start 2016-01-01 --end 2026-07-09 --universe all --sector-source industry --rebuild

# 2. 从本地 SQLite 跑 2026 全市场回测
python scripts/gm_alpha_store.py run --store data/gm_alpha/chan520_alpha.sqlite --start 2026-01-01 --end 2026-07-09 --output-dir reports/backtest/v6/store_all_2026 --lookback-days 900
```

本地库包含 `daily_bars`、`instrument_status`、`sector_map`、`dynamic_universe`、`index_bars` 等表。`data/gm_alpha/*.sqlite*` 已在 `.gitignore` 中排除；GitHub 只提交代码、脚本、测试和轻量回测报告。

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

## 本地模拟盘工作台

不接 GM/掘金时，可以使用本地 SQLite 模拟券商。默认账户资金为 100 万，账本写入 `data/local_sim/broker.sqlite`，该文件不会提交到 Git。

```bash
python3.11 -m venv .venv311
source .venv311/bin/activate
pip install -r requirements.txt

python scripts/local_sim_broker.py --initial-cash 1000000 init
python scripts/local_sim_broker.py buy --symbol SHSE.600288 --volume 100 --price 10.00 --client-order-id demo-001 --session-date 2026-07-15 --signal-name trend_pullback_entry --entry-reason "趋势向上，回踩后收回 MA20，R:R>=2"
python scripts/export_local_sim_dashboard.py --initial-cash 1000000
python scripts/check_local_sim_readiness.py --trade-date 2026-07-15 --fix
cd web_dashboard && python -m http.server 8765
```

浏览器打开 `http://127.0.0.1:8765/` 查看账户、持仓、每日汇总、订单和成交明细。页面每 15 秒自动读取最新导出数据；每日任务会在关键阶段自动刷新数据文件。本地模拟盘订单必须传 `--session-date`，否则会 fail-closed；买入可填写 `--entry-reason`，卖出可填写 `--exit-reason`、`--risk-reason`、`--risk-reason-code`，这些字段会进入工作台和飞书消息。工作台同时展示风险循环与新增买入两套 readiness，避免“可管理持仓”被误读为“允许开新仓”。

飞书推送使用本地私有配置或环境变量，不提交 webhook：

```bash
# 环境变量方式
export FEISHU_LOCAL_SIM_WEBHOOK_URL="你的飞书机器人 webhook"

# 或在本机创建 config_private.local.json:
# {"feishu":{"local_sim_webhook":"你的飞书机器人 webhook"}}

python scripts/export_local_sim_dashboard.py --initial-cash 1000000
python scripts/push_local_sim_feishu.py --mode trades --dry-run
python scripts/push_local_sim_feishu.py --mode trades
python scripts/push_local_sim_feishu.py --mode review
```

`plan` 推送盘前计划和阻断边界，`trades` 推送新增成交，`review` 推送每日账户复盘；推送状态用 `data/local_sim/feishu_push_state.json` 防重复。复盘按账户、收盘市场和全池回放的关键证据指纹去重：内容未变不重复，18:00 恢复后证据发生实质变化则允许发送更正版。估值不完整时盘后复盘 fail-closed，不发送误导性盈亏。审计结果写入 `reports/local_sim_feishu/YYYY-MM-DD/feishu_push_audit.json`。

如果希望下单成交后立即推送新增成交，可以在本地下单命令后加：

```bash
python scripts/local_sim_broker.py buy --symbol SHSE.600288 --volume 100 --price 10.00 --client-order-id demo-002 --session-date 2026-07-15 --entry-reason "严格价位触发：开盘价处于技术买入区" --push-feishu
```

本地模拟盘也可以接入 paper open/close kernel。`close` 生成计划单，`open` 把 kernel 触发的成交同步到本地模拟盘账本：

```bash
python scripts/paper_runner.py close --date 2026-07-15 --incremental-prefix --initial-cash 1000000 --local-sim-sync
python scripts/paper_runner.py open --date 2026-07-16 --incremental-prefix --initial-cash 1000000 --local-sim-sync
```

实时估值和盘中风控候选：

```bash
python scripts/export_local_sim_dashboard.py --initial-cash 1000000 --mark-quotes
python scripts/local_sim_risk_scan.py --trade-date 2026-07-15
python scripts/execute_local_sim_risk_exits.py --trade-date 2026-07-16 --submit
```

核心计划 `local_sim_core_plan_v2` 额外执行两项硬门：计划价格必须满足 `止损 < 下沿 <= 触发 <= 上沿 < 失效价`；仓位风险按技术止损与 T+1 隔夜波动缓冲的较大值计算，主板缓冲底线 7.5%，创业板/科创板 12%。风险策略 `local_sim_risk_policy_v2` 对 -5.5% 硬止损使用单次新鲜报价快速通道，MA20/MA5 失效仍需二阶段确认；利润保护在峰值浮盈达到 3%、回吐 1.5% 且保留利润低于峰值 65% 时触发候选。未完成风险计划会跨交易日保留武装时间、确认状态和证据；账户存在未解决风险卖单时，新增买入保持关闭。

推荐使用每日编排器。`plan` 只使用上一交易日完整日线；扫描覆盖率低于 85%、市场状态未知、计划几何无效或无严格候选时，新增买入保持关闭。`intraday` 先处理 T+1 风控卖出，再处理买入二阶段确认。

当市场状态为 `BEAR` 时，核心账户仍严格禁止新增买入。符合主板防御形态、完整止损/目标证据、有效价格几何且 `R:R >= 2` 的观察候选，可进入独立账户 `local-sim-bear-pilot`：单票最多 2.5%，最多 2 只，总仓最多 5%。该账户沿用报价新鲜度、停牌/涨跌停、二阶段确认、T+1 和盘中风险退出规则，仅用于本地模拟研究，不连接 GM，也不改变核心账户或 `shadow_readiness=false`。工作台与飞书分别展示两套账户，成交去重和风控状态不混用。

```bash
python scripts/run_local_sim_daily.py --phase plan --trade-date 2026-07-16 --feishu dry-run
python scripts/run_local_sim_daily.py --phase preopen --trade-date 2026-07-16 --feishu dry-run
python scripts/run_local_sim_daily.py --phase intraday --trade-date 2026-07-16 --feishu send
python scripts/run_local_sim_daily.py --phase eod --trade-date 2026-07-16 --feishu send
```

Mac 上可安装 `launchd` 任务与 8768 工作台常驻服务。安装器默认拒绝覆盖已有的同名但内容不同的 plist：

```bash
.venv311/bin/python scripts/install_local_sim_launchd.py
```

安装后工作日自动执行：08:00 计划，08:15 做一次幂等补偿检查（已有当日 `PASS` 计划即跳过）、09:20 盘前检查、09:30-11:28 与 13:00-14:58 每两分钟运行一次盘中风控和触发循环，15:20 严格按“收盘行情刷新 → 风险扫描 → 全候选回放 → 工作台导出 → 飞书复盘”生成最终证据，18:00 对降级收盘扫描做一次独立批次恢复。计划生成步骤有 5 分钟 SLA 和 10 分钟硬超时，保证首轮超时后 08:15 补偿任务仍有独立运行窗口。每轮都先处理风险卖出，再处理买入；两个及以上主要指数跌幅达到 1.5% 时阻断新增买入。同阶段 90 秒内的重复任务会幂等跳过，逐次摘要不会覆盖正式运行证据。外层日志写入 `reports/local_sim_launchd/`，每次任务的逐步骤证据仍写入 `reports/local_sim_daily/YYYYMMDD/`。扫描报告同时记录研究覆盖率与前复权执行级覆盖率；新增买入必须两者都达标。

关键行情采用多源和精确日期校验：股票/指数历史和报价优先腾讯、东方财富兜底，股票池由东方财富、新浪和合格本地快照交叉补充，同花顺公开数据仅用于情绪、行业和主题旁路诊断。成功取得的日线、指数、扫描快照、行业映射和历史分钟路径写入 `data/market.db`；本地回退必须精确包含目标交易日，不能拿最新报价补历史日期。盘后 `UNKNOWN` 市场状态会使用信号日指数重建研究状态，但不会改变实盘 fail-closed 决策。

熊市防御观察池回放同时输出三种证据：全候选逐票独立表现、几何有效候选按既定风险排序的前两笔组合，以及熊市研究子集的风险逆序/评分/代码顺序敏感性。即使信号日不是 `BEAR`、熊市研究子集为空，也必须保留完整观察池证据，不得退化为空白 `NO_CANDIDATES`。报告位于 `reports/local_sim_counterfactual/YYYYMMDD/watch_only_replay.json`，只用于复盘，不写入模拟盘账本。

本地自动执行不代表 GM/正式 shadow kernel 就绪。`auto_open_close_kernel_ready`、`gm_adapter_shadow_ready` 和 `shadow_readiness` 在完成正式 V5 数据、行业映射、回放对账与异常演练前继续保持 `false`。
