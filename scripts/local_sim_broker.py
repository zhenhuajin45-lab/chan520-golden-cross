from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.broker_adapter import (
    BrokerOrderRequest,
    BrokerSide,
    LocalSimBrokerAdapter,
    LocalSimBrokerConfig,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local SQLite simulated broker")
    parser.add_argument("--ledger", default="data/local_sim/broker.sqlite")
    parser.add_argument("--account-id", default="local-sim")
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--lot-size", type=int, default=100)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("account")
    for name in ("buy", "sell"):
        order = sub.add_parser(name)
        order.add_argument("--symbol", required=True)
        order.add_argument("--volume", type=int, required=True)
        order.add_argument("--price", type=float, required=True)
        order.add_argument("--client-order-id", default="")
        order.add_argument("--order-intent-id", default="")
        order.add_argument("--run-id", default="")
        order.add_argument("--session-date", required=True)
        order.add_argument("--signal-name", default="")
        order.add_argument("--entry-reason", default="")
        order.add_argument("--exit-reason", default="")
        order.add_argument("--risk-reason", default="")
        order.add_argument("--risk-reason-code", default="")
        order.add_argument("--notes", default="")
        order.add_argument("--push-feishu", action="store_true")
        order.add_argument("--push-dry-run", action="store_true")
        order.add_argument("--dashboard-output", default="web_dashboard/data/local_sim/latest_account.json")

    args = parser.parse_args()
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id=args.account_id,
            initial_cash=args.initial_cash,
            ledger_path=args.ledger,
            lot_size=args.lot_size,
        )
    )
    if args.cmd == "init":
        adapter.initialize_account()
        print_json(adapter.account_snapshot())
        return 0
    if args.cmd == "account":
        print_json(adapter.account_snapshot())
        return 0
    side = BrokerSide.BUY if args.cmd == "buy" else BrokerSide.SELL
    result = adapter.submit_order(
        BrokerOrderRequest(
            symbol=args.symbol,
            side=side,
            volume=args.volume,
            price=args.price,
            client_order_id=args.client_order_id,
            order_intent_id=args.order_intent_id,
            run_id=args.run_id,
            session_date=args.session_date,
            position_effect="OPEN" if side is BrokerSide.BUY else "CLOSE",
            extra={
                "signal_name": args.signal_name,
                "entry_reason": args.entry_reason,
                "exit_reason": args.exit_reason,
                "risk_reason": args.risk_reason,
                "risk_reason_code": args.risk_reason_code,
                "notes": args.notes,
            },
        )
    )
    payload = result.as_payload()
    if args.push_feishu and result.accepted:
        payload["feishu_push"] = push_feishu_after_fill(args)
    print_json(payload)
    return 0 if result.accepted else 2


def push_feishu_after_fill(args: argparse.Namespace) -> dict:
    from export_local_sim_dashboard import build_payload
    from push_local_sim_feishu import push_trade_cards, read_state, write_json

    data = build_payload(Path(args.ledger), args.account_id, args.session_date)
    dashboard_path = Path(args.dashboard_output)
    write_json(dashboard_path, data)
    state_path = ROOT / "data" / "local_sim" / "feishu_push_state.json"
    state = read_state(state_path)
    trade_date = args.session_date or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    audit = push_trade_cards(
        payload=data,
        state=state,
        trade_date=trade_date,
        dry_run=args.push_dry_run,
        force=False,
        timeout=8,
    )
    if audit["sent_count"]:
        write_json(state_path, state)
    return {
        "trade_date": trade_date,
        "sent_count": audit["sent_count"],
        "skipped_count": audit["skipped_count"],
        "error_count": audit["error_count"],
        "dry_run": args.push_dry_run,
    }


def print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
