from __future__ import annotations

import argparse
import copy
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.execute_local_sim_triggers import CORE_PLAN_POLICY_ID, evaluate_plan, plan_rank


TZ = ZoneInfo("Asia/Shanghai")
INDEX_SYMBOLS = ("000001", "399001", "399006", "000688")
POLICY_ID = "watch_only_counterfactual_v1"
REPLAY_MINUTES = (
    "0930", "0932", "0934", "0936", "0938", "0940", "0942", "0944", "0951",
    "1031", "1115", "1311", "1401", "1431", "1451",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay research-only WATCH plans against the real intraday minute path")
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--plan", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--initial-equity", type=float, default=1_000_000.0)
    parser.add_argument("--max-fills", type=int, default=2)
    parser.add_argument("--max-exposure-pct", type=float, default=0.15)
    args = parser.parse_args()

    trade_date = date.fromisoformat(args.trade_date)
    plan_path = Path(args.plan) if args.plan else ROOT / "reports" / "local_sim_plan" / trade_date.strftime("%Y%m%d") / "core_plan.json"
    output = Path(args.output) if args.output else ROOT / "reports" / "local_sim_counterfactual" / trade_date.strftime("%Y%m%d") / "watch_only_replay.json"
    core_plan = read_json(plan_path, {})
    candidates = research_candidates(core_plan)
    if not candidates:
        payload = run_replay(
            core_plan,
            trade_date,
            {},
            initial_equity=args.initial_equity,
            max_fills=args.max_fills,
            max_exposure_pct=args.max_exposure_pct,
        )
        payload["plan_path"] = str(plan_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(summary(payload), ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    try:
        market_data = {
            str(row.get("symbol") or ""): fetch_tencent_day(str(row.get("symbol") or ""), trade_date, is_index=False)
            for row in candidates
        }
        market_data.update(
            {index_data_key(symbol): fetch_tencent_day(symbol, trade_date, is_index=True) for symbol in INDEX_SYMBOLS}
        )
        payload = run_replay(
            core_plan,
            trade_date,
            market_data,
            initial_equity=args.initial_equity,
            max_fills=args.max_fills,
            max_exposure_pct=args.max_exposure_pct,
        )
    except (KeyError, ValueError, TimeoutError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        payload = failure_payload(core_plan, trade_date, candidates, f"{type(exc).__name__}: {exc}")
    payload["plan_path"] = str(plan_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary(payload), ensure_ascii=False, sort_keys=True), flush=True)
    # Research data failure is visible in the report/dashboard, but must not
    # prevent the actual account review from completing.
    return 0


def research_candidates(core_plan: dict[str, Any]) -> list[dict[str, Any]]:
    plans = core_plan.get("plans") if isinstance(core_plan.get("plans"), list) else []
    return sorted(
        [
            row
            for row in plans
            if isinstance(row, dict)
            and row.get("research_only") is True
            and row.get("research_cohort") == "BEAR_DEFENSIVE_WATCH"
            and str(row.get("status") or "").upper() == "WATCH_ONLY"
        ],
        key=plan_rank,
    )


def run_replay(
    core_plan: dict[str, Any],
    trade_date: date,
    market_data: dict[str, dict[str, Any]],
    *,
    initial_equity: float,
    max_fills: int,
    max_exposure_pct: float,
) -> dict[str, Any]:
    candidates = research_candidates(core_plan)
    if not candidates:
        return base_payload(core_plan, trade_date, candidates, status="NO_CANDIDATES")
    required = {
        *(str(row.get("symbol") or "") for row in candidates),
        *(index_data_key(symbol) for symbol in INDEX_SYMBOLS),
    }
    missing = sorted(symbol for symbol in required if symbol not in market_data or not market_data[symbol].get("minutes"))
    if missing:
        return failure_payload(core_plan, trade_date, candidates, f"minute_data_missing:{','.join(missing)}")

    effective_equity = float(core_plan.get("account_equity") or initial_equity)
    plans = [replay_plan(row, effective_equity) for row in candidates]
    minute_keys = list(REPLAY_MINUTES)
    filled_count = 0
    used_exposure = 0.0
    fills: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    latest_decisions: dict[str, dict[str, Any]] = {}
    reason_counts: Counter[str] = Counter()

    for minute in minute_keys:
        now = datetime.combine(trade_date, datetime.strptime(minute, "%H%M").time(), tzinfo=TZ)
        context = market_context_at(market_data, minute, trade_date)
        for plan in sorted(plans, key=plan_rank):
            symbol = str(plan.get("symbol") or "")
            if any(row["symbol"] == symbol for row in fills):
                continue
            raw_quote = raw_quote_at(market_data[symbol], symbol, minute, trade_date)
            if raw_quote is None:
                continue
            decision = evaluate_plan(
                plan,
                trade_date=trade_date.isoformat(),
                now=now,
                max_age_minutes=1,
                time_gate_ok=True,
                filled_count=filled_count,
                max_fills=max_fills,
                used_exposure=used_exposure,
                equity=effective_equity,
                max_exposure_pct=max_exposure_pct,
                max_trigger_drawdown_pct=1.2,
                max_open_drawdown_pct=1.0,
                confirmation_max_minutes=20,
                market_context=context,
                account_marks_ok=True,
                active_risk_exit_count=0,
                raw_quote=raw_quote,
            )
            latest_decisions[symbol] = {"minute": minute, "reason": decision["reason"], "action": decision["action"]}
            reason_counts[decision["reason"]] += 1
            if decision["action"] == "CONFIRM":
                plan["status"] = "CONFIRMED_TRIGGER"
                plan["confirmation_quote"] = decision["quote"]
                events.append({"minute": minute, "symbol": symbol, "action": "CONFIRM", "reason": decision["reason"]})
            elif decision["action"] == "SUBMIT":
                volume = int(plan.get("volume") or 0)
                price = float(decision["price"])
                gross = price * volume
                fill = mark_fill(symbol, market_data[symbol], minute, price, volume, gross, effective_equity)
                fills.append(fill)
                filled_count += 1
                used_exposure += gross
                events.append({"minute": minute, "symbol": symbol, "action": "FILL", "reason": decision["reason"]})
            elif plan.get("status") == "CONFIRMED_TRIGGER" and decision["reason"] not in {
                "MAX_FILLS_REACHED",
                "NOT_IN_CONTINUOUS_AUCTION",
                "CONFIRMATION_MIN_WAIT",
            }:
                plan["status"] = "WATCH_TRIGGER"
                plan.pop("confirmation_quote", None)
                events.append({"minute": minute, "symbol": symbol, "action": "RESET", "reason": decision["reason"]})

    gross_mark_pnl = sum(float(row["gross_mark_pnl"]) for row in fills)
    commissions = sum(float(row["buy_commission"]) for row in fills)
    payload = base_payload(core_plan, trade_date, candidates, status="PASS")
    payload.update(
        {
            "minute_data_source": "Tencent appstock day/query",
            "data_complete": True,
            "max_fills": max_fills,
            "max_exposure_pct": max_exposure_pct,
            "replay_equity": round(effective_equity, 2),
            "filled_count": len(fills),
            "used_exposure": round(used_exposure, 2),
            "fills": fills,
            "gross_mark_pnl": round(gross_mark_pnl, 2),
            "buy_commission": round(commissions, 2),
            "net_mark_pnl": round(gross_mark_pnl - commissions, 2),
            "net_mark_return_on_equity": round((gross_mark_pnl - commissions) / effective_equity, 8) if effective_equity else 0.0,
            "latest_decisions": latest_decisions,
            "decision_reason_counts": dict(sorted(reason_counts.items())),
            "events": events,
        }
    )
    return payload


def replay_plan(source: dict[str, Any], equity: float) -> dict[str, Any]:
    plan = copy.deepcopy(source)
    reference = float(plan.get("signal_close") or plan.get("trigger_price") or plan.get("upper_price") or 0)
    volume = int((equity * 0.05 / reference) // 100) * 100 if reference > 0 else 0
    payload = copy.deepcopy(plan)
    payload["market_regime"] = "NORMAL"
    payload["local_sim_execution_policy_id"] = CORE_PLAN_POLICY_ID
    payload["research_only"] = True
    plan.update(
        {
            "payload": payload,
            "status": "WATCH_TRIGGER",
            "volume": volume,
            "research_only": True,
            "counterfactual_overrides": ["PLAN_MARKET_REGIME_BLOCKED", "STRICT_ENTRY_REQUIRED"],
        }
    )
    return plan


def market_context_at(market_data: dict[str, dict[str, Any]], minute: str, trade_date: date) -> dict[str, Any]:
    indices: dict[str, dict[str, Any]] = {}
    for symbol in INDEX_SYMBOLS:
        raw = raw_quote_at(market_data[index_data_key(symbol)], symbol, minute, trade_date)
        if raw is None:
            return {"status": "UNAVAILABLE", "indices": indices, "message": f"index_minute_missing:{symbol}:{minute}"}
        prev_close = float(raw["prev_close"])
        price = float(raw["price"])
        indices[symbol] = {**raw, "pct_chg": (price / prev_close - 1) * 100 if prev_close else 0.0}
    return {"status": "OK", "indices": indices, "message": "historical minute replay"}


def raw_quote_at(data: dict[str, Any], symbol: str, minute: str, trade_date: date) -> dict[str, Any] | None:
    prices = data.get("minutes") if isinstance(data.get("minutes"), dict) else {}
    available = [key for key in prices if key <= minute]
    if not available:
        return None
    quote_minute = max(available)
    price = prices[quote_minute]
    elapsed = [float(value) for key, value in prices.items() if key <= quote_minute]
    prev_close = float(data.get("prev_close") or 0)
    return {
        "code": symbol,
        "name": str(data.get("name") or ""),
        "price": float(price),
        "prev_close": prev_close,
        "open": float(data.get("open") or (elapsed[0] if elapsed else price)),
        "high": max(elapsed) if elapsed else float(price),
        "low": min(elapsed) if elapsed else float(price),
        "pct_chg": (float(price) / prev_close - 1) * 100 if prev_close else 0.0,
        "datetime": f"{trade_date.strftime('%Y%m%d')}{quote_minute}00",
    }


def mark_fill(
    symbol: str,
    data: dict[str, Any],
    minute: str,
    fill_price: float,
    volume: int,
    gross: float,
    equity: float,
) -> dict[str, Any]:
    close_price = float(data["minutes"][max(data["minutes"])])
    gross_mark_pnl = (close_price - fill_price) * volume
    commission = max(gross * 0.00025, 5.0)
    return {
        "symbol": symbol,
        "stock_name": str(data.get("name") or ""),
        "fill_minute": minute,
        "fill_price": fill_price,
        "volume": volume,
        "gross": round(gross, 2),
        "close_price": close_price,
        "gross_mark_pnl": round(gross_mark_pnl, 2),
        "buy_commission": round(commission, 2),
        "net_mark_pnl": round(gross_mark_pnl - commission, 2),
        "position_weight_pct": round(gross / equity, 6) if equity else 0.0,
        "valuation_status": "UNREALIZED_CLOSE_MARK",
    }


def fetch_tencent_day(symbol: str, trade_date: date, *, is_index: bool) -> dict[str, Any]:
    market_symbol = tencent_symbol(symbol, is_index=is_index)
    url = "https://web.ifzq.gtimg.cn/appstock/app/day/query?" + urllib.parse.urlencode({"code": market_symbol})
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed Tencent market-data endpoint.
        payload = json.loads(response.read().decode("utf-8"))
    root = payload["data"][market_symbol]
    day = next(item for item in root["data"] if str(item.get("date")) == trade_date.strftime("%Y%m%d"))
    quote = root["qt"][market_symbol]
    minutes = {}
    for line in day["data"]:
        parts = str(line).split()
        if len(parts) >= 2:
            minutes[parts[0]] = float(parts[1])
    if not minutes:
        raise ValueError(f"empty minute data for {symbol} {trade_date}")
    return {
        "symbol": symbol,
        "name": str(quote[1]),
        "prev_close": float(quote[4]),
        "open": float(quote[5]),
        "minutes": minutes,
    }


def tencent_symbol(symbol: str, *, is_index: bool) -> str:
    code = "".join(ch for ch in str(symbol) if ch.isdigit())[:6]
    if is_index:
        return f"sh{code}" if code in {"000001", "000688"} else f"sz{code}"
    return f"sh{code}" if code.startswith(("5", "6", "9")) else f"sz{code}"


def index_data_key(symbol: str) -> str:
    return f"INDEX:{symbol}"


def base_payload(
    core_plan: dict[str, Any], trade_date: date, candidates: list[dict[str, Any]], *, status: str
) -> dict[str, Any]:
    return {
        "schema_version": "chan520_watch_only_counterfactual_v1",
        "policy_id": POLICY_ID,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "trade_date": trade_date.isoformat(),
        "status": status,
        "research_only": True,
        "live_execution_enabled": False,
        "core_plan_policy_id": core_plan.get("policy_id"),
        "core_market_regime": (core_plan.get("market_regime") or {}).get("state"),
        "actual_executable_buy_count": int(core_plan.get("executable_buy_count") or 0),
        "candidate_count": len(candidates),
        "candidate_symbols": [str(row.get("symbol") or "") for row in candidates],
        "counterfactual_overrides": ["PLAN_MARKET_REGIME_BLOCKED", "STRICT_ENTRY_REQUIRED"],
        "guards_retained": [
            "two_stage_confirmation",
            "market_shock",
            "board_risk",
            "relative_weakness",
            "limit_price",
            "ma5_ma20",
            "trigger_geometry",
            "max_fills",
            "max_exposure",
        ],
    }


def failure_payload(
    core_plan: dict[str, Any], trade_date: date, candidates: list[dict[str, Any]], error: str
) -> dict[str, Any]:
    payload = base_payload(core_plan, trade_date, candidates, status="FAIL_CLOSED")
    payload.update({"data_complete": False, "filled_count": 0, "fills": [], "error": error})
    return payload


def summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_date": payload.get("trade_date"),
        "status": payload.get("status"),
        "candidate_count": payload.get("candidate_count", 0),
        "filled_count": payload.get("filled_count", 0),
        "net_mark_pnl": payload.get("net_mark_pnl", 0),
        "research_only": True,
    }


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


if __name__ == "__main__":
    raise SystemExit(main())
