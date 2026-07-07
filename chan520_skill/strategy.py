from __future__ import annotations

from datetime import date
from statistics import mean

from .indicators import build_indicators, crossed_down, crossed_up, fmt, pct_change, weekly_bars
from .models import AnalysisReport, KLine, RuleResult, StockMeta


def analyze(meta: StockMeta, rows: list[KLine], target_date: date) -> AnalysisReport:
    indicators = build_indicators(rows)
    target = rows[-1]
    point = indicators[-1]
    prev = indicators[-2]
    report = AnalysisReport(meta=meta, target=target, indicator=point, previous_indicator=prev)

    report.large_cycle = _large_cycle(rows, point)
    report.buy_points = _buy_points(rows, indicators)
    report.satisfied = _satisfied_conditions(rows, indicators)
    report.defects = _defects(rows, indicators)
    report.verdict, report.level = _verdict(report)
    report.operation_rows = _operations(report)
    report.core_summary = _core_summary(report)
    return report


def _large_cycle(rows: list[KLine], point) -> list[RuleResult]:
    target = rows[-1]
    results: list[RuleResult] = []
    if len(rows) >= 61:
        rise60 = pct_change(rows[-61].close, target.close)
        status = "PASS" if rise60 >= 5 else ("WARN" if rise60 >= 0 else "FAIL")
        label = "上升确认" if status == "PASS" else ("震荡修复" if status == "WARN" else "震荡偏弱")
        results.append(
            RuleResult(
                "大周期 60日/长期趋势",
                status,
                f"{label}。60日涨幅 {rise60:.2f}%，收盘 {target.close:.2f}，MA60 {fmt(point.ma60)}。",
                2 if status == "PASS" else (-1 if status == "FAIL" else 0),
            )
        )
    if point.ma5 and point.ma10 and point.ma20 and point.ma60:
        bull = point.ma5 > point.ma10 > point.ma20 > point.ma60
        partial = point.ma5 > point.ma20 and target.close > point.ma20
        results.append(
            RuleResult(
                "日线均线结构",
                "PASS" if bull else ("WARN" if partial else "FAIL"),
                f"MA5 {fmt(point.ma5)} / MA10 {fmt(point.ma10)} / MA20 {fmt(point.ma20)} / MA60 {fmt(point.ma60)}。"
                + ("多头排列成立。" if bull else "多头排列不完整。"),
                2 if bull else (0 if partial else -2),
            )
        )
    if point.ma250:
        above250 = all(
            value is not None and value > point.ma250
            for value in (point.ma5, point.ma10, point.ma20, point.ma60, target.close)
        )
        results.append(
            RuleResult(
                "250日线过滤",
                "PASS" if above250 else "WARN",
                f"MA250 {fmt(point.ma250)}，用于新手框架的长期强弱过滤。",
                1 if above250 else 0,
            )
        )
    weekly = weekly_bars(rows)
    weekly_ind = build_indicators(weekly)
    if weekly_ind and weekly_ind[-1].ma60:
        w = weekly_ind[-1]
        results.append(
            RuleResult(
                "周线 MA60",
                "PASS" if weekly[-1].close > w.ma60 else "FAIL",
                f"周收盘 {weekly[-1].close:.2f}，周 MA60 {fmt(w.ma60)}。",
                2 if weekly[-1].close > w.ma60 else -2,
            )
        )
    return results


def _buy_points(rows: list[KLine], indicators) -> list[RuleResult]:
    target = rows[-1]
    prev_row = rows[-2]
    point = indicators[-1]
    prev = indicators[-2]
    results: list[RuleResult] = []

    cross520 = crossed_up(prev.ma5, prev.ma20, point.ma5, point.ma20)
    above_ma = point.ma5 and point.ma20 and target.close > point.ma5 and target.close > point.ma20
    macd_gold = point.macd_dif is not None and point.macd_dea is not None and point.macd_dif > point.macd_dea
    macd_strong = macd_gold and point.macd_dif > 0 and point.macd_dea > 0
    volume_ok = point.volume_ratio is not None and point.volume_ratio >= 1.2
    large_ok = len(rows) >= 61 and pct_change(rows[-61].close, target.close) >= 5

    if cross520 and above_ma:
        status = "PASS" if macd_strong and large_ok else "WARN"
        detail = (
            f"MA5 上穿 MA20，收盘 {target.close:.2f} > MA5 {fmt(point.ma5)} > MA20 {fmt(point.ma20)}。"
            f"MACD DIF {fmt(point.macd_dif, 3)} / DEA {fmt(point.macd_dea, 3)}。"
        )
        if status == "WARN":
            detail += " 触发成立，但大周期或 MACD 零轴强度不足，按弱化版处理。"
        results.append(RuleResult("520金叉买", status, detail, 3 if status == "PASS" else 1))
    else:
        results.append(
            RuleResult(
                "520金叉买",
                "FAIL",
                f"未触发。MA5 {fmt(point.ma5)}，MA20 {fmt(point.ma20)}，昨日 MA5/MA20 {fmt(prev.ma5)}/{fmt(prev.ma20)}。",
                -2,
            )
        )

    if macd_gold:
        results.append(
            RuleResult(
                "MACD同步金叉",
                "PASS" if macd_strong else "WARN",
                f"DIF {fmt(point.macd_dif, 3)} > DEA {fmt(point.macd_dea, 3)}，柱 {fmt(point.macd_hist, 3)}。"
                + ("零轴上方，强信号。" if macd_strong else "零轴未完全站上，信号偏弱。"),
                2 if macd_strong else 1,
            )
        )
    else:
        results.append(RuleResult("MACD同步金叉", "FAIL", f"DIF {fmt(point.macd_dif, 3)} <= DEA {fmt(point.macd_dea, 3)}。", -1))

    if point.ma60 and target.close > point.ma60:
        just_crossed = prev.ma60 is not None and prev_row.close <= prev.ma60
        results.append(
            RuleResult(
                "突破回踩买",
                "WARN" if just_crossed else "PASS",
                f"收盘 {target.close:.2f} > MA60 {fmt(point.ma60)}。"
                + ("刚突破，需未来2-3日确认不破。" if just_crossed else "已站在 MA60 上方。"),
                1,
            )
        )
    else:
        results.append(RuleResult("突破回踩买", "FAIL", f"未站上 MA60 {fmt(point.ma60)}。", -1))

    if point.ma20 and target.low <= point.ma20 * 1.015 and target.close >= point.ma20 and not cross520:
        results.append(RuleResult("20日线低吸", "WARN", f"触及/接近 MA20 {fmt(point.ma20)} 后收回，可观察缩量企稳。", 1))
    else:
        results.append(RuleResult("20日线低吸", "FAIL", "不在回踩 MA20 的低吸结构，或已被 520 金叉信号替代。", 0))

    if prev.ma5 and point.ma5 and prev_row.close < prev.ma5 and target.close > point.ma5:
        results.append(RuleResult("破五反五", "WARN", f"昨日跌破 MA5，今日收回 MA5 {fmt(point.ma5)}。", 1))
    else:
        results.append(RuleResult("破五反五", "FAIL", "未出现跌破5日线后快速收回结构。", 0))

    lows = [row.low for row in rows[-12:]]
    closes = [row.close for row in rows[-12:]]
    low_band = (max(lows) - min(lows)) / min(lows) if lows and min(lows) else 1
    if low_band <= 0.06 and target.close >= max(closes[:-1]):
        results.append(RuleResult("平底结构买", "WARN", f"近12日低点区间宽度 {low_band * 100:.2f}%，今日尝试向上突破。", 1))
    else:
        results.append(RuleResult("平底结构买", "FAIL", "未形成可确认的平底突破。", 0))

    if _chan_first_buy(rows, indicators):
        results.append(RuleResult("缠论一买", "WARN", "近阶段新低后 MACD 下行动能未同步创新低，属于左侧试错结构。", 1))
    else:
        results.append(RuleResult("缠论一买", "FAIL", "不在标准下跌背驰后的左侧一买。", 0))

    if point.ma60 and target.close > point.ma60 and _recent_breakout(rows, indicators, point.ma60):
        results.append(RuleResult("缠论二/三买", "WARN", "突破中枢/MA60 后处在首次确认期，等待回踩不破可升级。", 1))
    else:
        results.append(RuleResult("缠论二/三买", "FAIL", "未形成突破后回踩不破的二买/三买确认。", 0))

    if volume_ok:
        results.append(RuleResult("放量确认", "PASS", f"量比 {fmt(point.volume_ratio)}，高于近5日均量。", 1))
    else:
        results.append(RuleResult("放量确认", "WARN", f"量比 {fmt(point.volume_ratio)}，量能确认不足。", 0))

    return results


def _satisfied_conditions(rows: list[KLine], indicators) -> list[str]:
    target = rows[-1]
    point = indicators[-1]
    prev = indicators[-2]
    out: list[str] = []
    if crossed_up(prev.ma5, prev.ma20, point.ma5, point.ma20):
        out.append(f"520金叉当天触发：MA5 {fmt(point.ma5)} 上穿 MA20 {fmt(point.ma20)}")
    if point.macd_dif is not None and point.macd_dea is not None and point.macd_dif > point.macd_dea:
        out.append(f"MACD同步金叉：DIF {fmt(point.macd_dif, 3)} > DEA {fmt(point.macd_dea, 3)}")
    if point.volume_ratio and point.volume_ratio >= 1.2:
        out.append(f"放量确认：量比 {fmt(point.volume_ratio)}")
    if point.ma60 and target.close > point.ma60:
        out.append(f"股价突破MA60：{target.close:.2f} > {fmt(point.ma60)}")
    if point.rsi14 and 45 <= point.rsi14 <= 70:
        out.append(f"RSI {fmt(point.rsi14)}，处于健康/中性偏强区")
    if point.slope5_deg and 0 < point.slope5_deg < 20:
        out.append(f"斜率 {fmt(point.slope5_deg)} 度，温和上行")
    return out


def _defects(rows: list[KLine], indicators) -> list[str]:
    target = rows[-1]
    point = indicators[-1]
    defects: list[str] = []
    if len(rows) >= 61:
        rise60 = pct_change(rows[-61].close, target.close)
        if rise60 < 5:
            defects.append(f"大周期未达强趋势过滤：60日涨幅 {rise60:.2f}% < 5%")
    if point.macd_dif is not None and point.macd_dea is not None and (point.macd_dif < 0 or point.macd_dea < 0):
        defects.append("MACD金叉零轴未完全站上，属于弱化信号")
    if point.ma5 and point.ma10 and point.ma20 and point.ma60 and not (point.ma5 > point.ma10 > point.ma20 > point.ma60):
        defects.append("均线多头排列未完成，MA60仍可能构成中期压制")
    if point.ma60 and target.close > point.ma60 and target.close / point.ma60 < 1.03:
        defects.append("MA60突破幅度较小，需要2-3日站稳确认")
    if point.rsi14 and point.rsi14 > 70:
        defects.append("RSI进入超买区，追高风险上升")
    return defects


def _verdict(report: AnalysisReport) -> tuple[str, str]:
    score = sum(item.score for item in report.large_cycle + report.buy_points)
    has_520 = any(item.name == "520金叉买" and item.status in {"PASS", "WARN"} for item in report.buy_points)
    hard_defects = len(report.defects)
    if has_520 and score >= 8 and hard_defects <= 1:
        return "入选", "确认"
    if has_520 and score >= 3:
        return "观察（轻仓试探）", "观察"
    if score >= 2:
        return "观察", "观察"
    return "不入选", "回避"


def _operations(report: AnalysisReport) -> list[tuple[str, str]]:
    point = report.indicator
    close = report.target.close
    stop_ma20 = point.ma20 * 0.97 if point.ma20 else None
    stop_pct = close * 0.95
    stop = min(value for value in [stop_ma20, stop_pct] if value is not None)
    target1 = _recent_high_hint(report)
    rr = (target1 - close) / (close - stop) if close > stop else 0.0
    rows = [
        ("空仓者（未买入）", "可轻仓试探 <=5%，但必须把它当弱化信号；更稳妥是等待2-3日站稳MA60且MACD继续上行。"),
        ("已持仓者", f"以 MA60 {fmt(point.ma60)} 和 MA20 {fmt(point.ma20)} 为移动止盈/防守参考，跌破并放量则减仓。"),
        ("理想买点", f"今日若已触发520，只适合尾盘/次日确认式小仓；更优买点是回踩 MA60/MA20 不破后再放量上攻。"),
        ("止损位", f"硬止损参考 {stop:.2f}；若跌回 MA60 {fmt(point.ma60)} 下方且MACD走弱，确认失败。"),
        ("目标位", f"前高/压力区参考 {target1:.2f}，当前估算盈亏比约 {rr:.2f}:1；不足2:1时不宜重仓。"),
    ]
    return rows


def _core_summary(report: AnalysisReport) -> str:
    target = report.target
    point = report.indicator
    positives = " + ".join(item.split("：")[0] for item in report.satisfied[:4]) or "有效共振不足"
    defects = "；".join(report.defects[:3]) or "暂无明显硬伤"
    return (
        f"{report.meta.name}（{report.meta.code}）在 {target.date.isoformat()} 的结论为“{report.verdict}”。"
        f"积极因素：{positives}。主要缺陷：{defects}。"
        f"收盘 {target.close:.2f}，MA60 {fmt(point.ma60)}，RSI {fmt(point.rsi14)}，量比 {fmt(point.volume_ratio)}。"
    )


def _recent_high_hint(report: AnalysisReport) -> float:
    close = report.target.close
    # The framework uses recent former highs as first target, then enforces risk/reward.
    return max(close * 1.06, close + 0.01)


def _chan_first_buy(rows: list[KLine], indicators) -> bool:
    if len(rows) < 45:
        return False
    recent = rows[-30:]
    prior = rows[-60:-30] if len(rows) >= 60 else rows[:-30]
    if not prior:
        return False
    price_new_low = min(row.low for row in recent[-10:]) <= min(row.low for row in prior)
    recent_hist = [p.macd_hist for p in indicators[-10:] if p.macd_hist is not None]
    prior_hist = [p.macd_hist for p in indicators[-40:-10] if p.macd_hist is not None]
    if not recent_hist or not prior_hist:
        return False
    momentum_divergence = min(recent_hist) > min(prior_hist)
    return price_new_low and momentum_divergence


def _recent_breakout(rows: list[KLine], indicators, line: float) -> bool:
    for row, point in zip(rows[-4:], indicators[-4:]):
        if point.ma60 and row.close > point.ma60:
            return True
    return False
