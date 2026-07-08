from __future__ import annotations

from datetime import date

from .indicators import build_indicators, crossed_down, crossed_up, fmt, pct_change, weekly_bars
from .models import AnalysisReport, IndicatorPoint, KLine, RuleResult, StockMeta


def analyze(
    meta: StockMeta,
    rows: list[KLine],
    target_date: date,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
) -> AnalysisReport:
    if len(rows) < 61:
        raise ValueError("at least 61 daily bars are required for practical 520 analysis")
    indicators = build_indicators(rows, macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal)
    target = rows[-1]
    point = indicators[-1]
    prev = indicators[-2]
    report = AnalysisReport(meta=meta, target=target, indicator=point, previous_indicator=prev)

    report.large_cycle = _large_cycle(rows, indicators, macd_fast, macd_slow, macd_signal)
    report.buy_points = _buy_points(rows, indicators)
    report.trend_rules = _trend_5day_rules(rows, indicators)
    report.position_rules = _position_rules(rows, indicators)
    report.exit_rules = _exit_rules(rows, indicators)
    report.satisfied = _satisfied_conditions(rows, indicators, report)
    report.defects = _defects(rows, indicators, report)
    report.verdict, report.level = _verdict(report)
    report.operation_rows = _operations(rows, report)
    report.core_summary = _core_summary(report)
    return report


def _large_cycle(
    rows: list[KLine],
    indicators: list[IndicatorPoint],
    macd_fast: int,
    macd_slow: int,
    macd_signal: int,
) -> list[RuleResult]:
    target = rows[-1]
    point = indicators[-1]
    results: list[RuleResult] = []
    rise60 = pct_change(rows[-61].close, target.close)
    status = "PASS" if rise60 >= 5 else ("WARN" if rise60 >= 0 else "FAIL")
    label = "上升确认" if status == "PASS" else ("震荡修复" if status == "WARN" else "震荡偏弱")
    results.append(
        RuleResult(
            "大周期 60日/长期趋势",
            status,
            f"{label}。60日涨幅 {rise60:.2f}%，收盘 {target.close:.2f}，MA60 {fmt(point.ma60)}。",
            2 if status == "PASS" else (-2 if status == "FAIL" else 0),
        )
    )

    if point.ma5 and point.ma10 and point.ma20 and point.ma60:
        bull = point.ma5 > point.ma10 > point.ma20 > point.ma60
        repair = target.close > point.ma60 and point.ma5 > point.ma20
        partial = target.close > point.ma20 and point.ma5 > point.ma20
        results.append(
            RuleResult(
                "日线均线结构",
                "PASS" if bull else ("WARN" if repair or partial else "FAIL"),
                f"MA5 {fmt(point.ma5)} / MA10 {fmt(point.ma10)} / MA20 {fmt(point.ma20)} / MA60 {fmt(point.ma60)}。"
                + ("多头排列成立。" if bull else "多头排列未完整，按修复/观察处理。"),
                2 if bull else (1 if repair else (0 if partial else -2)),
            )
        )

    results.append(_mode_filter(rows, indicators))

    if point.ma250:
        above250 = target.close > point.ma250 and (point.ma60 is None or point.ma60 > point.ma250 * 0.95)
        results.append(
            RuleResult(
                "250日线过滤",
                "PASS" if above250 else "WARN",
                f"MA250 {fmt(point.ma250)}。长期过滤只用于降低大级别下跌趋势误用520的概率。",
                1 if above250 else 0,
            )
        )

    weekly = weekly_bars(rows)
    weekly_ind = build_indicators(weekly, macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal)
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


def _mode_filter(rows: list[KLine], indicators: list[IndicatorPoint]) -> RuleResult:
    target = rows[-1]
    point = indicators[-1]
    recent_high = _recent_high(rows, 20)
    trend_up = bool(point.ma5 and point.ma10 and point.ma20 and target.close > point.ma20 and point.ma5 > point.ma10 > point.ma20)
    platform_break = bool(recent_high and target.close >= recent_high and _volume_ok(point, 1.5))
    bottom_rebound = target.pct_chg >= 3 and _volume_ok(point, 1.5) and point.ma20 is not None and target.close >= point.ma20 * 0.96
    if trend_up or platform_break:
        detail = "符合“只做三种形态”中的趋势向上/平台放量突破。"
        return RuleResult("交易模式过滤", "PASS", detail, 2)
    if bottom_rebound:
        return RuleResult("交易模式过滤", "WARN", "底部放量反弹形态接近成立，但必须等待趋势不破确认。", 1)
    return RuleResult("交易模式过滤", "FAIL", "不属于趋势向上、平台放量突破或底部放量反弹的清晰形态。", -2)


def _buy_points(rows: list[KLine], indicators: list[IndicatorPoint]) -> list[RuleResult]:
    target = rows[-1]
    point = indicators[-1]
    prev = indicators[-2]
    results: list[RuleResult] = []

    cross_age = _recent_cross_age(indicators, "ma5", "ma20", lookback=3)
    above_ma = bool(point.ma5 and point.ma20 and target.close > point.ma5 and target.close > point.ma20)
    macd_gold = _macd_gold(point)
    macd_strong = macd_gold and point.macd_dif is not None and point.macd_dea is not None and point.macd_dif > 0 and point.macd_dea > 0
    large_ok = pct_change(rows[-61].close, target.close) >= 5
    volume_ok = _volume_ok(point, 1.2)
    extended = _is_extended(target, point)

    if cross_age is not None and above_ma:
        fresh = cross_age == 0
        standard = fresh and macd_strong and large_ok and volume_ok and not extended
        status = "PASS" if standard else "WARN"
        stage = "当天触发" if fresh else f"触发后第{cross_age + 1}日确认"
        detail = (
            f"{stage}。MA5 {fmt(point.ma5)} > MA20 {fmt(point.ma20)}，收盘 {target.close:.2f} 在双均线上方；"
            f"MACD DIF {fmt(point.macd_dif, 3)} / DEA {fmt(point.macd_dea, 3)}，量比 {fmt(point.volume_ratio)}。"
        )
        if not standard:
            detail += " 未同时满足大周期、零轴MACD、量能或低偏离要求，按弱化/确认期处理。"
        results.append(RuleResult("520金叉买", status, detail, 4 if standard else 2))
    else:
        results.append(
            RuleResult(
                "520金叉买",
                "FAIL",
                f"未触发近3日520金叉。今日 MA5 {fmt(point.ma5)}，MA20 {fmt(point.ma20)}；昨日 MA5/MA20 {fmt(prev.ma5)}/{fmt(prev.ma20)}。",
                -2,
            )
        )

    if macd_gold:
        results.append(
            RuleResult(
                "MACD同步金叉",
                "PASS" if macd_strong else "WARN",
                f"DIF {fmt(point.macd_dif, 3)} > DEA {fmt(point.macd_dea, 3)}，柱 {fmt(point.macd_hist, 3)}。"
                + ("零轴上方，强信号。" if macd_strong else "零轴未完全站上，信号偏弱但正在修复。"),
                2 if macd_strong else 1,
            )
        )
    else:
        results.append(RuleResult("MACD同步金叉", "FAIL", f"DIF {fmt(point.macd_dif, 3)} <= DEA {fmt(point.macd_dea, 3)}。", -1))

    if point.ma20 and target.low <= point.ma20 * 1.02 and target.close >= point.ma20 and cross_age is None:
        score = 2 if _volume_ok(point, 1.1) and _ma_turning_up(indicators, "ma5") else 1
        results.append(RuleResult("回踩MA20支撑买", "PASS" if score == 2 else "WARN", f"回踩/接近 MA20 {fmt(point.ma20)} 后收回。", score))
    else:
        results.append(RuleResult("回踩MA20支撑买", "FAIL", "不在上涨趋势回踩 MA20 不破的低吸结构。", 0))

    if point.ma60 and target.close > point.ma60:
        just_crossed = indicators[-2].ma60 is not None and rows[-2].close <= indicators[-2].ma60
        results.append(
            RuleResult(
                "突破回踩买",
                "WARN" if just_crossed or extended else "PASS",
                f"收盘 {target.close:.2f} > MA60 {fmt(point.ma60)}。"
                + ("刚突破/偏离较大，优先等2-3日确认或回踩不破。" if just_crossed or extended else "已站在 MA60 上方。"),
                1,
            )
        )
    else:
        results.append(RuleResult("突破回踩买", "FAIL", f"未站上 MA60 {fmt(point.ma60)}。", -1))

    if _chan_first_buy(rows, indicators):
        results.append(RuleResult("缠论一买", "WARN", "阶段新低后 MACD 下行动能未同步创新低，属于左侧试错结构。", 1))
    else:
        results.append(RuleResult("缠论一买", "FAIL", "不在标准下跌背驰后的左侧一买。", 0))

    if point.ma60 and target.close > point.ma60 and _recent_breakout(rows, indicators, point.ma60):
        results.append(RuleResult("缠论二/三买", "WARN", "突破中枢/MA60 后处在确认期，等待回踩不破可升级。", 1))
    else:
        results.append(RuleResult("缠论二/三买", "FAIL", "未形成突破后回踩不破的二买/三买确认。", 0))

    if volume_ok:
        results.append(RuleResult("放量确认", "PASS", f"量比 {fmt(point.volume_ratio)}，高于近5日均量。", 1))
    else:
        results.append(RuleResult("放量确认", "WARN", f"量比 {fmt(point.volume_ratio)}，量能确认不足。", 0))

    return results


def _trend_5day_rules(rows: list[KLine], indicators: list[IndicatorPoint]) -> list[RuleResult]:
    target = rows[-1]
    point = indicators[-1]
    conditions = _trend_5day_conditions(rows, indicators)
    hit_count = sum(1 for _, hit, _ in conditions if hit)
    applicable = bool(point.ma60 and target.close > point.ma60 and point.ma5 and point.ma20 and point.ma5 > point.ma20)
    if applicable and hit_count >= 3:
        status = "WARN" if _is_extended(target, point) else "PASS"
        detail = f"5日线主升买入法满足 {hit_count}/5 项；" + "；".join(text for _, hit, text in conditions if hit)
        score = 3 if status == "PASS" else 1
    elif applicable:
        status = "WARN"
        detail = f"趋势条件接近，但5日线买入法只满足 {hit_count}/5 项。"
        score = 0
    else:
        status = "FAIL"
        detail = "未处于突破MA60且MA5强于MA20的主升适用区。"
        score = -1
    hold_status = "PASS" if point.ma5 and target.close >= point.ma5 else "WARN"
    hold_detail = (
        f"收盘 {target.close:.2f} {'站上' if hold_status == 'PASS' else '跌破'} MA5 {fmt(point.ma5)}；"
        "5日线上方持有，跌破后反抽不过需退出。"
    )
    return [
        RuleResult("趋势5日线买入法", status, detail, score),
        RuleResult("5日线持股规则", hold_status, hold_detail, 1 if hold_status == "PASS" else -1),
    ]


def _trend_5day_conditions(rows: list[KLine], indicators: list[IndicatorPoint]) -> list[tuple[str, bool, str]]:
    target = rows[-1]
    point = indicators[-1]
    prev = indicators[-2]
    recent_attack = any(row.pct_chg >= 3 and _volume_ok(ind, 1.2) for row, ind in zip(rows[-8:], indicators[-8:]))
    touch_ma5 = bool(point.ma5 and target.low <= point.ma5 * 1.02 and target.close >= point.ma5)
    shrink = bool(point.volume_ratio and point.volume_ratio <= 1.05 and abs(target.pct_chg) <= 3)
    rebound = bool(
        prev.ma5
        and rows[-2].low <= prev.ma5 * 1.02
        and target.close > target.open
        and point.ma5
        and target.close > point.ma5
        and (point.volume_ratio or 0) >= 1.0
    )
    macd_better = bool(point.macd_hist is not None and prev.macd_hist is not None and point.macd_hist > prev.macd_hist)
    ma_spread = bool(point.ma5 and point.ma10 and point.ma20 and point.ma5 > point.ma10 > point.ma20)
    recent_ma_cross = _recent_cross_age(indicators, "ma5", "ma10", 5) is not None or _recent_cross_age(indicators, "ma5", "ma20", 5) is not None
    return [
        ("ma_spread", ma_spread or recent_ma_cross, "5日线上穿10/20或短期均线多头发散"),
        ("first_pullback", recent_attack and touch_ma5, "放量上攻后回踩5日线不破"),
        ("shrink_stop", shrink, "回踩/横盘过程缩量止跌"),
        ("rebound_candle", rebound, "回踩5日线后阳线收回且温和放量"),
        ("macd_better", macd_better, "MACD绿柱缩短或红柱继续增强"),
    ]


def _position_rules(rows: list[KLine], indicators: list[IndicatorPoint]) -> list[RuleResult]:
    target = rows[-1]
    point = indicators[-1]
    recent_high = _recent_high(rows, 20)
    breakout_add = bool(
        recent_high
        and target.close >= recent_high
        and point.ma5
        and point.ma10
        and point.ma20
        and point.ma5 > point.ma10 > point.ma20
        and _volume_ok(point, 1.2)
        and not _is_extended(target, point)
    )
    pullback_add = bool(
        point.ma10
        and target.low <= point.ma10 * 1.02
        and target.close >= point.ma10
        and point.ma5
        and point.ma5 > point.ma20
        and not _is_extended(target, point)
    )
    if breakout_add:
        add_rule = RuleResult("顺势加仓位置", "PASS", "首次突破近20日平台/压力且未明显追高，可按金字塔方式加仓。", 2)
    elif pullback_add:
        add_rule = RuleResult("顺势加仓位置", "WARN", "上升趋势回踩10日线附近止跌，属于观察型加仓点。", 1)
    else:
        add_rule = RuleResult("顺势加仓位置", "FAIL", "当前不是低风险加仓点；追高或逆势补仓都不符合材料纪律。", -1 if _is_extended(target, point) else 0)

    no_average_down = "PASS" if point.ma20 and target.close >= point.ma20 else "WARN"
    return [
        add_rule,
        RuleResult(
            "禁止逆势补仓",
            no_average_down,
            "趋势未失守，不涉及摊低成本；若跌破MA20/MA60只能减仓或止损，不能补仓。"
            if no_average_down == "PASS"
            else "价格已在MA20下方，材料规则要求先止损/等待底部结构，禁止亏损加仓。",
            0 if no_average_down == "PASS" else -2,
        ),
    ]


def _exit_rules(rows: list[KLine], indicators: list[IndicatorPoint]) -> list[RuleResult]:
    target = rows[-1]
    point = indicators[-1]
    prev = indicators[-2]
    results: list[RuleResult] = []
    death520 = crossed_down(prev.ma5, prev.ma20, point.ma5, point.ma20)
    lost_ma20 = bool(point.ma20 and target.close < point.ma20)
    if death520 or lost_ma20:
        results.append(RuleResult("MA20/520失败退出", "WARN", f"MA5 {fmt(point.ma5)}，MA20 {fmt(point.ma20)}，跌破或死叉应退出。", -4))
    else:
        results.append(RuleResult("MA20/520失败退出", "PASS", f"未跌破 MA20 {fmt(point.ma20)}，520中期防守未失效。", 1))

    lost_ma5_with_volume = bool(point.ma5 and target.close < point.ma5 and _volume_ok(point, 1.2))
    if lost_ma5_with_volume:
        results.append(RuleResult("5日线放量退出", "WARN", f"放量跌破 MA5 {fmt(point.ma5)}，趋势持仓需要减仓。", -2))
    else:
        results.append(RuleResult("5日线放量退出", "PASS", f"未出现放量跌破 MA5 {fmt(point.ma5)}。", 1))

    high_volume_stall = _is_long_upper(target) and _volume_ok(point, 1.5)
    sharp_run = len(rows) >= 11 and pct_change(rows[-11].close, target.close) >= 20
    if high_volume_stall or (sharp_run and _is_extended(target, point)):
        results.append(RuleResult("高位止盈风险", "WARN", "出现长上影/天量滞涨或短期涨幅过快，适合阶段性止盈而非追买。", -2))
    else:
        results.append(RuleResult("高位止盈风险", "PASS", "未触发天量滞涨、长上影或短期过热止盈条件。", 0))
    return results


def _satisfied_conditions(rows: list[KLine], indicators: list[IndicatorPoint], report: AnalysisReport) -> list[str]:
    target = rows[-1]
    point = indicators[-1]
    out: list[str] = []
    cross_age = _recent_cross_age(indicators, "ma5", "ma20", 3)
    if cross_age is not None:
        out.append(f"520金叉{'当天' if cross_age == 0 else '近3日'}触发：MA5 {fmt(point.ma5)} > MA20 {fmt(point.ma20)}")
    if _macd_gold(point):
        out.append(f"MACD同步金叉：DIF {fmt(point.macd_dif, 3)} > DEA {fmt(point.macd_dea, 3)}")
    if _volume_ok(point, 1.2):
        out.append(f"放量确认：量比 {fmt(point.volume_ratio)}")
    if point.ma60 and target.close > point.ma60:
        out.append(f"股价突破/站上MA60：{target.close:.2f} > {fmt(point.ma60)}")
    trend_rule = _find_rule(report.trend_rules, "趋势5日线买入法")
    if trend_rule and trend_rule.status in {"PASS", "WARN"}:
        out.append(trend_rule.detail)
    if point.rsi14 and 45 <= point.rsi14 <= 70:
        out.append(f"RSI {fmt(point.rsi14)}，处于健康/中性偏强区")
    if point.slope5_deg and 0 < point.slope5_deg < 25:
        out.append(f"MA5斜率 {fmt(point.slope5_deg)} 度，短线向上")
    return out


def _defects(rows: list[KLine], indicators: list[IndicatorPoint], report: AnalysisReport) -> list[str]:
    target = rows[-1]
    point = indicators[-1]
    defects: list[str] = []
    rise60 = pct_change(rows[-61].close, target.close)
    if rise60 < 5:
        defects.append(f"大周期未达强趋势过滤：60日涨幅 {rise60:.2f}% < 5%")
    if point.macd_dif is not None and point.macd_dea is not None and (point.macd_dif < 0 or point.macd_dea < 0):
        defects.append("MACD金叉零轴未完全站上，属于弱化信号")
    if point.ma5 and point.ma10 and point.ma20 and point.ma60 and not (point.ma5 > point.ma10 > point.ma20 > point.ma60):
        defects.append("均线多头排列未完成，MA60/MA20关系仍需确认")
    if _is_extended(target, point):
        defects.append("短线偏离5日线或20日线过大，空仓不宜追高，等待回踩确认")
    if point.rsi14 and point.rsi14 > 72:
        defects.append("RSI进入偏热区，追买风险上升")
    if any(item.status == "WARN" and item.score < 0 for item in report.exit_rules):
        defects.append("已有退出/止盈风险项被触发，买点失效或不适合新开仓")
    return defects


def _verdict(report: AnalysisReport) -> tuple[str, str]:
    score = _score(report)
    buy520 = _find_rule(report.buy_points, "520金叉买")
    trend5 = _find_rule(report.trend_rules, "趋势5日线买入法")
    exit_risk = any(item.status == "WARN" and item.score <= -2 for item in report.exit_rules)
    extended = any("追高" in item or "偏离" in item for item in report.defects)
    if exit_risk:
        return "回避/减仓观察", "风控"
    if buy520 and buy520.status == "PASS" and score >= 9 and len(report.defects) <= 1:
        return "入选", "标准520"
    if (buy520 and buy520.status in {"PASS", "WARN"} or trend5 and trend5.status in {"PASS", "WARN"}) and extended:
        return "观察（等待回踩）", "确认后回踩"
    if buy520 and buy520.status == "WARN" and score >= 4:
        return "观察（轻仓试探）", "弱化520"
    if trend5 and trend5.status == "PASS" and score >= 5:
        return "观察（趋势持有）", "5日线趋势"
    if score >= 2:
        return "观察", "观察"
    return "不入选", "回避"


def _operations(rows: list[KLine], report: AnalysisReport) -> list[tuple[str, str]]:
    point = report.indicator
    close = report.target.close
    stop = _stop_hint(report)
    target1 = _recent_high_hint(rows, close)
    rr = (target1 - close) / (close - stop) if close > stop else 0.0
    extended = _is_extended(report.target, point)
    if report.verdict == "入选":
        empty_advice = "可按计划小仓试错，首次仓位不宜过重；次日不破MA5/MA20且量价继续配合再考虑加仓。"
    elif extended:
        empty_advice = "不建议追高开仓；等待回踩MA5/MA20不破、缩量企稳后再评估。"
    else:
        empty_advice = "只适合观察或轻仓试探，必须把MA20/MA60作为确认失败线。"
    return [
        ("空仓者（未买入）", empty_advice),
        ("已持仓者", f"5日线上方可持有；跌破 MA5 {fmt(point.ma5)} 且放量先减仓，跌破 MA20 {fmt(point.ma20)} 或520死叉退出。"),
        ("加仓位置", "只在突破平台后缩量回踩不破、或回踩5/10/20日线止跌再放量时加仓；禁止亏损补仓和涨停后追高加仓。"),
        ("止损位", f"硬止损参考 {stop:.2f}；若跌回 MA20 {fmt(point.ma20)} / MA60 {fmt(point.ma60)} 下方且MACD走弱，确认失败。"),
        ("目标位", f"前高/压力区参考 {target1:.2f}，当前估算盈亏比约 {rr:.2f}:1；不足2:1时只适合轻仓或等待更好买点。"),
    ]


def _core_summary(report: AnalysisReport) -> str:
    target = report.target
    point = report.indicator
    positives = " + ".join(item.split("：")[0] for item in report.satisfied[:4]) or "有效共振不足"
    defects = "；".join(report.defects[:3]) or "暂无明显硬伤"
    return (
        f"{report.meta.name}（{report.meta.code}）在 {target.date.isoformat()} 的结论为“{report.verdict}”。"
        f"积极因素：{positives}。主要缺陷：{defects}。"
        f"收盘 {target.close:.2f}，MA5 {fmt(point.ma5)}，MA20 {fmt(point.ma20)}，MA60 {fmt(point.ma60)}，"
        f"RSI {fmt(point.rsi14)}，量比 {fmt(point.volume_ratio)}。"
    )


def _score(report: AnalysisReport) -> int:
    return sum(
        item.score
        for item in report.large_cycle + report.buy_points + report.trend_rules + report.position_rules + report.exit_rules
    )


def _stop_hint(report: AnalysisReport) -> float:
    close = report.target.close
    point = report.indicator
    candidates = [close * 0.95]
    if point.ma20:
        candidates.append(point.ma20 * 0.97)
    if point.ma60:
        candidates.append(point.ma60 * 0.98)
    below_close = [value for value in candidates if value < close]
    return max(below_close) if below_close else close * 0.95


def _recent_high_hint(rows: list[KLine], close: float) -> float:
    recent = [row.high for row in rows[-80:-1]]
    pressure = max(recent) if recent else close * 1.06
    return max(pressure, close * 1.06)


def _recent_high(rows: list[KLine], window: int) -> float | None:
    if len(rows) <= window:
        return None
    return max(row.high for row in rows[-window - 1 : -1])


def _volume_ok(point: IndicatorPoint, threshold: float) -> bool:
    return point.volume_ratio is not None and point.volume_ratio >= threshold


def _macd_gold(point: IndicatorPoint) -> bool:
    return point.macd_dif is not None and point.macd_dea is not None and point.macd_dif > point.macd_dea


def _ma_turning_up(indicators: list[IndicatorPoint], name: str) -> bool:
    if len(indicators) < 3:
        return False
    values = [getattr(item, name) for item in indicators[-3:]]
    return None not in values and values[-1] > values[-2] >= values[-3]


def _is_extended(row: KLine, point: IndicatorPoint) -> bool:
    ma5_ext = point.ma5 is not None and row.close / point.ma5 - 1 >= 0.08
    ma20_ext = point.ma20 is not None and row.close / point.ma20 - 1 >= 0.15
    rsi_hot = point.rsi14 is not None and point.rsi14 >= 72
    return ma5_ext or ma20_ext or rsi_hot


def _is_long_upper(row: KLine) -> bool:
    upper = row.high - max(row.open, row.close)
    body = abs(row.close - row.open)
    return upper >= max(body * 1.8, row.close * 0.025)


def _recent_cross_age(indicators: list[IndicatorPoint], left: str, right: str, lookback: int) -> int | None:
    for age in range(0, min(lookback, len(indicators) - 1) + 1):
        idx = len(indicators) - 1 - age
        prev = indicators[idx - 1]
        cur = indicators[idx]
        if crossed_up(getattr(prev, left), getattr(prev, right), getattr(cur, left), getattr(cur, right)):
            return age
    return None


def _find_rule(items: list[RuleResult], name: str) -> RuleResult | None:
    for item in items:
        if item.name == name:
            return item
    return None


def _chan_first_buy(rows: list[KLine], indicators: list[IndicatorPoint]) -> bool:
    if len(rows) < 60:
        return False
    recent = rows[-30:]
    prior = rows[-60:-30]
    price_new_low = min(row.low for row in recent[-10:]) <= min(row.low for row in prior)
    recent_hist = [p.macd_hist for p in indicators[-10:] if p.macd_hist is not None]
    prior_hist = [p.macd_hist for p in indicators[-40:-10] if p.macd_hist is not None]
    if not recent_hist or not prior_hist:
        return False
    momentum_divergence = min(recent_hist) > min(prior_hist)
    return price_new_low and momentum_divergence


def _recent_breakout(rows: list[KLine], indicators: list[IndicatorPoint], line: float) -> bool:
    del line
    return any(point.ma60 and row.close > point.ma60 for row, point in zip(rows[-4:], indicators[-4:]))
