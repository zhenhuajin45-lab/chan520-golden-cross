# 部署说明

本项目是一个本地 A 股 `520/缠论` 策略分析器，依赖公开行情接口。适合通过 GitHub 分发给其他人本地运行。

## 环境要求

- Python 3.10 或更高版本
- Windows/macOS/Linux 均可
- 能访问腾讯行情、东方财富或 akshare 依赖的数据接口

## 安装

```bash
git clone <your-repo-url>
cd <repo-dir>
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS/Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## 单票分析

```bash
python -m chan520_skill analyze 600288 --date 2026-07-07 --source auto
```

默认会启用市场 `regime` 降级。若只想复刻单票技术面，可关闭：

```bash
python -m chan520_skill analyze 600288 --date 2026-07-07 --no-regime
```

输出默认写入：

```text
reports/600288_2026-07-07.md
```

## 全市场候选扫描

```bash
python scripts/scan_candidates.py
python scripts/make_clean_report.py
```

输出：

```text
reports/market_quote_prefilter_2026-07-07.csv
reports/market_candidate_scan_2026-07-07.csv
reports/market_candidate_scan_2026-07-07.md
reports/market_candidate_clean_2026-07-07.md
```

## 事件驱动回测

```bash
python -m chan520_skill backtest 600288 --start 2026-01-01 --end 2026-07-01 --split-date 2026-04-01
python -m chan520_skill backtest --basket "600288,300568,688106,002132,603638,301282,600203,300390" --start 2026-01-01 --end 2026-07-01 --split-date 2026-04-01
```

第三轮回测默认使用共享账户组合引擎，不是逐票独立相加。默认纪律包括：

- 单笔风险 1%、单股 20%、单行业 40%。
- 首仓资金上限 15%，目标规模不足 1 手时跳过。
- 市场 regime 默认使用沪深300指数：`trend_up=80%`、`range=50%`、`down=30%`。
- 现金保留 20%。
- 只接受真实 `analyze()` 的标准 520 入选，执行“四不做”和 `R:R>=2`。
- D+1 开盘成交前复核涨跌停、现金、单股、行业、总仓位。
- 持仓后执行延迟移动止损、时间止损、3 天不创新高和跌破 MA20 退出。
- 板块宽度门控默认关闭；`--use-sector` 才启用当前代理行业层。

复现第三轮证据：

```powershell
python -m chan520_skill backtest --basket "600288,300568,688106,002132,603638,301282,600203,300390,002830,601199,688584,688130,300966,603893,002077,600519,000858,601318,600036,000001,600030,601166,600887,000333,000651,002594,300750,601012,600276,000725" --start 2024-07-01 --end 2026-07-01 --split-date 2025-07-01 --output-dir reports/backtest/v3
```

当前第三轮证据只有 3 笔交易，`sample_sufficient=0`，不用于判断策略 EV。

输出：

```text
reports/backtest/trades_*.csv
reports/backtest/metrics_*.md
```

## 注意事项

- 本项目只做策略复盘和本地分析，不构成投资建议。
- 行情接口是公开接口，可能出现限流、断开、字段变化。
- 批量扫描采用“两阶段”：全市场报价初筛，再对候选拉历史 K 线精扫。
- `reports/` 是生成目录；普通扫描输出默认不提交到 GitHub，`reports/backtest/` 中的评审证据会提交。
- 若需要复现某天结果，可以把对应 `reports/` 文件单独作为附件发给别人。

## 常见问题

### 1. akshare 或行情接口超时

稍后重试，或降低并发扫描量。公开接口偶发断开属于正常情况。

### 2. PowerShell 控制台中文乱码

通常不影响生成的 Markdown/CSV 文件，文件使用 UTF-8 编码。

### 3. 别人运行全市场扫描很慢

这是正常的。全市场历史 K 线逐只下载会被限速，所以默认脚本只对报价初筛后的候选做历史精扫。
