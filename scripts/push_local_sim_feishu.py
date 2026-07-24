from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DATA = ROOT / "web_dashboard" / "data" / "local_sim" / "latest_account.json"
DEFAULT_STATE = ROOT / "data" / "local_sim" / "feishu_push_state.json"
PRIVATE_CONFIG_PATHS = [
    ROOT / "config_private.local.json",
    ROOT / "configs" / "config_private.local.json",
    ROOT / "configs" / "private.local.json",
]
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

HEADER_TEMPLATES = {
    "buy": "green",
    "sell": "red",
    "plan": "purple",
    "review": "blue",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Push local simulated broker updates to Feishu")
    parser.add_argument("--mode", choices=["plan", "trades", "review", "all"], default="all")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--timeout", type=int, default=8)
    args = parser.parse_args()

    payload = read_json(Path(args.data), {})
    state = read_state(Path(args.state))
    trade_date = args.trade_date or str(payload.get("trade_date") or today())
    runs: list[dict[str, Any]] = []
    exit_code = 0
    if args.mode in {"plan", "all"}:
        run = push_plan_card(
            payload=payload,
            state=state,
            trade_date=trade_date,
            dry_run=args.dry_run,
            force=args.force,
            timeout=args.timeout,
        )
        runs.append(run)
        if run["error_count"]:
            exit_code = 1
    if args.mode in {"trades", "all"}:
        run = push_trade_cards(
            payload=payload,
            state=state,
            trade_date=trade_date,
            dry_run=args.dry_run,
            force=args.force,
            timeout=args.timeout,
        )
        runs.append(run)
        if run["error_count"]:
            exit_code = 1
    if args.mode in {"review", "all"}:
        run = push_review_card(
            payload=payload,
            state=state,
            trade_date=trade_date,
            dry_run=args.dry_run,
            force=args.force,
            timeout=args.timeout,
        )
        runs.append(run)
        if run["error_count"]:
            exit_code = 1
    if any(run["sent_count"] for run in runs):
        write_json(Path(args.state), state)
    audit = {
        "schema_version": "chan520_local_sim_feishu_audit_v0",
        "generated_at": now_str(),
        "trade_date": trade_date,
        "data_path": str(Path(args.data)),
        "webhook": webhook_source_info(),
        "runs": runs,
    }
    write_audit_files(ROOT / "reports" / "local_sim_feishu" / trade_date, audit)
    print(json.dumps(summarize_audit(audit), ensure_ascii=False, sort_keys=True), flush=True)
    return exit_code


def push_trade_cards(
    *,
    payload: dict[str, Any],
    state: dict[str, Any],
    trade_date: str,
    dry_run: bool,
    force: bool,
    timeout: int,
) -> dict[str, Any]:
    pushed = state.setdefault("pushed_keys", {})
    webhook = "" if dry_run else resolve_webhook()[0]
    pilot = research_pilot(payload)
    fills = [
        {**row, "_account_scope": "core"} for row in payload.get("fills", [])
        if not trade_date or row_date(row) == trade_date
    ]
    fills.extend(
        {**row, "_account_scope": "bear_pilot", "_research_pilot": True}
        for row in pilot.get("fills", [])
        if not trade_date or row_date(row) == trade_date
    )
    orders_by_id = {
        ("core", str(row.get("order_id") or "")): row
        for row in payload.get("orders", [])
    }
    orders_by_id.update(
        {
            ("bear_pilot", str(row.get("order_id") or "")): row
            for row in pilot.get("orders", [])
        }
    )
    sent: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []
    for fill in sorted(fills, key=lambda row: str(row.get("created_at") or row.get("fill_id") or "")):
        scope = str(fill.get("_account_scope") or "core")
        key = f"fill:{scope}:{fill.get('fill_id')}"
        legacy_key = f"fill:{fill.get('fill_id')}" if scope == "core" else ""
        if not force and (key in pushed or (legacy_key and legacy_key in pushed)):
            skipped.append({"key": key, "reason": "already_pushed", "symbol": fill.get("symbol"), "side": fill.get("side")})
            continue
        order = orders_by_id.get((scope, str(fill.get("order_id") or "")), {})
        card = build_trade_card(payload, fill, order)
        cards.append({"key": key, "symbol": fill.get("symbol"), "side": fill.get("side")})
        if dry_run:
            skipped.append({"key": key, "reason": "dry_run", "symbol": fill.get("symbol"), "side": fill.get("side")})
            continue
        if not webhook:
            errors.append({"key": key, "error": "webhook_not_configured"})
            continue
        result = send_card(webhook, card, timeout=timeout)
        if result.get("ok"):
            pushed[key] = {"pushed_at": now_str(), "symbol": fill.get("symbol"), "side": fill.get("side")}
            sent.append({"key": key, "symbol": fill.get("symbol"), "side": fill.get("side")})
            time.sleep(0.5)
        else:
            errors.append({"key": key, "error": "feishu_response_not_ok", "response": result.get("response")})
    return {
        "type": "trades",
        "generated_at": now_str(),
        "trade_date": trade_date,
        "candidate_count": len(fills),
        "sent_count": len(sent),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "dry_run": dry_run,
        "force": force,
        "sent": sent,
        "skipped": skipped[:20],
        "errors": errors,
        "cards": cards[:20],
    }


def push_review_card(
    *,
    payload: dict[str, Any],
    state: dict[str, Any],
    trade_date: str,
    dry_run: bool,
    force: bool,
    timeout: int,
) -> dict[str, Any]:
    pushed = state.setdefault("pushed_keys", {})
    webhook = "" if dry_run else resolve_webhook()[0]
    key = f"review:{trade_date}"
    evidence_fingerprint = review_evidence_fingerprint(payload, trade_date)
    previous = pushed.get(key) if isinstance(pushed.get(key), dict) else {}
    sent: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    card = build_review_card(payload, trade_date)
    pilot = research_pilot(payload)
    pilot_valuation_incomplete = bool(pilot.get("positions")) and pilot.get("valuation_complete") is False
    if payload.get("valuation_complete") is False or pilot_valuation_incomplete:
        errors.append({"key": key, "error": "valuation_incomplete", "valuation_status": payload.get("valuation_status")})
    elif (
        not force
        and key in pushed
        and (
            not previous.get("evidence_fingerprint")
            or previous.get("evidence_fingerprint") == evidence_fingerprint
        )
    ):
        skipped.append({"key": key, "reason": "already_pushed"})
    elif dry_run:
        skipped.append({"key": key, "reason": "dry_run"})
    elif not webhook:
        errors.append({"key": key, "error": "webhook_not_configured"})
    else:
        result = send_card(webhook, card, timeout=timeout)
        if result.get("ok"):
            pushed[key] = {
                "pushed_at": now_str(),
                "trade_date": trade_date,
                "evidence_fingerprint": evidence_fingerprint,
            }
            sent.append({"key": key})
        else:
            errors.append({"key": key, "error": "feishu_response_not_ok", "response": result.get("response")})
    return {
        "type": "review",
        "generated_at": now_str(),
        "trade_date": trade_date,
        "sent_count": len(sent),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "dry_run": dry_run,
        "force": force,
        "evidence_fingerprint": evidence_fingerprint,
        "sent": sent,
        "skipped": skipped,
        "errors": errors,
        "card_title": card["card"]["header"]["title"]["content"],
    }


def review_evidence_fingerprint(payload: dict[str, Any], trade_date: str) -> str:
    account = payload.get("account") or {}
    snapshot = payload.get("session_market_snapshot") or {}
    market = snapshot.get("market_regime") or {}
    quality = snapshot.get("scan_quality") or {}
    replay = payload.get("counterfactual_replay") or {}
    all_summary = replay.get("all_candidate_close_summary") or {}
    ranked = replay.get("all_candidate_ranked_portfolio") or {}
    evidence = {
        "trade_date": trade_date,
        "account": {
            "total_equity": account.get("total_equity"),
            "fill_count": account.get("fill_count"),
            "open_position_count": account.get("open_position_count"),
            "valuation_status": payload.get("valuation_status"),
        },
        "session_market": {
            "status": snapshot.get("status"),
            "state": market.get("state"),
            "regime_ok": market.get("regime_ok"),
            "scan_rows": snapshot.get("scan_rows"),
            "research_coverage_pass": quality.get("research_coverage_pass"),
            "execution_coverage_pass": quality.get("execution_coverage_pass"),
        },
        "replay": {
            "status": replay.get("status"),
            "candidate_count": all_summary.get("candidate_count"),
            "available_count": all_summary.get("available_count"),
            "mean_close_return_pct": all_summary.get("mean_close_return_pct"),
            "ranked_filled_count": ranked.get("filled_count"),
            "ranked_net_mark_pnl": ranked.get("net_mark_pnl"),
        },
    }
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def push_plan_card(
    *,
    payload: dict[str, Any],
    state: dict[str, Any],
    trade_date: str,
    dry_run: bool,
    force: bool,
    timeout: int,
) -> dict[str, Any]:
    pushed = state.setdefault("pushed_keys", {})
    webhook = "" if dry_run else resolve_webhook()[0]
    key = f"plan:{trade_date}"
    sent: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    card = build_plan_card(payload, trade_date)
    if not force and key in pushed:
        skipped.append({"key": key, "reason": "already_pushed"})
    elif dry_run:
        skipped.append({"key": key, "reason": "dry_run"})
    elif not webhook:
        errors.append({"key": key, "error": "webhook_not_configured"})
    else:
        result = send_card(webhook, card, timeout=timeout)
        if result.get("ok"):
            pushed[key] = {"pushed_at": now_str(), "trade_date": trade_date}
            sent.append({"key": key})
        else:
            errors.append({"key": key, "error": "feishu_response_not_ok", "response": result.get("response")})
    return {
        "type": "plan",
        "generated_at": now_str(),
        "trade_date": trade_date,
        "sent_count": len(sent),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "dry_run": dry_run,
        "force": force,
        "sent": sent,
        "skipped": skipped,
        "errors": errors,
        "card_title": card["card"]["header"]["title"]["content"],
    }


def build_trade_card(payload: dict[str, Any], fill: dict[str, Any], order: dict[str, Any] | None = None) -> dict[str, Any]:
    order = order or {}
    side = str(fill.get("side") or "").upper()
    kind = "buy" if side == "BUY" else "sell"
    side_label = "买入成交" if side == "BUY" else "卖出成交"
    fee = safe_float(fill.get("commission")) + safe_float(fill.get("stamp_duty"))
    pilot = research_pilot(payload)
    is_pilot = bool(fill.get("_research_pilot"))
    account = (pilot.get("account") or {}) if is_pilot else (payload.get("account") or {})
    account_label = "熊市研究小仓" if is_pilot else "核心模拟盘"
    elements = [
        div(f"**{side_label}｜{account_label}**\n{stock_label(fill)}"),
        fields_block(
            [
                ("代码", fill.get("symbol") or "-"),
                ("名称", fill.get("stock_name") or "-"),
                ("方向", "买入" if side == "BUY" else "卖出"),
                ("数量", f"{safe_int(fill.get('volume'))} 股"),
                ("成交价", price(fill.get("price"))),
                ("成交金额", yuan(fill.get("gross"))),
                ("交易费用", yuan(fee)),
            ]
        ),
        fields_block(
            [
                ("成交时间", short_time(fill.get("created_at"))),
                ("订单ID", order.get("client_order_id") or fill.get("order_id") or "-"),
                ("交易日", order.get("session_date") or row_date(fill) or "-"),
                ("账户现金", yuan(account.get("cash"))),
                ("总资产", yuan(account.get("total_equity"))),
                ("当前仓位", pct(account.get("gross_exposure_pct"))),
            ]
        ),
        div(f"**{trade_reason_label(fill)}**\n{trade_reason_text(fill, order)}"),
        {"tag": "hr"},
        div(f"说明：这是 Chan520 {account_label}成交记录，不连接真实券商账户；研究小仓不影响核心账户。"),
    ]
    return card_payload(kind, f"Chan520 {account_label}｜{side_label}", elements)


def build_plan_card(payload: dict[str, Any], trade_date: str) -> dict[str, Any]:
    plans = [row for row in payload.get("planned_orders", []) if str(row.get("trade_date") or "") == trade_date]
    executable = [row for row in plans if str(row.get("status") or "") in {"WATCH_TRIGGER", "CONFIRMED_TRIGGER"}]
    watches = [row for row in plans if str(row.get("status") or "") == "WATCH_ONLY"]
    risks = [row for row in plans if str(row.get("status") or "") in {"RISK_CANDIDATE", "RISK_CONFIRMED"}]
    core = payload.get("core_plan") if isinstance(payload.get("core_plan"), dict) else {}
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), dict) else {}
    quality = core.get("scan_quality") if isinstance(core.get("scan_quality"), dict) else {}
    supplemental = core.get("supplemental_market_context") if isinstance(core.get("supplemental_market_context"), dict) else {}
    style = core.get("candidate_style_diagnostic") if isinstance(core.get("candidate_style_diagnostic"), dict) else {}
    funnel = core.get("execution_funnel") if isinstance(core.get("execution_funnel"), dict) else {}
    pilot = research_pilot(payload)
    pilot_plans = [
        row for row in pilot.get("planned_orders", [])
        if str(row.get("trade_date") or "") == trade_date
        and str(row.get("status") or "") in {"WATCH_TRIGGER", "CONFIRMED_TRIGGER"}
    ]
    pilot_cohort = {
        "position_cap_pct": pilot.get("position_cap_pct"),
        "account_exposure_cap_pct": pilot.get("account_exposure_cap_pct"),
        "max_positions": pilot.get("max_positions"),
        **((core.get("research_cohorts") or {}).get("bear_pilot") or {}),
    }
    elements = [
        div(f"**盘前核心交易计划**\n{trade_date}"),
        fields_block(
            [
                ("可执行买入", f"{len(executable)} 只"),
                ("仅观察", f"{len(watches)} 只"),
                ("风控候选", f"{len(risks)} 只"),
                ("计划状态", core.get("status") or "-"),
                ("研究覆盖率", pct(quality.get("coverage"))),
                ("执行级覆盖率", pct(quality.get("execution_coverage"))),
                ("市场状态", (core.get("market_regime") or {}).get("state") or "-"),
                ("同花顺旁路", supplemental.get("emotion_label") or supplemental.get("status") or "-"),
                ("风险闭环", ready_label(readiness.get("local_sim_risk_loop_ready"))),
                ("新增买入", ready_label(readiness.get("local_sim_buy_entry_ready"))),
                ("几何拦截", f"{safe_int(core.get('geometry_blocked_count'))} 只"),
                ("熊市研究小仓", f"{len(pilot_plans)} 只"),
            ]
        ),
        div("**执行漏斗**\n" + execution_funnel_lines(funnel)),
        div("**执行边界**\n" + core_plan_boundary(core)),
        div("**市场宽度与候选风格（诊断，不参与放宽入场）**\n" + style_diagnostic_lines(style)),
        {"tag": "hr"},
        div("**T+1/风控优先**\n" + planned_order_lines(risks)),
        div("**严格待触发**\n" + planned_order_lines(executable)),
        div("**观察池（不会自动成交）**\n" + planned_order_lines(watches)),
        div("**熊市研究小仓（独立账户，不影响核心）**\n" + bear_pilot_plan_lines(pilot_plans, pilot_cohort)),
        div("说明：盘前计划只使用上一交易日完整日线；盘中仍需二阶段确认与市场风险门。"),
    ]
    return card_payload("plan", "Chan520 本地模拟盘｜盘前核心计划", elements)


def core_plan_boundary(core: dict[str, Any]) -> str:
    if not core:
        return "未找到核心计划摘要，自动买入保持关闭。"
    if core.get("status") != "PASS":
        reason = compact_text(core.get("failure_step"), core.get("failure_reason"))
        suffix = f" 失败原因：{reason}" if reason != "-" else ""
        return f"计划已 fail-closed；不会新增买入，只执行现有持仓风控。{suffix}"
    if safe_int(core.get("executable_buy_count")) <= 0:
        return "没有通过严格门槛的可执行买入；观察池不会自动成交。"
    return f"仅 {safe_int(core.get('executable_buy_count'))} 只严格候选可进入盘中二阶段确认。"


def execution_funnel_lines(funnel: dict[str, Any]) -> str:
    if not funnel:
        return "本次未记录执行漏斗。"
    return (
        f"扫描 {safe_int(funnel.get('scanned_count'))} → 严格 {safe_int(funnel.get('strict_count'))} → "
        f"观察 {safe_int(funnel.get('watch_count'))} → 核心可执行 {safe_int(funnel.get('core_executable_count'))} → "
        f"熊市研究小仓 {safe_int(funnel.get('bear_pilot_count'))}。"
    )


def bear_pilot_plan_lines(rows: list[dict[str, Any]], cohort: dict[str, Any]) -> str:
    boundary = (
        f"状态 {cohort.get('status') or '-'}；单票上限 {pct(cohort.get('position_cap_pct'))}，"
        f"账户总仓上限 {pct(cohort.get('account_exposure_cap_pct'))}，最多 {safe_int(cohort.get('max_positions'))} 只，"
        "仅本地模拟研究，不连接 GM。"
    )
    return boundary + "\n" + planned_order_lines(rows)


def style_diagnostic_lines(style: dict[str, Any]) -> str:
    if not style or style.get("status") == "UNAVAILABLE":
        return "本次未取得完整风格诊断。"
    market = style.get("market_breadth") or {}
    candidate = style.get("candidate_breadth") or {}
    top = "、".join(str(row.get("sector")) for row in (style.get("top_industries") or [])[:3]) or "-"
    return (
        f"全市场上涨占比 {pct(market.get('up_ratio'))}，候选上涨占比 {pct(candidate.get('up_ratio'))}；"
        f"强势行业 {top}；Top3 行业重合 {pct(style.get('top3_industry_overlap_ratio'))}；"
        f"偏离预警 {'是' if style.get('mismatch_alert') else '否'}。"
    )


def build_review_card(payload: dict[str, Any], trade_date: str) -> dict[str, Any]:
    account = payload.get("account") or {}
    daily = next((row for row in payload.get("daily", []) if str(row.get("trade_date") or "") == trade_date), {})
    fills = [row for row in payload.get("fills", []) if row_date(row) == trade_date]
    positions = list(payload.get("positions") or [])
    plans = [row for row in payload.get("planned_orders", []) if str(row.get("trade_date") or "") in {"", trade_date}]
    replay = payload.get("counterfactual_replay") if isinstance(payload.get("counterfactual_replay"), dict) else {}
    session_market = (
        payload.get("session_market_snapshot")
        if isinstance(payload.get("session_market_snapshot"), dict)
        else {}
    )
    pilot = research_pilot(payload)
    pilot_account = pilot.get("account") or {}
    pilot_positions = list(pilot.get("positions") or [])
    pilot_fills = [row for row in pilot.get("fills", []) if row_date(row) == trade_date]
    pilot_plans = [row for row in pilot.get("planned_orders", []) if str(row.get("trade_date") or "") == trade_date]
    elements = [
        div(f"**每日账户复盘**\n{trade_date}"),
        fields_block(
            [
                ("初始资金", yuan(account.get("initial_cash"))),
                ("总资产", yuan(account.get("total_equity"))),
                ("账户盈亏", pnl_text(account.get("total_pnl"), account.get("total_pnl_pct"))),
                ("可用现金", yuan(account.get("cash"))),
                ("持仓市值", yuan(account.get("market_value"))),
                ("当前仓位", pct(account.get("gross_exposure_pct"))),
            ]
        ),
        fields_block(
            [
                ("当日成交", f"{len(fills)} 笔"),
                ("买入金额", yuan(daily.get("buy_gross", 0))),
                ("卖出金额", yuan(daily.get("sell_gross", 0))),
                ("交易费用", yuan(daily.get("fees", 0))),
                ("累计订单", str(account.get("order_count", 0))),
                ("累计成交", str(account.get("fill_count", 0))),
            ]
        ),
        {"tag": "hr"},
        div("**交易日收盘市场状态**\n" + session_market_lines(session_market)),
        div("**待触发/计划订单**\n" + planned_order_lines(plans)),
        div("**持仓明细**\n" + position_lines(positions)),
        div("**当日成交明细**\n" + fill_lines(fills)),
        div("**当日理由分布**\n" + reason_summary_lines(fills)),
        div("**熊市研究小仓账户（独立于核心）**\n" + bear_pilot_review_lines(pilot_account, pilot_positions, pilot_fills, pilot_plans)),
        div("**观察池与熊市子集反事实回放（仅研究）**\n" + counterfactual_lines(replay)),
        div(f"说明：估值口径 {payload.get('valuation_basis') or '-'}，状态 {payload.get('valuation_status') or '-'}。估值不完整时本复盘会 fail-closed，不会推送误导性盈亏。"),
    ]
    return card_payload("review", "Chan520 本地模拟盘｜每日账户复盘", elements)


def session_market_lines(snapshot: dict[str, Any]) -> str:
    if not snapshot:
        return "收盘行情快照尚未生成；复盘证据不完整。"
    regime = snapshot.get("market_regime") or {}
    quality = snapshot.get("scan_quality") or {}
    return (
        f"状态 {regime.get('state') or 'UNKNOWN'}，门控 {'通过' if regime.get('regime_ok') else '阻断'}；"
        f"{regime.get('detail') or '-'}\n"
        f"研究覆盖 {pct(quality.get('research_coverage', quality.get('coverage')))}，"
        f"执行级覆盖 {pct(quality.get('execution_coverage'))}，"
        f"收盘扫描 {safe_int(snapshot.get('scan_rows'))} 只，证据状态 {snapshot.get('status') or '-'}。"
    )


def bear_pilot_review_lines(
    account: dict[str, Any],
    positions: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    plans: list[dict[str, Any]],
) -> str:
    if not account:
        return "研究小仓尚未初始化；核心账户不受影响。"
    lines = [
        f"总资产 {yuan(account.get('total_equity'))}，盈亏 {pnl_text(account.get('total_pnl'), account.get('total_pnl_pct'))}，"
        f"仓位 {pct(account.get('gross_exposure_pct'))}，当日成交 {len(fills)} 笔，待触发 {sum(str(row.get('status') or '') in {'WATCH_TRIGGER', 'CONFIRMED_TRIGGER'} for row in plans)} 只。",
        "持仓：" + position_lines(positions),
        "成交：" + fill_lines(fills),
    ]
    return "\n".join(lines)


def research_pilot(payload: dict[str, Any]) -> dict[str, Any]:
    pilot = payload.get("research_pilot")
    return pilot if isinstance(pilot, dict) and pilot.get("status") == "ACTIVE" else {}


def counterfactual_lines(replay: dict[str, Any]) -> str:
    if not replay:
        return "未生成研究回放；不影响实际订单与账户。"
    if replay.get("status") == "FAIL_CLOSED":
        return f"数据不完整，回放已关闭：{replay.get('error') or '-'}"
    lines = [
        f"状态 {replay.get('status') or '-'}，研究市场状态 {replay.get('research_market_regime') or '-'}，"
        f"熊市子集 {safe_int(replay.get('candidate_count'))} 只，子集排序前两笔触发 {safe_int(replay.get('filled_count'))} 只，"
        f"收盘净盯市 {pnl_text(replay.get('net_mark_pnl'), replay.get('net_mark_return_on_equity'))}。",
        "该结果不写入模拟盘账本，不代表应当放宽熊市禁买门槛。",
    ]
    independent = replay.get("individual_candidate_results") or []
    triggered = sum(safe_int(row.get("filled_count")) > 0 for row in independent)
    sensitivity = replay.get("ordering_sensitivity") or {}
    lines.append(
        f"逐票独立触发 {triggered}/{len(independent)}；排序敏感性净盈亏区间 "
        f"{yuan(sensitivity.get('worst_net_mark_pnl'))} 至 {yuan(sensitivity.get('best_net_mark_pnl'))}，"
        f"极差 {yuan(sensitivity.get('spread_net_mark_pnl'))}。"
    )
    for row in (replay.get("fills") or [])[:4]:
        lines.append(
            f"- {stock_label(row)} {row.get('fill_minute') or '-'} @ {price(row.get('fill_price'))}，收盘 {price(row.get('close_price'))}，净盯市 {pnl_text(row.get('net_mark_pnl'))}"
        )
    all_summary = replay.get("all_candidate_close_summary") or {}
    if all_summary:
        lines.append(
            f"全观察池收盘覆盖 {safe_int(all_summary.get('available_count'))}/{safe_int(all_summary.get('candidate_count'))}，"
            f"平均 {pct_points(all_summary.get('mean_close_return_pct'))}，中位 {pct_points(all_summary.get('median_close_return_pct'))}；"
            f"几何有效平均 {pct_points(all_summary.get('geometry_valid_mean_close_return_pct'))}，"
            f"几何无效平均 {pct_points(all_summary.get('invalid_geometry_mean_close_return_pct'))}。"
        )
    full_pool = replay.get("all_candidate_ranked_portfolio") or {}
    if full_pool:
        lines.append(
            f"全池几何有效优先组合：候选 {safe_int(full_pool.get('candidate_count'))} 只，"
            f"前两笔触发 {safe_int(full_pool.get('filled_count'))} 只，"
            f"净盯市 {pnl_text(full_pool.get('net_mark_pnl'), full_pool.get('net_mark_return_on_equity'))}。"
        )
    return "\n".join(lines)


def position_lines(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "暂无持仓。"
    lines = []
    for row in rows[:8]:
        lines.append(
            f"- {stock_label(row)}: {safe_int(row.get('shares'))} 股，成本 {price(row.get('average_price'))}，市值 {yuan(row.get('market_value'))}，浮盈亏 {pnl_text(row.get('unrealized_pnl'), row.get('unrealized_pnl_pct'))}，峰值 {pct(row.get('peak_unrealized_pnl_pct'))}，利润保护 {'已武装' if row.get('profit_protection_armed') else '未武装'}，入场 {position_entry_reason(row)}"
        )
    if len(rows) > 8:
        lines.append(f"- 另有 {len(rows) - 8} 个持仓略。")
    return "\n".join(lines)


def fill_lines(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "今日暂无成交。"
    lines = []
    for row in rows[:8]:
        side = "买入" if str(row.get("side") or "").upper() == "BUY" else "卖出"
        lines.append(
            f"- {short_time(row.get('created_at'))} {side} {stock_label(row)} {safe_int(row.get('volume'))} 股 @ {price(row.get('price'))}，{trade_reason_label(row)}：{trade_reason_text(row)}"
        )
    if len(rows) > 8:
        lines.append(f"- 另有 {len(rows) - 8} 笔成交略。")
    return "\n".join(lines)


def planned_order_lines(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "暂无待触发计划订单。"
    lines = []
    for row in rows[:8]:
        side = "买入" if str(row.get("side") or "").upper() == "BUY" else "卖出"
        lines.append(
            f"- {side} {stock_label(row)} {safe_int(row.get('volume'))} 股，状态 {row.get('status') or '-'}，触发 {price(row.get('lower_price'))}-{price(row.get('upper_price') or row.get('trigger_price'))}，几何 {geometry_label(row)}，T+1缓冲 {pct((row.get('payload') or {}).get('t1_loss_buffer_pct'))}，理由 {row.get('reason_text') or '-'}"
        )
    if len(rows) > 8:
        lines.append(f"- 另有 {len(rows) - 8} 条计划略。")
    return "\n".join(lines)


def trade_reason_label(row: dict[str, Any]) -> str:
    side = str(row.get("side") or "").upper()
    if side == "BUY":
        return "入场理由"
    if side == "SELL":
        return "离场/风控理由"
    return "交易理由"


def trade_reason_text(row: dict[str, Any], order: dict[str, Any] | None = None) -> str:
    order = order or {}
    side = str(row.get("side") or order.get("side") or "").upper()
    if side == "BUY":
        return compact_text(pick_reason(row, order, "signal_name"), pick_reason(row, order, "entry_reason"), pick_reason(row, order, "notes"))
    if side == "SELL":
        return compact_text(
            pick_reason(row, order, "risk_reason_code"),
            pick_reason(row, order, "risk_reason"),
            pick_reason(row, order, "exit_reason"),
            pick_reason(row, order, "notes"),
        )
    return compact_text(
        pick_reason(row, order, "signal_name"),
        pick_reason(row, order, "entry_reason"),
        pick_reason(row, order, "exit_reason"),
        pick_reason(row, order, "risk_reason"),
        pick_reason(row, order, "notes"),
    )


def position_entry_reason(row: dict[str, Any]) -> str:
    return compact_text(row.get("signal_name"), row.get("entry_reason"), row.get("entry_notes"))


def ready_label(value: Any) -> str:
    return "READY" if value is True else "BLOCKED" if value is False else "-"


def geometry_label(row: dict[str, Any]) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    valid = payload.get("geometry_valid")
    return "有效" if valid is True else "无效" if valid is False else "-"


def stock_label(row: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or "-")
    name = str(row.get("stock_name") or "").strip()
    return f"{symbol} {name}".strip()


def pnl_text(value: Any, pct_value: Any = None) -> str:
    amount = safe_float(value)
    pct_part = f" / {pct(pct_value)}" if pct_value is not None else ""
    sign = "+" if amount > 0 else ""
    return f"{sign}{yuan(amount)}{pct_part}"


def reason_summary_lines(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "今日暂无理由记录。"
    counts: dict[str, int] = {}
    for row in rows:
        label = trade_reason_label(row)
        reason = trade_reason_text(row)
        key = f"{label}：{reason}"
        counts[key] = counts.get(key, 0) + 1
    return "\n".join(f"- {key}（{count} 笔）" for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:8])


def compact_text(*parts: Any) -> str:
    text = "｜".join(str(item).strip() for item in parts if str(item or "").strip())
    return text or "未记录"


def pick_reason(row: dict[str, Any], order: dict[str, Any], key: str) -> Any:
    value = row.get(key)
    return value if str(value or "").strip() else order.get(key)


def send_card(
    webhook: str,
    payload: dict[str, Any],
    *,
    timeout: int = 8,
    attempts: int = 4,
    backoff_seconds: float = 1.0,
) -> dict[str, Any]:
    last_result: dict[str, Any] = {"ok": False, "error": "not_attempted"}
    for attempt in range(1, max(attempts, 1) + 1):
        last_result = _send_card_once(webhook, payload, timeout=timeout)
        last_result["attempts"] = attempt
        if last_result.get("ok") or not retryable_feishu_result(last_result) or attempt >= attempts:
            return last_result
        time.sleep(backoff_seconds * (2 ** (attempt - 1)))
    return last_result


def _send_card_once(webhook: str, payload: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-configured Feishu webhook.
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": type(exc).__name__}
    try:
        response = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": bool(text), "raw_response": text[:200]}
    ok = response.get("code") in (0, "0", None) and response.get("StatusCode") in (0, "0", None)
    if str(response.get("msg", "")).lower() not in {"", "success", "ok"}:
        ok = ok and response.get("code") in (0, "0")
    return {"ok": bool(ok), "response": response}


def retryable_feishu_result(result: dict[str, Any]) -> bool:
    if result.get("error"):
        return True
    response = result.get("response") if isinstance(result.get("response"), dict) else {}
    try:
        code = int(response.get("code"))
    except (TypeError, ValueError):
        code = 0
    message = str(response.get("msg") or "").lower()
    return code in {11232, 99991663} or "frequency" in message or "rate" in message


def resolve_webhook() -> tuple[str, str]:
    for key in ("FEISHU_LOCAL_SIM_WEBHOOK_URL", "FEISHU_LOCAL_SIM_WEBHOOK", "FEISHU_PAPER_WEBHOOK_URL", "FEISHU_PAPER_WEBHOOK"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value, f"env:{key}"
    for path in PRIVATE_CONFIG_PATHS:
        payload = read_json(path, {})
        if not isinstance(payload, dict):
            continue
        feishu = payload.get("feishu") or {}
        if isinstance(feishu, dict):
            for key in ("local_sim_webhook", "paper_trading_webhook", "webhook"):
                value = str(feishu.get(key) or "").strip()
                if value:
                    return value, f"{path.name}:feishu.{key}"
        for key in ("FEISHU_LOCAL_SIM_WEBHOOK_URL", "FEISHU_LOCAL_SIM_WEBHOOK", "FEISHU_WEBHOOK_URL", "FEISHU_WEBHOOK"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value, f"{path.name}:{key}"
    for key in ("FEISHU_WEBHOOK_URL", "FEISHU_WEBHOOK"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value, f"env:{key}"
    return "", ""


def webhook_source_info() -> dict[str, str]:
    value, source = resolve_webhook()
    return {"configured": bool(value), "source": source, "fingerprint": webhook_fingerprint(value)}


def webhook_fingerprint(value: str) -> str:
    token = str(value or "").strip().rsplit("/", 1)[-1]
    if not token:
        return ""
    if len(token) <= 12:
        return "***"
    return f"{token[:8]}...{token[-4:]}"


def card_payload(kind: str, title: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": HEADER_TEMPLATES.get(kind, "blue"),
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": elements,
        },
    }


def div(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def field(label: str, value: Any) -> dict[str, Any]:
    return {"is_short": True, "text": {"tag": "lark_md", "content": f"**{label}**\n{value if value not in (None, '') else '-'}"}}


def fields_block(rows: list[tuple[str, Any]]) -> dict[str, Any]:
    return {"tag": "div", "fields": [field(label, value) for label, value in rows]}


def read_state(path: Path) -> dict[str, Any]:
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("schema_version", "chan520_local_sim_feishu_state_v0")
    payload.setdefault("pushed_keys", {})
    return payload


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def write_audit_files(root: Path, audit: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    generated_at = str(audit.get("generated_at") or now_str())
    stamp = "".join(ch for ch in generated_at if ch.isdigit())[:14] or datetime.now(SHANGHAI_TZ).strftime("%Y%m%d%H%M%S")
    runs = [row for row in audit.get("runs", []) if isinstance(row, dict) and row.get("type")]
    for run in runs:
        mode = str(run["type"])
        mode_audit = {**audit, "runs": [run]}
        write_json(root / f"feishu_push_audit_{mode}.json", mode_audit)
        write_json(root / "runs" / f"{stamp}_{mode}.json", mode_audit)

    aggregate_path = root / "feishu_push_audit.json"
    previous = read_json(aggregate_path, {})
    latest_by_type = previous.get("latest_by_type") if isinstance(previous.get("latest_by_type"), dict) else {}
    if not latest_by_type:
        latest_by_type = {
            str(row.get("type")): row
            for row in previous.get("runs", [])
            if isinstance(row, dict) and row.get("type")
        }
    latest_by_type.update({str(row["type"]): row for row in runs})
    history = previous.get("history") if isinstance(previous.get("history"), list) else []
    history.append({"generated_at": generated_at, "runs": runs})
    mode_order = {"plan": 0, "trades": 1, "review": 2}
    aggregate = {
        **audit,
        "schema_version": "chan520_local_sim_feishu_audit_v1",
        "runs": sorted(latest_by_type.values(), key=lambda row: mode_order.get(str(row.get("type")), 99)),
        "latest_by_type": latest_by_type,
        "history": history[-200:],
    }
    write_json(aggregate_path, aggregate)


def summarize_audit(audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_date": audit.get("trade_date"),
        "webhook_configured": bool((audit.get("webhook") or {}).get("configured")),
        "runs": [
            {
                "type": run.get("type"),
                "sent_count": run.get("sent_count"),
                "skipped_count": run.get("skipped_count"),
                "error_count": run.get("error_count"),
                "dry_run": run.get("dry_run"),
            }
            for run in audit.get("runs", [])
        ],
    }


def row_date(row: dict[str, Any]) -> str:
    session_date = str(row.get("session_date") or "").strip()
    if session_date:
        return session_date
    text = str(row.get("created_at") or "")
    return text[:10] if len(text) >= 10 else ""


def today() -> str:
    return datetime.now(SHANGHAI_TZ).date().isoformat()


def now_str() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def yuan(value: Any) -> str:
    num = safe_float(value, float("nan"))
    if num != num:
        return "-"
    sign = "-" if num < 0 else ""
    num = abs(num)
    if num >= 10000:
        return f"{sign}{num / 10000:.2f} 万元"
    return f"{sign}{num:.2f} 元"


def price(value: Any) -> str:
    num = safe_float(value, float("nan"))
    return "-" if num != num else f"{num:.2f}"


def pct(value: Any) -> str:
    num = safe_float(value, float("nan"))
    return "-" if num != num else f"{num * 100:.2f}%"


def pct_points(value: Any) -> str:
    num = safe_float(value, float("nan"))
    return "-" if num != num else f"{num:+.2f}%"


def short_time(value: Any) -> str:
    text = str(value or "").strip()
    return text.replace("T", " ")[:19] if text else "-"


if __name__ == "__main__":
    raise SystemExit(main())
