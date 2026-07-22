from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.broker_adapter import LocalSimBrokerAdapter, LocalSimBrokerConfig  # noqa: E402
from chan520_skill.execution_policy import (  # noqa: E402
    BEAR_PILOT_ACCOUNT_ID,
    BEAR_PILOT_EXECUTION_SCOPE,
    BEAR_PILOT_MAX_EXPOSURE_PCT,
    BEAR_PILOT_MAX_FILLS,
    BEAR_PILOT_MIN_RR,
    BEAR_PILOT_POLICY_ID,
    BEAR_PILOT_POSITION_PCT,
    CORE_PLAN_POLICY_ID,
)
from chan520_skill.data import eastmoney_history, sina_history, tencent_history, trim_to_date  # noqa: E402
from chan520_skill.market_store import (  # noqa: E402
    DEFAULT_PATH as MARKET_STORE,
    initialize as initialize_market_store,
    latest_bar_date_before,
    load_history as load_stored_history,
    load_scan as load_stored_scan,
    load_sectors,
    upsert_history,
    upsert_scan,
    upsert_sectors,
)
from chan520_skill.regime import evaluate_regime, index_history, tencent_index_history  # noqa: E402
from chan520_skill.scanner import scan_market  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")
POLICY_ID = CORE_PLAN_POLICY_ID
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
    parser.add_argument("--pilot-account-id", default=BEAR_PILOT_ACCOUNT_ID)
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--max-buy-plans", type=int, default=2)
    parser.add_argument("--max-new-exposure-pct", type=float, default=0.15)
    parser.add_argument("--risk-per-plan-pct", type=float, default=0.005)
    parser.add_argument("--disable-bear-pilot", action="store_true")
    parser.add_argument("--refresh-scan-if-missing", action="store_true")
    parser.add_argument("--offline-regime", action="store_true", help="Fail closed with UNKNOWN regime instead of fetching index data")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    trade_date = date.fromisoformat(args.trade_date)
    initialize_market_store(MARKET_STORE)
    signal_date = resolve_signal_date(trade_date, args.signal_date)
    verify_trade_date(trade_date, signal_date)
    scan_path = ROOT / "reports" / f"scan_{signal_date.isoformat()}" / f"market_scan_{signal_date.isoformat()}.csv"
    scan_stats = None
    restored_scan = None if scan_path.exists() else load_stored_scan(signal_date, path=MARKET_STORE)
    if restored_scan is not None:
        restore_scan_files(scan_path, signal_date, *restored_scan)
    if not scan_path.exists() and args.refresh_scan_if_missing:
        _csv_path, _md_path, scan_stats = scan_market(signal_date, scan_path.parent, max_workers=16)
    if not scan_path.exists():
        raise SystemExit(f"FAIL_CLOSED: prior-session scan missing: {scan_path}")

    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(account_id=args.account_id, initial_cash=args.initial_cash, ledger_path=args.ledger)
    )
    adapter.initialize_account()
    pilot_adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id=args.pilot_account_id,
            initial_cash=args.initial_cash,
            ledger_path=args.ledger,
        )
    )
    pilot_adapter.initialize_account()
    rows = read_scan(scan_path)
    scan_quality = resolve_scan_quality(scan_path, scan_stats)
    stored_scan = load_stored_scan(signal_date, path=MARKET_STORE)
    rows, scan_quality, scan_path = prefer_qualified_scan_evidence(
        rows,
        scan_quality,
        scan_path,
        stored_scan,
    )
    upsert_scan(signal_date, rows, scan_quality, source="qualified_scan_csv", path=MARKET_STORE)
    sector_map = resolve_sector_map(rows)
    regime = resolve_market_regime(signal_date, offline=args.offline_regime)
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
        sector_map=sector_map,
        pilot_adapter=None if args.disable_bear_pilot else pilot_adapter,
        pilot_account_id=args.pilot_account_id,
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
    errors = []
    completed_session_cutoff = trade_date - timedelta(days=1)
    for loader in (tencent_index_history, index_history):
        try:
            rows = loader("000001", completed_session_cutoff)
            prior = [row.date for row in rows if row.date < trade_date]
            if prior:
                return max(prior)
        except Exception as exc:  # noqa: BLE001 - exact-date local evidence is the final fallback.
            errors.append(f"{type(exc).__name__}:{exc}")
    cached = latest_bar_date_before("000001", trade_date, is_index=True, path=MARKET_STORE)
    if cached is not None:
        return cached
    raise RuntimeError(f"FAIL_CLOSED: no prior trading session before {trade_date}: {' | '.join(errors)}")


def verify_trade_date(trade_date: date, signal_date: date) -> None:
    # A complete prior-session scan is local evidence that the signal date was
    # a market session. Combined with a weekday trade date, it lets the
    # scheduled plan run without depending on AkShare's unbounded calendar
    # request during the pre-open window.
    local_scan = ROOT / "reports" / f"scan_{signal_date.isoformat()}" / f"market_scan_{signal_date.isoformat()}.csv"
    quality_path = local_scan.with_name(f"scan_quality_{signal_date.isoformat()}.json")
    if trade_date.weekday() < 5 and local_scan.exists() and quality_path.exists():
        return
    try:
        import akshare as ak

        frame = ak.tool_trade_date_hist_sina()
        sessions = {item.date() if hasattr(item, "date") else date.fromisoformat(str(item)[:10]) for item in frame["trade_date"]}
    except Exception as exc:  # noqa: BLE001 - unavailable calendar must fail closed.
        raise RuntimeError(f"FAIL_CLOSED: trading calendar unavailable and no complete local prior-session scan: {type(exc).__name__}: {exc}") from exc
    if trade_date not in sessions:
        raise RuntimeError(f"FAIL_CLOSED: {trade_date} is not an A-share trading session")


def resolve_market_regime(signal_date: date, *, offline: bool = False) -> dict[str, Any]:
    if offline:
        cached = load_stored_history("000001", signal_date, is_index=True, path=MARKET_STORE)
        if cached is None:
            return {"state": "UNKNOWN", "regime_ok": False, "source": "unavailable", "detail": "offline_regime: exact-date local index data unavailable; buy entries fail closed"}
        _meta, rows, source = cached
        state = evaluate_regime("000001", rows, signal_date)
        return mapped_regime(state, source)
    errors = []
    for source, loader in (("tencent", tencent_index_history), ("eastmoney_or_tencent_fallback", index_history)):
        try:
            rows = loader("000001", signal_date)
            state = evaluate_regime("000001", rows, signal_date)
            upsert_history("000001", "上证指数", 1, rows, source=source, is_index=True, path=MARKET_STORE)
            return mapped_regime(state, source)
        except Exception as exc:  # noqa: BLE001 - try the next audited source.
            errors.append(f"{source}:{type(exc).__name__}:{exc}")
    cached = load_stored_history("000001", signal_date, is_index=True, path=MARKET_STORE)
    if cached is not None:
        _meta, rows, source = cached
        return mapped_regime(evaluate_regime("000001", rows, signal_date), source)
    return {"state": "UNKNOWN", "regime_ok": False, "source": "unavailable", "detail": " | ".join(errors)}


def mapped_regime(state: Any, source: str) -> dict[str, Any]:
    mapped = {"trend_up": "BULL", "range": "NORMAL", "down": "BEAR"}.get(state.regime, "UNKNOWN")
    return {"state": mapped, "regime_ok": mapped in {"BULL", "NORMAL"}, "source": source, "detail": state.detail}


def read_scan(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def restore_scan_files(
    scan_path: Path,
    target: date,
    rows: list[dict[str, Any]],
    quality: dict[str, Any],
) -> None:
    if not rows:
        return
    scan_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with scan_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    quality_path = scan_path.with_name(f"scan_quality_{target.isoformat()}.json")
    quality_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def prefer_qualified_scan_evidence(
    rows: list[dict[str, Any]],
    quality: dict[str, Any],
    scan_path: Path,
    stored_scan: tuple[list[dict[str, Any]], dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], Path]:
    if quality.get("coverage_pass") is True or stored_scan is None:
        return rows, quality, scan_path
    stored_rows, stored_quality = stored_scan
    if stored_rows and stored_quality.get("coverage_pass") is True:
        return stored_rows, stored_quality, MARKET_STORE
    return rows, quality, scan_path


def load_candidate_history(code: str, target: date, *, offline: bool) -> tuple[Any, list[Any], str] | None:
    if not offline:
        errors = []
        for source, loader, kwargs in (
            ("tencent", tencent_history, {"adjust": "qfq"}),
            ("sina_unadjusted", sina_history, {}),
            ("eastmoney", eastmoney_history, {"adjust": 1}),
        ):
            try:
                meta, rows = loader(code, target, **kwargs)
                rows = trim_to_date(rows, target)
                if not rows or rows[-1].date != target:
                    raise RuntimeError(f"{source} exact-date history unavailable for {target}")
                upsert_history(code, meta.name, meta.market, rows, source=source, path=MARKET_STORE)
                return meta, rows, source
            except Exception as exc:  # noqa: BLE001 - try every bounded source before local fallback.
                errors.append(exc)
    return load_stored_history(code, target, path=MARKET_STORE)


def resolve_sector_map(scan_rows: list[dict[str, Any]]) -> dict[str, str]:
    mapping = load_sectors(path=MARKET_STORE)
    embedded = {
        str(row.get("code") or ""): str(row.get("industry") or "")
        for row in scan_rows
        if row.get("code") and row.get("industry")
    }
    mapping.update(embedded)
    sources = sorted(ROOT.glob("reports/backtest/**/candidate_symbols*.csv"), reverse=True)
    for path in sources:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    code = str(row.get("code") or "")
                    sector = str(row.get("industry") or row.get("sector") or "")
                    if code and sector and code not in mapping:
                        mapping[code] = sector
        except (OSError, csv.Error):
            continue
        if len(mapping) >= 1000:
            break
    if mapping:
        upsert_sectors(mapping, source="local_research_sector_snapshot", path=MARKET_STORE)
    return mapping


def build_style_diagnostic(
    signal_date: date,
    scan_rows: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    sector_map: dict[str, str],
) -> dict[str, Any]:
    usable = [row for row in scan_rows if safe_float(row.get("close")) > 0]
    plan_codes = {str(row.get("symbol") or "") for row in plans}
    candidates = [row for row in usable if str(row.get("code") or "") in plan_codes]

    def breadth(rows: list[dict[str, Any]]) -> dict[str, Any]:
        count = len(rows)
        up = sum(safe_float(row.get("pct_chg")) > 0 for row in rows)
        above20 = sum(safe_float(row.get("close")) > safe_float(row.get("ma20")) > 0 for row in rows)
        return {
            "count": count,
            "up_ratio": round(up / count, 4) if count else 0.0,
            "average_pct_chg": round(sum(safe_float(row.get("pct_chg")) for row in rows) / count, 4) if count else 0.0,
            "above_ma20_ratio": round(above20 / count, 4) if count else 0.0,
        }

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in usable:
        sector = sector_map.get(str(row.get("code") or ""), str(row.get("industry") or "UNMAPPED"))
        grouped.setdefault(sector or "UNMAPPED", []).append(row)
    sectors = [
        {"sector": sector, **breadth(rows)}
        for sector, rows in grouped.items()
        if sector != "UNMAPPED" and len(rows) >= 10
    ]
    sectors.sort(key=lambda item: (item["average_pct_chg"], item["up_ratio"], item["count"]), reverse=True)
    top_sectors = sectors[:8]
    top_names = {str(item["sector"]) for item in top_sectors[:3]}
    candidate_sectors = [sector_map.get(str(row.get("code") or ""), "UNMAPPED") for row in candidates]
    overlap = sum(sector in top_names for sector in candidate_sectors)
    market = breadth(usable)
    candidate = breadth(candidates)
    overlap_ratio = overlap / len(candidates) if candidates else 0.0
    mismatch = bool(candidates and market["up_ratio"] >= 0.60 and (overlap_ratio < 0.20 or candidate["up_ratio"] + 0.15 < market["up_ratio"]))
    boards = {
        "main": sum(not str(row.get("code") or "").startswith(("3", "688", "689")) for row in candidates),
        "chinext": sum(str(row.get("code") or "").startswith("3") for row in candidates),
        "star": sum(str(row.get("code") or "").startswith(("688", "689")) for row in candidates),
    }
    return {
        "status": "PASS" if usable else "UNAVAILABLE",
        "signal_date": signal_date.isoformat(),
        "role": "completed_session_diagnostic_only",
        "used_for_execution_gate": False,
        "market_breadth": market,
        "candidate_breadth": candidate,
        "top_industries": top_sectors,
        "candidate_industries": dict(sorted(Counter(candidate_sectors).items())),
        "candidate_board_distribution": boards,
        "top3_industry_overlap_ratio": round(overlap_ratio, 4),
        "mismatch_alert": mismatch,
        "diagnosis": "候选与强势行业/市场宽度存在偏离，仅作观察池诊断，不放宽入场门槛" if mismatch else "候选风格未见显著宽度偏离",
    }


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
    sector_map: dict[str, str] | None = None,
    pilot_adapter: LocalSimBrokerAdapter | None = None,
    pilot_account_id: str = BEAR_PILOT_ACCOUNT_ID,
    max_candidates: int,
    max_buy_plans: int,
    max_new_exposure_pct: float,
    risk_per_plan_pct: float,
) -> dict[str, Any]:
    expired = expire_old_buy_plans(ledger, account_id, trade_date.isoformat())
    pilot_expired = expire_old_buy_plans(ledger, pilot_account_id, trade_date.isoformat()) if pilot_adapter else 0
    account = adapter.account_snapshot()
    equity = float(account["cash"]) + sum(float(row["shares"]) * float(row["average_price"]) for row in account["positions"])
    # Alpha-score ranking remains disabled. Risk evidence only decides which
    # already-qualified plan gets scarce execution capacity first.
    sector_map = sector_map or {}
    ordered = sorted(
        [{**row, "industry": sector_map.get(str(row.get("code") or ""), str(row.get("industry") or "UNMAPPED"))} for row in scan_rows],
        key=lambda row: str(row.get("code") or ""),
    )
    strict_candidates = [
        (row, candidate_levels(row, signal_date, offline=str(regime.get("state") or "").upper() == "UNKNOWN"))
        for row in ordered
        if strict_scan_pass(row)
    ]
    strict_candidates.sort(key=lambda item: execution_risk_priority_key(item[0], item[1]))
    strict_candidates = strict_candidates[:max_candidates]
    strict_rows = [row for row, _levels in strict_candidates]
    watch_rows = [row for row in ordered if watch_scan_pass(row) and row not in strict_rows]
    created: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    planned_gross = 0.0
    pilot_plans: list[dict[str, Any]] = []
    pilot_planned_gross = 0.0
    pilot_equity = account_equity(pilot_adapter) if pilot_adapter else 0.0
    scan_quality = scan_quality or {"coverage_pass": True, "coverage": 1.0, "minimum_coverage": 0.85}
    strict_enabled = bool(regime.get("regime_ok")) and bool(scan_quality.get("coverage_pass"))

    for rank, (row, levels) in enumerate(strict_candidates, start=1):
        zone = entry_zone(row, levels)
        code = str(row.get("code") or "")
        close = safe_float(row.get("close"))
        reasons = [*levels["reason_codes"], *zone["reason_codes"]]
        adjusted_history = execution_history_adjusted(row)
        hard_pass = (
            strict_enabled
            and adjusted_history
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
        if not adjusted_history:
            reasons.append("UNADJUSTED_HISTORY_BLOCKED")
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

    watch_candidates = []
    for row in watch_rows:
        levels = fallback_levels(row)
        zone = entry_zone(row, levels)
        watch_candidates.append((row, levels, zone))
    watch_candidates.sort(key=lambda item: research_priority_key(item[0], item[1], item[2]))
    watch_candidates = watch_candidates[:max_candidates]
    bear_defensive_symbols: list[str] = []
    for rank, (row, levels, zone) in enumerate(watch_candidates, start=1):
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
        if bear_defensive_shape_eligible(row, zone):
            plan["research_conditional_cohorts"] = ["BEAR_DEFENSIVE_WATCH"]
            plan["research_policy_id"] = "bear_defensive_watch_v2"
            plan["execution_priority_basis"] = "geometry_then_t1_then_score_tiebreak_v1"
        if bear_defensive_eligible(row, zone, regime):
            plan["research_only"] = True
            plan["research_cohort"] = "BEAR_DEFENSIVE_WATCH"
            plan["research_policy_id"] = "bear_defensive_watch_v2"
            plan["reason_codes"] = [*plan["reason_codes"], "BEAR_DEFENSIVE_RESEARCH_ONLY"]
            bear_defensive_symbols.append(str(row.get("code") or ""))
            if pilot_adapter is not None and len(pilot_plans) < BEAR_PILOT_MAX_FILLS:
                pilot_levels = candidate_levels(row, signal_date, offline=False)
                pilot_zone = entry_zone(row, pilot_levels)
                close = safe_float(row.get("close"))
                pilot_eligible = (
                    scan_quality.get("coverage_pass") is True
                    and execution_history_adjusted(row)
                    and pilot_zone.get("geometry_valid") is True
                    and safe_float(pilot_levels.get("rr")) >= BEAR_PILOT_MIN_RR
                    and safe_float(pilot_levels.get("target")) > close > 0
                )
                if pilot_eligible:
                    shares = planned_shares(
                        equity=pilot_equity,
                        entry=close,
                        stop=safe_float(pilot_levels.get("stop")),
                        score=safe_float(row.get("score")),
                        risk_per_plan_pct=BEAR_PILOT_POSITION_PCT,
                        t1_loss_buffer_pct=safe_float(pilot_levels.get("t1_loss_buffer_pct")),
                        value_pct=BEAR_PILOT_POSITION_PCT,
                    )
                    remaining = max(pilot_equity * BEAR_PILOT_MAX_EXPOSURE_PCT - pilot_planned_gross, 0.0)
                    shares = min(shares, int((remaining / close) // 100) * 100 if close > 0 else 0)
                    if shares > 0:
                        pilot = bear_pilot_plan(
                            row,
                            trade_date,
                            signal_date,
                            len(pilot_plans) + 1,
                            shares,
                            pilot_levels,
                            pilot_zone,
                            regime,
                            pilot_account_id,
                        )
                        pilot_adapter.record_planned_order(pilot)
                        pilot_plans.append(pilot)
                        pilot_planned_gross += close * shares
        adapter.record_planned_order(plan)
        audits.append(plan)

    status = "PASS" if regime.get("state") != "UNKNOWN" and scan_quality.get("coverage_pass") else "FAIL_CLOSED"
    style_diagnostic = build_style_diagnostic(signal_date, ordered, audits, sector_map)
    return {
        "schema_version": "chan520_local_sim_core_plan_v2",
        "policy_id": POLICY_ID,
        "selection_policy": "hard_gate_then_t1_risk_priority_v3",
        "alpha_ranking_execution_enabled": False,
        "sizing_policy": "t1_volatility_risk_with_5pct_and_exposure_caps_v2",
        "generated_at": now(),
        "status": status,
        "trade_date": trade_date.isoformat(),
        "signal_date": signal_date.isoformat(),
        "scan_path": str(scan_path),
        "market_regime": regime,
        "supplemental_market_context": load_supplemental_market_context(signal_date),
        "candidate_style_diagnostic": style_diagnostic,
        "scan_quality": scan_quality,
        "expired_plan_count": expired,
        "pilot_expired_plan_count": pilot_expired,
        "strict_scan_count": len(strict_rows),
        "watch_scan_count": len(watch_rows),
        "executable_buy_count": len(created),
        "buy_entry_ready": len(created) > 0,
        "execution_readiness": "TRADE_READY" if created else "RISK_ONLY",
        "planned_new_gross": planned_gross,
        "planned_new_exposure_pct": planned_gross / equity if equity else 0.0,
        "account_equity": equity,
        "execution_funnel": {
            "scanned_count": int(scan_quality.get("success") or scan_quality.get("successful_count") or scan_quality.get("usable_count") or len(ordered)),
            "universe_count": int(scan_quality.get("universe") or scan_quality.get("universe_count") or scan_quality.get("expected_count") or len(ordered)),
            "strict_count": len(strict_rows),
            "watch_count": len(watch_rows),
            "shortlisted_count": len(audits),
            "geometry_valid_count": sum(row.get("geometry_valid") is True for row in audits),
            "core_executable_count": len(created),
            "bear_defensive_count": len(bear_defensive_symbols),
            "bear_pilot_count": len(pilot_plans),
        },
        "research_cohorts": {
            "bear_defensive": {
                "policy_id": "bear_defensive_watch_v2",
                "status": "RESEARCH_ONLY",
                "eligible_count": len(bear_defensive_symbols),
                "symbols": bear_defensive_symbols,
                "constraints": [
                    "market_regime=BEAR",
                    "WATCH_ONLY",
                    "main_board",
                    "geometry_valid",
                    "score>=18",
                    "ma5>ma20",
                    "rsi14<=70",
                ],
                "live_execution_enabled": False,
            },
            "conditional_bear_defensive": {
                "policy_id": "bear_defensive_watch_v2",
                "status": "CONDITIONAL_RESEARCH_ONLY",
                "eligible_count": sum(
                    "BEAR_DEFENSIVE_WATCH" in list(row.get("research_conditional_cohorts") or []) for row in audits
                ),
                "activation": "reconstruct signal-date regime and require BEAR",
                "live_execution_enabled": False,
            },
            "bear_pilot": {
                "policy_id": BEAR_PILOT_POLICY_ID,
                "status": "ARMED" if pilot_plans else "NO_ELIGIBLE_PLAN",
                "account_id": pilot_account_id,
                "eligible_count": len(pilot_plans),
                "symbols": [str(row.get("symbol") or "") for row in pilot_plans],
                "max_positions": BEAR_PILOT_MAX_FILLS,
                "position_cap_pct": BEAR_PILOT_POSITION_PCT,
                "account_exposure_cap_pct": BEAR_PILOT_MAX_EXPOSURE_PCT,
                "minimum_risk_reward": BEAR_PILOT_MIN_RR,
                "planned_gross": round(pilot_planned_gross, 2),
                "planned_exposure_pct": pilot_planned_gross / pilot_equity if pilot_equity else 0.0,
                "execution_scope": BEAR_PILOT_EXECUTION_SCOPE,
                "core_account_affected": False,
                "gm_submit_enabled": False,
            },
        },
        "research_warnings": [
            "V11 conditional randomization did not validate ranked alpha selection; ranking is disabled for automatic orders.",
            "Open-source scan evidence is not a substitute for the GM V5 dynamic-universe/sector kernel; formal auto_open_close_kernel_ready remains false.",
        ],
        "plans": audits,
    }


def load_supplemental_market_context(signal_date: date) -> dict[str, Any]:
    path = ROOT / "reports" / "market_context" / signal_date.strftime("%Y%m%d") / "ths_data_center.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "UNAVAILABLE",
            "source": "tonghuashun_public_data_center",
            "trade_date": signal_date.isoformat(),
            "used_for_execution_gate": False,
        }
    audit = payload.get("audit") if isinstance(payload.get("audit"), dict) else {}
    display = payload.get("display") if isinstance(payload.get("display"), dict) else {}
    limit_up = payload.get("limit_up") if isinstance(payload.get("limit_up"), dict) else {}
    return {
        "status": "PASS" if audit.get("passed") is True else "DEGRADED",
        "source": "tonghuashun_public_data_center",
        "source_path": str(path),
        "trade_date": str(payload.get("trade_date") or signal_date.isoformat()),
        "generated_at": payload.get("generated_at"),
        "endpoint_count": safe_int(audit.get("endpoint_count")),
        "failed_count": safe_int(audit.get("failed_count")),
        "emotion_status": limit_up.get("emotion_status"),
        "emotion_label": limit_up.get("emotion_label"),
        "summary_line": display.get("summary_line"),
        "money_line": display.get("money_line"),
        "hot_topics": list(display.get("hot_topics") or [])[:16],
        "exposure_hint": display.get("exposure_hint"),
        "used_for_execution_gate": False,
        "role": "supplemental_validation_only",
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
    scan_date = scan_path.stem.removeprefix("market_scan_")
    quality_path = scan_path.parent / f"scan_quality_{scan_date}.json"
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


def execution_history_adjusted(row: dict[str, Any]) -> bool:
    return str(row.get("history_source") or "legacy_adjusted_unknown") != "sina_unadjusted"


def watch_scan_pass(row: dict[str, str]) -> bool:
    verdict = str(row.get("verdict") or "")
    return verdict.startswith("观察") and safe_float(row.get("score")) >= 15 and safe_int(row.get("defect_count")) <= 1


def execution_risk_priority_key(row: dict[str, str], levels: dict[str, Any]) -> tuple[float, float, float, str]:
    amplitude = safe_float(levels.get("average_amplitude_pct"), 999.0)
    if amplitude <= 0:
        amplitude = 999.0
    return (
        safe_float(levels.get("t1_loss_buffer_pct"), 999.0),
        amplitude,
        -safe_float(levels.get("rr")),
        str(row.get("code") or ""),
    )


def research_priority_key(
    row: dict[str, str], levels: dict[str, Any], zone: dict[str, Any]
) -> tuple[int, float, float, str]:
    return (
        0 if zone.get("geometry_valid") else 1,
        safe_float(levels.get("t1_loss_buffer_pct"), 999.0),
        -safe_float(row.get("score")),
        str(row.get("code") or ""),
    )


def bear_defensive_eligible(row: dict[str, str], zone: dict[str, Any], regime: dict[str, Any]) -> bool:
    return str(regime.get("state") or "").upper() == "BEAR" and bear_defensive_shape_eligible(row, zone)


def bear_defensive_shape_eligible(row: dict[str, str], zone: dict[str, Any]) -> bool:
    code = str(row.get("code") or "")
    return (
        bool(zone.get("geometry_valid"))
        and not code.startswith(("3", "688", "689"))
        and safe_float(row.get("score")) >= 18
        and safe_float(row.get("ma5")) > safe_float(row.get("ma20")) > 0
        and 0 < safe_float(row.get("rsi14")) <= 70
    )


def candidate_levels(row: dict[str, str], signal_date: date, *, offline: bool = False) -> dict[str, Any]:
    close = safe_float(row.get("close"))
    ma20 = safe_float(row.get("ma20"))
    stop = max(ma20 * 0.985, close * 0.945) if close > 0 and ma20 > 0 else 0.0
    target = 0.0
    atr14 = 0.0
    average_amplitude_pct = 0.0
    t1_loss_buffer_pct = board_t1_floor_pct(str(row.get("code") or ""))
    reason_codes: list[str] = []
    evidence_status = "COMPLETE"
    evidence_source = "unavailable"
    try:
        code = str(row.get("code") or "")
        loaded = load_candidate_history(code, signal_date, offline=offline)
        if loaded is None:
            raise RuntimeError("exact-date history unavailable")
        _meta, history, evidence_source = loaded
        try:
            history = trim_to_date(history, signal_date)
            prior_highs = [item.high for item in history[-81:-1]]
            target = max(prior_highs) if prior_highs else 0.0
            atr14 = average_true_range(history[-15:])
            amplitudes = [max(item.high - item.low, 0.0) / item.close for item in history[-20:] if item.close > 0]
            average_amplitude_pct = sum(amplitudes) / len(amplitudes) if amplitudes else 0.0
            atr_pct = atr14 / close if close > 0 else 0.0
            t1_loss_buffer_pct = max(t1_loss_buffer_pct, atr_pct * 2.0, average_amplitude_pct * 1.25)
        except Exception:
            raise
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
        "level_evidence_source": evidence_source,
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
        "level_evidence_source": "scan_snapshot",
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
    value_pct: float = 0.05,
) -> int:
    if equity <= 0 or entry <= stop or stop <= 0:
        return 0
    t1_risk_per_share = entry * max(t1_loss_buffer_pct, 0.0)
    effective_risk_per_share = max(entry - stop, t1_risk_per_share)
    if effective_risk_per_share <= 0:
        return 0
    risk_cap = int((equity * risk_per_plan_pct / effective_risk_per_share) // 100) * 100
    _ = score
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
        "execution_priority": rank,
        "execution_priority_basis": "t1_buffer_then_volatility_then_rr_v1",
        "run_id": f"core-plan-{trade_date.isoformat()}",
        "trade_date": trade_date.isoformat(),
        "signal_date": signal_date.isoformat(),
        "symbol": code,
        "stock_name": str(row.get("name") or ""),
        "signal_history_source": str(row.get("history_source") or "legacy_adjusted_unknown"),
        "industry": str(row.get("industry") or "UNMAPPED"),
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
        "level_evidence_source": levels.get("level_evidence_source", "unknown"),
        "target_price_available": bool(levels.get("target_price_available")),
        "t1_loss_buffer_pct": levels.get("t1_loss_buffer_pct", board_t1_floor_pct(code)),
        "t1_risk_budget_amount": safe_float(row.get("close")) * shares * safe_float(levels.get("t1_loss_buffer_pct")),
        "signal_close": safe_float(row.get("close")),
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


def bear_pilot_plan(
    row: dict[str, str],
    trade_date: date,
    signal_date: date,
    rank: int,
    shares: int,
    levels: dict[str, Any],
    zone: dict[str, Any],
    regime: dict[str, Any],
    account_id: str,
) -> dict[str, Any]:
    payload = plan_payload(
        row,
        trade_date,
        signal_date,
        rank,
        "WATCH_TRIGGER",
        shares,
        levels,
        zone,
        regime,
        ["BEAR_PILOT_RESEARCH_ONLY"],
    )
    code = str(row.get("code") or "")
    payload.update(
        {
            "planned_order_id": f"BEAR-PILOT:{trade_date.isoformat()}:{code}",
            "pending_order_id": f"BEAR-PILOT:{trade_date.isoformat()}:{code}",
            "order_intent_id": f"BEAR-PILOT-{trade_date.isoformat()}-{rank:02d}",
            "run_id": f"bear-pilot-{trade_date.isoformat()}",
            "account_id": account_id,
            "local_sim_execution_policy_id": BEAR_PILOT_POLICY_ID,
            "execution_scope": BEAR_PILOT_EXECUTION_SCOPE,
            "research_only": True,
            "research_pilot": True,
            "research_cohort": "BEAR_DEFENSIVE_PILOT",
            "core_account_affected": False,
            "gm_submit_enabled": False,
            "reason_code": "BEAR_PILOT_RISK_QUALIFIED",
            "reason_codes": [
                "BEAR_DEFENSIVE_SHAPE",
                "RR_GTE_2",
                "VALID_ENTRY_GEOMETRY",
                "LOCAL_SIM_RESEARCH_ONLY",
            ],
            "blocking_reason_codes": [],
            "sizing_policy": "bear_pilot_2_5pct_with_t1_risk_cap_v1",
        }
    )
    return payload


def account_equity(adapter: LocalSimBrokerAdapter | None) -> float:
    if adapter is None:
        return 0.0
    account = adapter.account_snapshot()
    return float(account["cash"]) + sum(
        float(row["shares"]) * float(row["average_price"]) for row in account.get("positions", [])
    )


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
