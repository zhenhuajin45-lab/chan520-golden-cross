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

## 注意事项

- 本项目只做策略复盘和本地分析，不构成投资建议。
- 行情接口是公开接口，可能出现限流、断开、字段变化。
- 批量扫描采用“两阶段”：全市场报价初筛，再对候选拉历史 K 线精扫。
- `reports/` 是生成目录，默认不提交到 GitHub。
- 若需要复现某天结果，可以把对应 `reports/` 文件单独作为附件发给别人。

## 常见问题

### 1. akshare 或行情接口超时

稍后重试，或降低并发扫描量。公开接口偶发断开属于正常情况。

### 2. PowerShell 控制台中文乱码

通常不影响生成的 Markdown/CSV 文件，文件使用 UTF-8 编码。

### 3. 别人运行全市场扫描很慢

这是正常的。全市场历史 K 线逐只下载会被限速，所以默认脚本只对报价初筛后的候选做历史精扫。
