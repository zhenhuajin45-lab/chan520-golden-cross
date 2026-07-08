from __future__ import annotations

from .indicators import fmt
from .models import AnalysisReport, RuleResult


def render_markdown(report: AnalysisReport) -> str:
    target = report.target
    point = report.indicator
    lines: list[str] = []
    lines.append(f"# {report.meta.name}（{report.meta.code}）520战法买卖点报告")
    lines.append("")
    lines.append(f"数据截止：{target.date.isoformat()} 收盘 | 收盘价：{target.close:.2f}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 一、三周期扫描结论")
    lines.extend(_rule_bullets(report.large_cycle))
    lines.append("")
    lines.append("- **中周期（日线结构）**："
                 f"MA5 {fmt(point.ma5)} > MA10 {fmt(point.ma10)} > MA20 {fmt(point.ma20)}，"
                 f"MA60 {fmt(point.ma60)}；MACD DIF {fmt(point.macd_dif, 3)} / DEA {fmt(point.macd_dea, 3)}，"
                 f"柱 {fmt(point.macd_hist, 3)}。")
    lines.append("- **小周期（近期/量价）**："
                 f"量比 {fmt(point.volume_ratio)}，MA5斜率 {fmt(point.slope5_deg)} 度，"
                 f"RSI {fmt(point.rsi14)}，收盘相对 MA60 偏离 "
                 f"{_ma_bias(target.close, point.ma60)}。")
    lines.append("")
    lines.append("趋势5日线规则：")
    lines.extend(_rule_bullets(report.trend_rules))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 二、选股判定")
    lines.append("")
    lines.append("| 项目 | 判定 |")
    lines.append("| --- | --- |")
    lines.append(f"| 是否入选 | {report.verdict} |")
    lines.append(f"| 入选等级 | {report.level} |")
    lines.append(f"| 综合分 | {_score(report)} |")
    lines.append("")
    lines.append("满足的条件：")
    for item in report.satisfied:
        lines.append(f"- {item}")
    if not report.satisfied:
        lines.append("- 暂无足够强的共振条件。")
    lines.append("")
    if report.defects:
        lines.append("主要缺陷：")
        for item in report.defects:
            lines.append(f"- {item}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 三、买卖点判定")
    lines.append("")
    lines.append(f"当前状态：{report.verdict}")
    lines.append("")
    lines.append(f"买点判定：{_main_buy_label(report)}")
    lines.append("")
    lines.append("| 买点类型 | 触发状态 | 详细判定 |")
    lines.append("| --- | --- | --- |")
    for item in report.buy_points:
        lines.append(f"| {item.name} | {_status_text(item.status)} | {item.detail} |")
    lines.append("")
    lines.append("520金叉买分析：")
    lines.extend(_numbered_520(report))
    lines.append("")
    lines.append("趋势持仓/加仓判定：")
    lines.append("")
    lines.append("| 项目 | 状态 | 详细判定 |")
    lines.append("| --- | --- | --- |")
    for item in report.trend_rules + report.position_rules:
        lines.append(f"| {item.name} | {_status_text(item.status)} | {item.detail} |")
    lines.append("")
    lines.append("退出与止盈风险：")
    lines.append("")
    lines.append("| 项目 | 状态 | 详细判定 |")
    lines.append("| --- | --- | --- |")
    for item in report.exit_rules:
        lines.append(f"| {item.name} | {_status_text(item.status)} | {item.detail} |")
    lines.append("")
    lines.append("额外积极信号：")
    extra = [item for item in report.satisfied if not item.startswith("520")]
    if extra:
        for item in extra:
            lines.append(f"- {item}")
    else:
        lines.append("- 暂无额外强化项。")
    lines.append("")
    lines.append("结论：" f"{report.core_summary}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 四、风险与过滤")
    lines.append("")
    lines.append("| 风险项 | 当前结论 |")
    lines.append("| --- | --- |")
    risk_items = report.defects or ["无明显硬风险，但仍需按交易计划执行。"]
    for item in risk_items:
        lines.append(f"| 风险 | {item} |")
    for item in report.exit_rules:
        if item.status == "WARN":
            lines.append(f"| 退出/止盈 | {item.detail} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 五、操作建议")
    lines.append("")
    lines.append("| 投资者类型 | 建议 |")
    lines.append("| --- | --- |")
    for who, advice in report.operation_rows:
        lines.append(f"| {who} | {advice} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 六、核心结论")
    lines.append("")
    lines.append(report.core_summary)
    lines.append("")
    lines.append("本报告只用于本地复盘和策略验证，不构成投资建议。")
    lines.append("")
    return "\n".join(lines)


def _rule_bullets(items: list[RuleResult]) -> list[str]:
    lines = []
    for item in items:
        lines.append(f"- **{item.name}**：{_status_text(item.status)}。{item.detail}")
    return lines


def _status_text(status: str) -> str:
    return {"PASS": "通过", "WARN": "观察/弱化", "FAIL": "未触发"}.get(status, status)


def _ma_bias(close: float, ma: float | None) -> str:
    if ma is None:
        return "N/A"
    return f"{(close / ma - 1) * 100:.2f}%"


def _main_buy_label(report: AnalysisReport) -> str:
    for item in report.buy_points:
        if item.name == "520金叉买" and item.status in {"PASS", "WARN"}:
            if report.verdict == "观察（等待回踩）":
                return "520确认期/趋势延续（不追高，等回踩）"
            return "520金叉买（弱化版）" if item.status == "WARN" else "520金叉买（标准版）"
    for item in report.trend_rules:
        if item.name == "趋势5日线买入法" and item.status in {"PASS", "WARN"}:
            return "趋势5日线结构"
    return "无标准买点"


def _numbered_520(report: AnalysisReport) -> list[str]:
    point = report.indicator
    prev = report.previous_indicator
    close = report.target.close
    out = [
        f"1. MA5/MA20关系 -> 昨日 {fmt(prev.ma5)} / {fmt(prev.ma20)}，今日 {fmt(point.ma5)} / {fmt(point.ma20)}。",
        f"2. 股价站稳均线之上 -> 当前 {close:.2f}，MA5 {fmt(point.ma5)}，MA20 {fmt(point.ma20)}。",
        f"3. MACD同步金叉 -> DIF {fmt(point.macd_dif, 3)}，DEA {fmt(point.macd_dea, 3)}，柱 {fmt(point.macd_hist, 3)}。",
        f"4. 放量确认 -> 量比 {fmt(point.volume_ratio)}。",
    ]
    if report.target and len(report.large_cycle) > 0:
        out.append(f"5. 大周期过滤 -> {report.large_cycle[0].detail}")
    return out


def _score(report: AnalysisReport) -> int:
    return sum(
        item.score
        for item in report.large_cycle + report.buy_points + report.trend_rules + report.position_rules + report.exit_rules
    )
