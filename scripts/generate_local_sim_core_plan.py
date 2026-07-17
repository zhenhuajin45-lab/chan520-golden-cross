from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.broker_adapter import LocalSimBrokerAdapter, LocalSimBrokerConfig  # noqa: E402
from chan520_skill.data import auto_history, trim_to_date  # noqa: E402
from chan520_skill.regime import fetch_regime, index_history  # noqa: E402
from chan520_skill.scanner import scan_market  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")
POLICY_ID = "local_sim_core_plan_v2"
ACTIVE_BUY_STATUSES = (
    "PLANNED",
    "WATCH_TRIGGER",
    "CONFIRMED_TRIGGER",
    "REJECTED_STALE_QUOTE",
    "REJECTED_NOT_TRIGGERED",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a fail-closed local simulated core plan from the prior session close")
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--signal-date", default="")
    parser.add_argument("--ledger", default="data/local_sim/broker.sqlite")
    parser.add_argument("--account-id", default="local-sim")
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--max-buy-plans", type=int, default=2)
    parser.add_argument("--max-new-exposure-pct", type=float, default=0.15)
    parser.add_argument("--risk-per-plan-pct", type=float, default=0.005)
    parser.add_argument("--refresh-scan-if-missing", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    trade_date = date.fromisoformat(args.trade_date)
    verify_trade_date(trade_date)
    signal_date = resolve_signal_date(trade_date, args.signal_date)
    scan_path = ROOT / "reports" / f"scan_{signal_date.isoformat()}" / f"market_scan_{signal_date.isoformat()}.csv"
    scan_stats = None
    if not scan_path.exists() and args.refresh_scan_if_missing:
        _csv_path, _md_path, scan_stats = scan_market(signal_date, scan_path.parent, max_workers=16)
    if not scan_path.exists():
        raise SystemExit(f"FAIL_CLOSED: prior-session scan missing: {scan_path}")

    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id=args.account_id, initial_cash=args.initial_cash, ledger_path=args.ledger)
    )
    adapter.initialize_account()
    regime = resolve_market_regime(signal_date)
    rows = read_scan(scan_path)
    scan_quality = resolve_scan_quality(scan_path, scan_stats)
    payload = generate_plan(
        adapter=adapter,
        ledger=Path(args.ledger),
        account_id=args.account_id,
        trade_date=trade_date,
        signal_date=signal_date,
        scan_path=scan_path,
        scan_rows=rows,
        regime=regime,
        scan_quality=scan_quality,
        max_candidates=args.max_candidates,
        max_buy_plans=args.max_buy_plans,
        max_new_exposure_pct=args.max_new_exposure_pct,
        risk_per_plan_pct=args.risk_per_plan_pct,
    )
    output = Path(args.output) if args.output else ROOT / "reports" / "local_sim_plan" / trade_date.strftime("%Y%m%d") / "core_plan.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary(payload), ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if payload["status"] == "PASS" else 2


def resolve_signal_date(trade_date: date, supplied: str = "") -> date:
    if supplied:
        signal_date = date.fromisoformat(supplied)
        if signal_date >= trade_date:
            raise ValueError("signal_date must be earlier than trade_date")
        return signal_date
    rows = index_history("000001", trade_date)
    prior = [row.date for row in rows if row.date < trade_date]
    if not prior:
        raise RuntimeError(f"FAIL_CLOSED: no prior trading session before {trade_date}")
    return max(prior)


def verify_trade_date(trade_date: date) -> None:
    try:
        import akshare as ak

        frame = ak.tool_trade_date_hist_sina()
        sessions = {item.date() if hasattr(item, "date") else date.fromisoformat(str(item)[:10]) for item in frame["trade_date"]}
    except Exception as exc:  # noqa: BLE001 - unavailable calendar must fail closed.
        raise RuntimeError(f"FAIL_CLOSED: trading calendar unavailable: {type(exc).__name__}: {exc}") from exc
    if trade_date not in sessions:
        raise RuntimeError(f"FAIL_CLOSED: {trade_date} is not an A-share trading session")


def resolve_market_regime(signal_date: date) -> dict[str, Any]:
    try:
        state = fetch_regime("000001", signal_date, adjust=0)
    except Exception as exc:  # noqa: BLE001 - plan generation must record an unavailable regime.
        return {"state": "UNKNOWN", "regime_ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    mapped = {"trend_up": "BULL", "range": "NORMAL", "down": "BEAR"}.get(state.regime, "UNKNOWN")
    return {"state": mapped, "regime_ok": mapped in {"BULL", "NORMAL"}, "detail": state.detail}


def read_scan(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def generate_plan(
    *,
    adapter: LocalSimBrokerAdapter,
    ledger: Path,
    account_id: str,
    trade_date: date,
    signal_date: date,
    scan_path: Path,
    scan_rows: list[dict[str, str]],
    regime: dict[str, Any],
    scan_quality: dict[str, Any] | None = None,
    max_candidates: int,
    max_buy_plans: int,
    max_new_exposure_pct: float,
    risk_per_plan_pct: float,
) -> dict[str, Any]:
    expired = expire_old_buy_plans(ledger, account_id, trade_date.isoformat())
    account = adapter.account_snapshot()
    equity = float(account["cash"]) + sum(float(row["shares"]) * float(row["average_price"]) for row in account["positions"])
    # The current Monte Carlo evidence does not support alpha-score ranking for
    # execution. Freeze selection to symbol order until ranking is revalidated.
    ordered = sorted(scan_rows, key=lambda row: str(row.get("code") or ""))
    strict_rows = [row for row in ordered if strict_scan_pass(row)][:max_candidates]
    watch_rows = [row for row in ordered if watch_scan_pass(row) and row not in strict_rows][:max_candidates]
    created: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    planned_gross = 0.0
    scan_quality = scan_quality or {"coverage_pass": True, "coverage": 1.0, "minimum_coverage": 0.85}
    strict_enabled = bool(regime.get("regime_ok")) and bool(scan_quality.get("coverage_pass"))

    for rank, row in enumerate(strict_rows, start=1):
        levels = candidate_levels(row, signal_date)
        zone = entry_zone(row, levels)
        code = str(row.get("code") or "")
        close = safe_float(row.get("close"))
        reasons = [*levels["reason_codes"], *zone["reason_codes"]]
        hard_pass = (
            strict_enabled
            and levels["rr"] >= 2.0
            and levels["target"] > close
            and zone["geometry_valid"]
        )
        if not strict_enabled:
            if not regime.get("regime_ok"):
                reasons.append("MARKET_REGIME_BLOCKED")
            if not scan_quality.get("coverage_pass"):
                reasons.append("SCAN_COVERAGE_BLOCKED")
        if levels["rr"] < 2.0:
            reasons.append("RR_TOO_LOW")
        shares = 0
        if hard_pass:
            shares = planned_shares(
                equity=equity,
                entry=close,
                stop=levels["stop"],
                score=safe_float(row.get("score")),
                risk_per_plan_pct=risk_per_plan_pct,
                t1_loss_buffer_pct=safe_float(levels.get("t1_loss_buffer_pct")),
            )
            remaining = max(equity * max_new_exposure_pct - planned_gross, 0.0)
            shares = min(shares, int((remaining / close) // 100) * 100 if close > 0 else 0)
            hard_pass = shares > 0 and len(created) < max_buy_plans
            if not hard_pass:
                reasons.append("POSITION_OR_DAILY_CAP")
        status = "WATCH_TRIGGER" if hard_pass else "WATCH_ONLY"
        plan = plan_payload(row, trade_date, signal_date, rank, status, shares, levels, zone, regime, reasons)
        adapter.record_planned_order(plan)
        audits.append(plan)
        if hard_pass:
            created.append(plan)
            planned_gross += close * shares

    for rank, row in enumerate(watch_rows, start=1):
        levels = fallback_levels(row)
        zone = entry_zone(row, levels)
        plan = plan_payload(
            row,
            trade_date,
            signal_date,
            rank,
            "WATCH_ONLY",
            0,
            levels,
            zone,
            regime,
            ["SCAN_WATCH_ONLY", "STRICT_ENTRY_REQUIRED", *zone["reason_codes"]],
        )
        adapter.record_planned_order(plan)
        audits.append(plan)

    status = "PASS" if regime.get("state") != "UNKNOWN" and scan_quality.get("coverage_pass") else "FAIL_CLOSED"
    return {
        "schema_version": "chan520_local_sim_core_plan_v2",
        "policy_id": POLICY_ID,
        "selection_policy": "hard_gate_geometry_then_symbol_order_v2",
        "alpha_ranking_execution_enabled": False,
        "sizing_policy": "t1_volatility_risk_with_5pct_and_exposure_caps_v2",
        "generated_at": now(),
        "status": status,
        "trade_date": trade_date.isoformat(),
        "signal_date": signal_date.isoformat(),
        "scan_path": str(scan_path),
        "market_regime": regime,
        "scan_quality": scan_quality,
        "expired_plan_count": expired,
        "strict_scan_count": len(strict_rows),
        "watch_scan_count": len(watch_rows),
        "executable_buy_count": len(created),
        "buy_entry_ready": len(created) > 0,
        "execution_readiness": "TRADE_READY" if created else "RISK_ONLY",
        "planned_new_gross": planned_gross,
        "planned_new_exposure_pct": planned_gross / equity if equity else 0.0,
        "research_warnings": [
            "V11 conditional randomization did not validate ranked alpha selection; ranking is disabled for automatic orders.",
            "Open-source scan evidence is not a substitute for the GM V5 dynamic-universe/sector kernel; formal auto_open_close_kernel_ready remains false.",
        ],
        "plans": audits,
    }


def resolve_scan_quality(scan_path: Path, supplied: dict[str, Any] | None = None) -> dict[str, Any]:
    if supplied is not None:
        universe = safe_int(supplied.get("universe"))
        success = safe_int(supplied.get("success", supplied.get("rows")))
        coverage = success / universe if universe else 0.0
        return {
            **supplied,
            "coverage": coverage,
            "minimum_coverage": 0.85,
            "coverage_pass": coverage >= 0.85,
        }
    quality_path = scan_path.parent / f"scan_quality_{scan_path.parent.name.removeprefix('scan_')}.json"
    if quality_path.exists():
        try:
            payload = json.loads(quality_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, json.JSONDecodeError):
            pass
    markdown_path = scan_path.with_suffix(".md")
    try:
        text = markdown_path.read_text(encoding="utf-8")
    except OSError:
        return {"coverage": 0.0, "minimum_coverage": 0.85, "coverage_pass": False, "reason": "scan_quality_missing"}
    universe_match = re.search(r"合计\s*(\d+)\s*只", text)
    success_match = re.search(r"成功输出[：:]\s*(\d+)\s*只", text)
    universe = int(universe_match.group(1)) if universe_match else 0
    success = int(success_match.group(1)) if success_match else 0
    coverage = success / universe if universe else 0.0
    return {
        "universe": universe,
        "success": success,
        "failures": max(universe - success, 0),
        "coverage": coverage,
        "minimum_coverage": 0.85,
        "coverage_pass": coverage >= 0.85,
        "source": "markdown_fallback",
    }


def strict_scan_pass(row: dict[str, str]) -> bool:
    close = safe_float(row.get("close"))
    ma5 = safe_float(row.get("ma5"))
    ma20 = safe_float(row.get("ma20"))
    return (
        str(row.get("verdict") or "") == "入选"
        and safe_int(row.get("defect_count")) == 0
        and close > ma5 > ma20 > 0
    )


def watch_scan_pass(row: dict[str, str]) -> bool:
    verdict = str(row.get("verdict") or "")
    return verdict.startswith("观察") and safe_float(row.get("score")) >= 15 and safe_int(row.get("defect_count")) <= 1


def candidate_levels(row: dict[str, str], signal_date: date) -> dict[str, Any]:
    close = safe_float(row.get("close"))
    ma20 = safe_float(row.get("ma20"))
    stop = max(ma20 * 0.985, close * 0.945) if close > 0 and ma20 > 0 else 0.0
    target = 0.0
    atr14 = 0.0
    average_amplitude_pct = 0.0
    t1_loss_buffer_pct = board_t1_floor_pct(str(row.get("code") or ""))
    reason_codes: list[str] = []
    evidence_status = "COMPLETE"
    try:
        _meta, history = auto_history(str(row.get("code") or ""), signal_date, adjust=1)
        history = trim_to_date(history, signal_date)
        prior_highs = [item.high for item in history[-81:-1]]
        target = max(prior_highs) if prior_highs else 0.0
        atr14 = average_true_range(history[-15:])
        amplitudes = [max(item.high - item.low, 0.0) / item.close for item in history[-20:] if item.close > 0]
        average_amplitude_pct = sum(amplitudes) / len(amplitudes) if amplitudes else 0.0
        atr_pct = atr14 / close if close > 0 else 0.0
        t1_loss_buffer_pct = max(t1_loss_buffer_pct, atr_pct * 2.0, average_amplitude_pct * 1.25)
    except Exception as exc:  # noqa: BLE001 - missing pressure must fail closed for executable entries.
        reason_codes.append(f"PRESSURE_DATA_UNAVAILABLE:{type(exc).__name__}")
        evidence_status = "UNAVAILABLE"
    rr = (target - close) / (close - stop) if target > close > stop else 0.0
    if target <= close:
        reason_codes.append("NO_UPSIDE_PRESSURE_SPACE")
    return {
        "stop": round(stop, 2),
        "target": round(target, 2),
        "rr": round(rr, 4),
        "atr14": round(atr14, 4),
        "average_amplitude_pct": round(average_amplitude_pct, 6),
        "t1_loss_buffer_pct": round(t1_loss_buffer_pct, 6),
        "reason_codes": reason_codes,
        "level_evidence_status": evidence_status,
        "target_price_available": target > 0,
    }


def fallback_levels(row: dict[str, str]) -> dict[str, Any]:
    close = safe_float(row.get("close"))
    ma20 = safe_float(row.get("ma20"))
    stop = max(ma20 * 0.985, close * 0.945) if close > 0 and ma20 > 0 else 0.0
    return {
        "stop": round(stop, 2),
        "target": 0.0,
        "rr": 0.0,
        "atr14": 0.0,
        "average_amplitude_pct": 0.0,
        "t1_loss_buffer_pct": board_t1_floor_pct(str(row.get("code") or "")),
        "reason_codes": [],
        "level_evidence_status": "NOT_COMPUTED_WATCH_ONLY",
        "target_price_available": False,
    }


def planned_shares(
    *,
    equity: float,
    entry: float,
    stop: float,
    score: float,
    risk_per_plan_pct: float,
    t1_loss_buffer_pct: float = 0.0,
) -> int:
    if equity <= 0 or entry <= stop or stop <= 0:
        return 0
    t1_risk_per_share = entry * max(t1_loss_buffer_pct, 0.0)
    effective_risk_per_share = max(entry - stop, t1_risk_per_share)
    if effective_risk_per_share <= 0:
        return 0
    risk_cap = int((equity * risk_per_plan_pct / effective_risk_per_share) // 100) * 100
    _ = score
    value_pct = 0.05
    value_cap = int((equity * value_pct / entry) // 100) * 100
    return max(min(risk_cap, value_cap), 0)


def board_t1_floor_pct(code: str) -> float:
    normalized = "".join(ch for ch in str(code) if ch.isdigit())[:6]
    return 0.12 if normalized.startswith(("3", "688", "689")) else 0.075


def average_true_range(history: list[Any]) -> float:
    if len(history) < 2:
        return 0.0
    ranges = []
    for previous, current in zip(history, history[1:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    tail = ranges[-14:]
    return sum(tail) / len(tail) if tail else 0.0


def entry_zone(row: dict[str, str], levels: dict[str, Any]) -> dict[str, Any]:
    close = safe_float(row.get("close"))
    ma5 = safe_float(row.get("ma5"))
    lower = max(ma5 * 0.99, close * 0.98) if ma5 > 0 else close * 0.98
    upper = min(close * 1.01, ma5 * 1.02) if ma5 > 0 else close * 1.01
    trigger = ma5 or close
    invalid = close * 1.025
    stop = safe_float(levels.get("stop"))
    reason_codes: list[str] = []
    if stop <= 0 or lower <= 0 or trigger <= 0 or upper <= 0 or invalid <= 0:
        reason_codes.append("NON_POSITIVE_ENTRY_GEOMETRY")
    if stop >= lower:
        reason_codes.append("STOP_NOT_BELOW_LOWER")
    if lower > trigger:
        reason_codes.append("TRIGGER_BELOW_LOWER")
    if trigger > upper:
        reason_codes.append("TRIGGER_ABOVE_UPPER")
    if upper >= invalid:
        reason_codes.append("UPPER_NOT_BELOW_INVALID")
    valid = not reason_codes
    return {
        "lower": round(lower, 2),
        "upper": round(upper, 2),
        "trigger": round(trigger, 2),
        "invalid": round(invalid, 2),
        "geometry_valid": valid,
        "reason_codes": [] if valid else ["INVALID_PLAN_GEOMETRY", *reason_codes],
    }


def plan_payload(
    row: dict[str, str],
    trade_date: date,
    signal_date: date,
    rank: int,
    status: str,
    shares: int,
    levels: dict[str, Any],
    zone: dict[str, Any],
    regime: dict[str, Any],
    reason_codes: list[str],
) -> dict[str, Any]:
    code = str(row.get("code") or "")
    reason_text = str(row.get("satisfied") or row.get("main_signal") or "").strip()
    entry_reasons = [item.strip() for item in reason_text.split("；") if item.strip()]
    positive_codes = ["STRICT_SCAN_ENTRY", "NO_REPORTED_DEFECT", "CLOSE_ABOVE_MA5", "MA5_ABOVE_MA20"]
    if levels["rr"] >= 2.0:
        positive_codes.append("RR_GTE_2")
    payload = {
        "planned_order_id": f"CORE:{trade_date.isoformat()}:{code}",
        "pending_order_id": f"CORE:{trade_date.isoformat()}:{code}",
        "order_intent_id": f"CORE-{trade_date.isoformat()}-{rank:02d}",
        "run_id": f"core-plan-{trade_date.isoformat()}",
        "trade_date": trade_date.isoformat(),
        "signal_date": signal_date.isoformat(),
        "symbol": code,
        "stock_name": str(row.get("name") or ""),
        "side": "BUY",
        "volume": shares,
        "status": status,
        "trigger_price": zone["trigger"],
        "lower_price": zone["lower"],
        "upper_price": zone["upper"],
        "invalid_price": zone["invalid"],
        "stop_price": levels["stop"],
        "target_price": levels["target"],
        "risk_reward": levels["rr"],
        "geometry_valid": zone["geometry_valid"],
        "atr14": levels.get("atr14", 0.0),
        "average_amplitude_pct": levels.get("average_amplitude_pct", 0.0),
        "level_evidence_status": levels.get("level_evidence_status", "UNKNOWN"),
        "target_price_available": bool(levels.get("target_price_available")),
        "t1_loss_buffer_pct": levels.get("t1_loss_buffer_pct", board_t1_floor_pct(code)),
        "t1_risk_budget_amount": safe_float(row.get("close")) * shares * safe_float(levels.get("t1_loss_buffer_pct")),
        "ma5": safe_float(row.get("ma5")),
        "ma10": safe_float(row.get("ma10")),
        "ma20": safe_float(row.get("ma20")),
        "ma60": safe_float(row.get("ma60")),
        "score": safe_float(row.get("score")),
        "volume_ratio": safe_float(row.get("volume_ratio")),
        "rsi14": safe_float(row.get("rsi14")),
        "market_regime": regime.get("state"),
        "local_sim_execution_policy_id": POLICY_ID,
        "reason_code": "STRICT_SCAN_ENTRY" if status == "WATCH_TRIGGER" else "SCAN_WATCH_ONLY",
        "reason_codes": positive_codes if status == "WATCH_TRIGGER" else reason_codes,
        "blocking_reason_codes": reason_codes,
        "reasons": entry_reasons,
        "reason_text": reason_text,
        "entry_reason": reason_text,
        "exit_risk_reason": f"跌破 MA20/止损 {levels['stop']:.2f} 或确认超时取消",
        "candidate_evidence_source": "open_source_full_market_scan",
        "alpha_ranking_execution_enabled": False,
        "sizing_policy": "equal_base_5pct_with_risk_and_exposure_caps_v1",
    }
    return payload


def expire_old_buy_plans(ledger: Path, account_id: str, trade_date: str) -> int:
    if not ledger.exists():
        return 0
    placeholders = ",".join("?" for _ in ACTIVE_BUY_STATUSES)
    with sqlite3.connect(ledger) as conn:
        cur = conn.execute(
            f"""
            update planned_orders
            set status = 'EXPIRED_PLAN_DATE',
                last_message = 'expired by daily core plan generation',
                updated_at = ?
            where account_id = ? and upper(side) = 'BUY' and trade_date < ?
              and status in ({placeholders})
            """,
            (datetime.now(TZ).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"), account_id, trade_date, *ACTIVE_BUY_STATUSES),
        )
        return int(cur.rowcount)


def summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "trade_date": payload.get("trade_date"),
        "signal_date": payload.get("signal_date"),
        "market_regime": (payload.get("market_regime") or {}).get("state"),
        "strict_scan_count": payload.get("strict_scan_count"),
        "watch_scan_count": payload.get("watch_scan_count"),
        "executable_buy_count": payload.get("executable_buy_count"),
        "buy_entry_ready": payload.get("buy_entry_ready"),
        "execution_readiness": payload.get("execution_readiness"),
        "expired_plan_count": payload.get("expired_plan_count"),
    }


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value)) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
