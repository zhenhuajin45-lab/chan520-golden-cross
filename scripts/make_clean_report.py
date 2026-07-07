from __future__ import annotations

from pathlib import Path

import pandas as pd


def main() -> int:
    src = Path("reports/market_candidate_scan_2026-07-07.csv")
    df = pd.read_csv(src, dtype={"code": str}).fillna("")
    trial = df[df["verdict"].str.contains("轻仓", regex=False, na=False)].copy()
    clean = df[
        (df["verdict"].eq("观察"))
        & (df["score"] >= 9)
        & (df["pct_chg"] >= 0)
        & (~df["defects"].str.contains("超买", regex=False, na=False))
    ].copy()
    selected = df[df["verdict"].eq("入选")]

    lines: list[str] = [
        "# 2026-07-07 沪深非ST 520扫描精简结论",
        "",
        "- 口径：全市场腾讯收盘报价初筛，然后对候选跑完整历史K线规则。",
        "- 初筛条件：涨跌幅 >= 3%，或盘口量比 >= 2.5，或换手率 >= 8%。",
        "- 全市场代码池：4992；报价成功：4992；精扫候选：490；历史K线成功：489。",
        f"- 严格入选确认：{len(selected)}。",
        f"- 520弱化/轻仓试探：{len(trial)}。",
        f"- 正涨幅、分数>=9、无RSI超买缺陷的重点观察：{len(clean)}。",
        "",
    ]
    add_table(lines, "一、严格入选确认", selected, 50)
    add_table(lines, "二、520弱化/轻仓试探", trial.sort_values(["score", "pct_chg"], ascending=[False, False]), 50)
    add_table(lines, "三、重点观察 Top 50", clean.sort_values(["score", "pct_chg"], ascending=[False, False]), 50)
    Path("reports/market_candidate_clean_2026-07-07.md").write_text("\n".join(lines), encoding="utf-8")
    return 0


def add_table(lines: list[str], title: str, data, limit: int) -> None:
    lines.append(f"## {title}")
    lines.append("")
    if data.empty:
        lines.append("无。")
        lines.append("")
        return
    lines.append("| 代码 | 名称 | 收盘 | 涨跌幅 | 分数 | 主信号 | 主要缺陷 |")
    lines.append("| --- | --- | ---: | ---: | ---: | --- | --- |")
    for _, row in data.head(limit).iterrows():
        defect = shorten(str(row["defects"]).replace("|", "/"), 80)
        signal = shorten(str(row["main_signal"]).replace("|", "/"), 60)
        lines.append(
            f"| {row['code']} | {row['name']} | {float(row['close']):.2f} | "
            f"{float(row['pct_chg']):.2f}% | {int(row['score'])} | {signal} | {defect} |"
        )
    lines.append("")


def shorten(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
