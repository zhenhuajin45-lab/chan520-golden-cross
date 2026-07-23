from __future__ import annotations

import json

from scripts import push_local_sim_feishu as feishu
from scripts.push_local_sim_feishu import (
    build_plan_card,
    build_review_card,
    build_trade_card,
    push_plan_card,
    push_review_card,
    push_trade_cards,
    send_card,
    write_audit_files,
)


def sample_payload():
    return {
        "trade_date": "2026-07-15",
        "account": {
            "account_id": "local-sim",
            "initial_cash": 1_000_000.0,
            "cash": 998_995.0,
            "market_value": 1_000.0,
            "total_equity": 999_995.0,
            "total_pnl": -5.0,
            "total_pnl_pct": -0.000005,
            "gross_exposure_pct": 0.001,
            "open_position_count": 1,
            "order_count": 1,
            "fill_count": 1,
        },
        "positions": [
            {
                "symbol": "SHSE.600288",
                "stock_name": "大恒科技",
                "shares": 100,
                "average_price": 10.0,
                "market_value": 1000.0,
                "signal_name": "trend_pullback_entry",
                "entry_reason": "趋势向上，回踩后收回 MA20",
                "entry_notes": "R:R>=2",
                "peak_unrealized_pnl_pct": 0.04,
                "profit_protection_armed": True,
            }
        ],
        "orders": [
            {
                "order_id": "LSIM-1",
                "client_order_id": "demo-1",
                "symbol": "SHSE.600288",
                "stock_name": "大恒科技",
                "session_date": "2026-07-15",
                "signal_name": "trend_pullback_entry",
                "entry_reason": "趋势向上，回踩后收回 MA20",
                "notes": "R:R>=2",
            }
        ],
        "fills": [
            {
                "fill_id": "FILL-1",
                "order_id": "LSIM-1",
                "client_order_id": "demo-1",
                "symbol": "SHSE.600288",
                "stock_name": "大恒科技",
                "side": "BUY",
                "volume": 100,
                "price": 10.0,
                "gross": 1000.0,
                "commission": 5.0,
                "stamp_duty": 0.0,
                "session_date": "2026-07-15",
                "created_at": "2026-07-15T09:31:00+08:00",
                "signal_name": "trend_pullback_entry",
                "entry_reason": "趋势向上，回踩后收回 MA20",
                "notes": "R:R>=2",
            }
        ],
        "planned_orders": [
            {
                "planned_order_id": "PLAN-1",
                "trade_date": "2026-07-15",
                "symbol": "SHSE.600288",
                "stock_name": "大恒科技",
                "side": "BUY",
                "volume": 100,
                "status": "PLANNED",
                "lower_price": 9.95,
                "upper_price": 10.3,
                "trigger_price": 10.0,
                "reason_text": "趋势回踩计划入场",
                "payload": {
                    "geometry_valid": True,
                    "t1_loss_buffer_pct": 0.075,
                },
            }
        ],
        "daily": [
            {
                "trade_date": "2026-07-15",
                "fill_count": 1,
                "buy_gross": 1000.0,
                "sell_gross": 0.0,
                "fees": 5.0,
            }
        ],
    }


def test_build_trade_card_uses_interactive_card():
    payload = sample_payload()
    card = build_trade_card(payload, payload["fills"][0], payload["orders"][0])

    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "green"
    assert "买入成交" in card["card"]["header"]["title"]["content"]
    assert "大恒科技" in str(card)
    assert "入场理由" in str(card)
    assert "趋势向上" in str(card)


def test_build_review_card_contains_account_summary():
    payload = sample_payload()
    payload["counterfactual_replay"] = {
        "status": "PASS",
        "candidate_count": 2,
        "filled_count": 1,
        "net_mark_pnl": 128.0,
        "net_mark_return_on_equity": 0.000128,
        "fills": [],
    }
    card = build_review_card(payload, "2026-07-15")

    content = str(card)
    assert card["msg_type"] == "interactive"
    assert "每日账户复盘" in content
    assert "初始资金" in content
    assert "账户盈亏" in content
    assert "大恒科技" in content
    assert "SHSE.600288" in content
    assert "理由分布" in content
    assert "R:R>=2" in content
    assert "利润保护" in content
    assert "待触发/计划订单" in content
    assert "趋势回踩计划入场" in content
    assert "反事实回放" in content
    assert "不写入模拟盘账本" in content


def test_build_plan_card_separates_executable_and_watch_only_orders():
    payload = sample_payload()
    payload["planned_orders"][0]["status"] = "WATCH_TRIGGER"
    payload["planned_orders"].append(
        {
            "planned_order_id": "PLAN-WATCH",
            "trade_date": "2026-07-15",
            "symbol": "SZSE.000001",
            "stock_name": "平安银行",
            "side": "BUY",
            "volume": 0,
            "status": "WATCH_ONLY",
            "reason_text": "严格条件未满足",
        }
    )

    card = build_plan_card(payload, "2026-07-15")

    content = str(card)
    assert "盘前核心交易计划" in content
    assert "严格待触发" in content
    assert "观察池" in content
    assert "大恒科技" in content
    assert "平安银行" in content


def test_push_plan_card_dry_run_does_not_mark_state():
    state = {"pushed_keys": {}}
    audit = push_plan_card(
        payload=sample_payload(),
        state=state,
        trade_date="2026-07-15",
        dry_run=True,
        force=False,
        timeout=1,
    )

    assert audit["sent_count"] == 0
    assert audit["skipped_count"] == 1
    assert state["pushed_keys"] == {}


def test_plan_card_exposes_generation_failure_reason():
    payload = sample_payload()
    payload["core_plan"] = {
        "status": "GENERATION_FAILED",
        "failure_step": "generate_core_plan",
        "failure_reason": "Sina and Eastmoney unavailable",
    }

    card = build_plan_card(payload, "2026-07-15")

    content = str(card)
    assert "GENERATION_FAILED" in content
    assert "generate_core_plan" in content
    assert "Sina and Eastmoney unavailable" in content


def test_push_trade_cards_dry_run_does_not_mark_state():
    state = {"pushed_keys": {}}
    audit = push_trade_cards(
        payload=sample_payload(),
        state=state,
        trade_date="2026-07-15",
        dry_run=True,
        force=False,
        timeout=1,
    )

    assert audit["candidate_count"] == 1
    assert audit["sent_count"] == 0
    assert audit["skipped_count"] == 1
    assert state["pushed_keys"] == {}


def test_core_trade_push_honors_legacy_deduplication_key():
    state = {"pushed_keys": {"fill:FILL-1": {"pushed_at": "before-account-scopes"}}}

    audit = push_trade_cards(
        payload=sample_payload(),
        state=state,
        trade_date="2026-07-15",
        dry_run=False,
        force=False,
        timeout=1,
    )

    assert audit["sent_count"] == 0
    assert audit["skipped_count"] == 1
    assert audit["skipped"][0]["reason"] == "already_pushed"


def test_feishu_cards_include_isolated_bear_pilot_plan_trade_and_review():
    payload = sample_payload()
    payload["core_plan"] = {
        "status": "PASS",
        "execution_funnel": {"scanned_count": 4976, "strict_count": 1, "watch_count": 23, "core_executable_count": 0, "bear_pilot_count": 1},
        "research_cohorts": {
            "bear_pilot": {
                "status": "ARMED",
                "position_cap_pct": 0.025,
                "account_exposure_cap_pct": 0.05,
                "max_positions": 2,
            }
        },
    }
    pilot_fill = {
        **payload["fills"][0],
        "fill_id": "PILOT-FILL-1",
        "order_id": "PILOT-ORDER-1",
        "symbol": "SHSE.600671",
        "stock_name": "天目药业",
        "entry_reason": "熊市防御形态，R:R>=2",
    }
    pilot_order = {**payload["orders"][0], "order_id": "PILOT-ORDER-1", "symbol": "SHSE.600671", "stock_name": "天目药业"}
    pilot_plan = {
        **payload["planned_orders"][0],
        "planned_order_id": "BEAR-PILOT:2026-07-15:600671",
        "symbol": "SHSE.600671",
        "stock_name": "天目药业",
        "status": "WATCH_TRIGGER",
        "reason_text": "熊市防御研究小仓",
    }
    payload["research_pilot"] = {
        "status": "ACTIVE",
        "valuation_complete": True,
        "account": {**payload["account"], "account_id": "local-sim-bear-pilot", "gross_exposure_pct": 0.025},
        "positions": [],
        "orders": [pilot_order],
        "fills": [pilot_fill],
        "planned_orders": [pilot_plan],
    }

    trade_card = build_trade_card(payload, {**pilot_fill, "_research_pilot": True}, pilot_order)
    plan_card = build_plan_card(payload, "2026-07-15")
    review_card = build_review_card(payload, "2026-07-15")
    audit = push_trade_cards(
        payload=payload,
        state={"pushed_keys": {}},
        trade_date="2026-07-15",
        dry_run=True,
        force=False,
        timeout=1,
    )

    assert "熊市研究小仓" in str(trade_card)
    assert "天目药业" in str(plan_card)
    assert "单票上限" in str(plan_card)
    assert "4976" in str(plan_card)
    assert "独立于核心" in str(review_card)
    assert "熊市防御形态" in str(review_card)
    assert audit["candidate_count"] == 2
    assert {row["key"] for row in audit["cards"]} == {"fill:core:FILL-1", "fill:bear_pilot:PILOT-FILL-1"}


def test_push_review_card_skips_existing_key():
    state = {"pushed_keys": {"review:2026-07-15": {"pushed_at": "x"}}}
    audit = push_review_card(
        payload=sample_payload(),
        state=state,
        trade_date="2026-07-15",
        dry_run=False,
        force=False,
        timeout=1,
    )

    assert audit["sent_count"] == 0
    assert audit["skipped_count"] == 1
    assert audit["error_count"] == 0


def test_push_review_card_fails_closed_when_valuation_is_incomplete():
    payload = sample_payload()
    payload["valuation_complete"] = False
    payload["valuation_status"] = "DEGRADED"
    state = {"pushed_keys": {}}

    audit = push_review_card(
        payload=payload,
        state=state,
        trade_date="2026-07-15",
        dry_run=True,
        force=False,
        timeout=1,
    )

    assert audit["sent_count"] == 0
    assert audit["error_count"] == 1
    assert audit["errors"][0]["error"] == "valuation_incomplete"


def test_audit_files_preserve_latest_run_for_each_message_type(tmp_path):
    base = {
        "schema_version": "chan520_local_sim_feishu_audit_v0",
        "trade_date": "2026-07-20",
        "data_path": "dashboard.json",
        "webhook": {"configured": True},
    }
    write_audit_files(
        tmp_path,
        {**base, "generated_at": "2026-07-20T08:04:19+08:00", "runs": [{"type": "plan", "sent_count": 1}]},
    )
    write_audit_files(
        tmp_path,
        {**base, "generated_at": "2026-07-20T15:20:04+08:00", "runs": [{"type": "review", "sent_count": 1}]},
    )

    import json

    aggregate = json.loads((tmp_path / "feishu_push_audit.json").read_text(encoding="utf-8"))
    assert [row["type"] for row in aggregate["runs"]] == ["plan", "review"]
    assert len(aggregate["history"]) == 2
    assert (tmp_path / "feishu_push_audit_plan.json").exists()
    assert (tmp_path / "feishu_push_audit_review.json").exists()
    assert len(list((tmp_path / "runs").glob("*.json"))) == 2


def test_send_card_retries_feishu_frequency_limit(monkeypatch):
    responses = [
        {"code": 11232, "msg": "frequency limited"},
        {"code": 0, "msg": "success"},
    ]
    sleeps = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(self.payload).encode()

    monkeypatch.setattr(feishu.urllib.request, "urlopen", lambda *_args, **_kwargs: Response(responses.pop(0)))
    monkeypatch.setattr(feishu.time, "sleep", sleeps.append)

    result = send_card("https://example.invalid/hook", {"msg_type": "interactive"}, timeout=1)

    assert result["ok"] is True
    assert result["attempts"] == 2
    assert sleeps == [1.0]
