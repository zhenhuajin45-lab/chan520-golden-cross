from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from chan520_skill.broker_adapter import (
    BrokerGuardCode,
    BrokerOrderRequest,
    BrokerPreflight,
    BrokerSide,
    GMSimBrokerAdapter,
    GMSimulationConfig,
    InternalPaperBroker,
    LocalSimBrokerAdapter,
    LocalSimBrokerConfig,
)


def order_request() -> BrokerOrderRequest:
    return BrokerOrderRequest(
        symbol="SHSE.600288",
        side=BrokerSide.BUY,
        volume=100,
        price=15.55,
        order_intent_id="intent1",
        run_id="run1",
        session_date="2026-07-07",
    )


def passing_preflight() -> BrokerPreflight:
    return BrokerPreflight(identity_pass=True, data_gate_pass=True, reconciliation_pass=True)


class FakeGmClient:
    OrderSide_Buy = 1
    OrderSide_Sell = 2
    OrderType_Limit = 11
    PositionEffect_Open = 21
    PositionEffect_Close = 22

    def __init__(self) -> None:
        self.token = ""
        self.account_id = ""
        self.orders: list[dict] = []

    def set_token(self, token: str) -> None:
        self.token = token

    def set_account_id(self, account_id: str) -> None:
        self.account_id = account_id

    def order_volume(self, **kwargs):
        self.orders.append(kwargs)
        return SimpleNamespace(cl_ord_id="cl-1", counter_order_id="ctr-1")


def test_internal_paper_accepts_without_external_submit():
    result = InternalPaperBroker().submit_order(order_request())

    assert result.accepted is True
    assert result.submitted is False
    assert result.reason_code is BrokerGuardCode.PAPER_ACCEPTED


def test_gm_sim_default_dry_run_does_not_touch_client(monkeypatch):
    monkeypatch.delenv("GM_TOKEN", raising=False)
    client = FakeGmClient()
    adapter = GMSimBrokerAdapter(
        GMSimulationConfig(account_id="sim-account", dry_run=True, enable_submit=False),
        gm_client=client,
    )

    result = adapter.submit_order(order_request(), passing_preflight())

    assert result.reason_code is BrokerGuardCode.DRY_RUN
    assert result.submitted is False
    assert client.orders == []
    assert client.token == ""


def test_gm_sim_rejects_non_simulation_account_before_submit(monkeypatch):
    monkeypatch.setenv("GM_TOKEN", "token")
    client = FakeGmClient()
    adapter = GMSimBrokerAdapter(
        GMSimulationConfig(account_id="real-account", account_type="LIVE", dry_run=False, enable_submit=True),
        gm_client=client,
    )

    result = adapter.submit_order(order_request(), passing_preflight())

    assert result.reason_code is BrokerGuardCode.NON_SIM_ACCOUNT_TYPE
    assert client.orders == []


def test_gm_sim_requires_account_id_when_submit_enabled(monkeypatch):
    monkeypatch.setenv("GM_TOKEN", "token")
    adapter = GMSimBrokerAdapter(
        GMSimulationConfig(account_id="", dry_run=False, enable_submit=True),
        gm_client=FakeGmClient(),
    )

    result = adapter.submit_order(order_request(), passing_preflight())

    assert result.reason_code is BrokerGuardCode.ACCOUNT_ID_REQUIRED
    assert result.submitted is False


def test_gm_sim_requires_token_when_submit_enabled(monkeypatch):
    monkeypatch.delenv("GM_TOKEN", raising=False)
    client = FakeGmClient()
    adapter = GMSimBrokerAdapter(
        GMSimulationConfig(account_id="sim-account", dry_run=False, enable_submit=True),
        gm_client=client,
    )

    result = adapter.submit_order(order_request(), passing_preflight())

    assert result.reason_code is BrokerGuardCode.TOKEN_REQUIRED
    assert client.orders == []


def test_gm_sim_requires_preflight_gates(monkeypatch):
    monkeypatch.setenv("GM_TOKEN", "token")
    client = FakeGmClient()
    adapter = GMSimBrokerAdapter(
        GMSimulationConfig(account_id="sim-account", dry_run=False, enable_submit=True),
        gm_client=client,
    )

    result = adapter.submit_order(order_request(), BrokerPreflight(identity_pass=True, data_gate_pass=False, reconciliation_pass=True))

    assert result.reason_code is BrokerGuardCode.DATA_GATE_FAILED
    assert client.orders == []


def test_gm_sim_submit_passes_explicit_account_and_constants(monkeypatch):
    monkeypatch.setenv("GM_TOKEN", "token")
    client = FakeGmClient()
    adapter = GMSimBrokerAdapter(
        GMSimulationConfig(account_id="sim-account", dry_run=False, enable_submit=True),
        gm_client=client,
    )

    result = adapter.submit_order(order_request(), passing_preflight())

    assert result.reason_code is BrokerGuardCode.OK
    assert result.submitted is True
    assert result.accepted is True
    assert result.cl_ord_id == "cl-1"
    assert client.token == "token"
    assert client.account_id == "sim-account"
    assert client.orders == [
        {
            "symbol": "SHSE.600288",
            "volume": 100,
            "side": 1,
            "order_type": 11,
            "position_effect": 21,
            "price": 15.55,
            "account": "sim-account",
        }
    ]


def test_invalid_order_is_rejected_before_gm_submit(monkeypatch):
    monkeypatch.setenv("GM_TOKEN", "token")
    client = FakeGmClient()
    adapter = GMSimBrokerAdapter(
        GMSimulationConfig(account_id="sim-account", dry_run=False, enable_submit=True),
        gm_client=client,
    )
    bad = BrokerOrderRequest(symbol="SHSE.600288", side=BrokerSide.BUY, volume=0, price=15.55)

    result = adapter.submit_order(bad, passing_preflight())

    assert result.reason_code is BrokerGuardCode.INVALID_ORDER
    assert client.orders == []


def test_local_sim_broker_starts_with_one_million_and_fills_buy(tmp_path):
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(tmp_path / "local_sim.sqlite"),
        )
    )

    result = adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.0,
            client_order_id="local-1",
            session_date="2026-07-15",
        )
    )
    snapshot = adapter.account_snapshot()

    assert result.reason_code is BrokerGuardCode.OK
    assert result.submitted is True
    assert result.accepted is True
    assert snapshot["initial_cash"] == 1_000_000.0
    assert snapshot["cash"] == 998994.99
    assert snapshot["positions"] == [
        {"symbol": "SHSE.600288", "shares": 100, "average_price": 10.0},
    ]


def test_local_sim_broker_persists_order_reasons(tmp_path):
    ledger = tmp_path / "local_sim.sqlite"
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(ledger),
        )
    )

    result = adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.0,
            client_order_id="reason-1",
            session_date="2026-07-15",
            extra={
                "signal_name": "trend_pullback_entry",
                "entry_reason": "趋势向上，回踩后收回 MA20",
                "notes": "R:R>=2",
            },
        )
    )

    with sqlite3.connect(ledger) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select signal_name, entry_reason, notes from orders where client_order_id = ?",
            ("reason-1",),
        ).fetchone()

    assert result.reason_code is BrokerGuardCode.OK
    assert row["signal_name"] == "trend_pullback_entry"
    assert row["entry_reason"] == "趋势向上，回踩后收回 MA20"
    assert row["notes"] == "R:R>=2"


def test_local_sim_broker_records_planned_orders(tmp_path):
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(tmp_path / "local_sim.sqlite"),
        )
    )

    planned_id = adapter.record_planned_order(
        {
            "planned_order_id": "plan-1",
            "pending_order_id": "pending-1",
            "order_intent_id": "intent-1",
            "run_id": "run-1",
            "trade_date": "2026-07-15",
            "symbol": "SHSE.600288",
            "side": "BUY",
            "volume": 100,
            "signal_close": 10.0,
            "lower_price": 9.95,
            "upper_price": 10.3,
            "planned_stop": 9.2,
            "planned_target": 12.0,
            "reason_text": "趋势回踩计划入场",
        }
    )
    adapter.mark_planned_order("plan-1", "FILLED", "filled by kernel")
    rows = adapter.planned_orders_snapshot()

    assert planned_id == "plan-1"
    assert rows[0]["planned_order_id"] == "plan-1"
    assert rows[0]["status"] == "FILLED"
    assert rows[0]["last_message"] == "filled by kernel"


def test_local_sim_broker_clears_stale_message_when_resolved_risk_reactivates(tmp_path):
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(tmp_path / "local_sim.sqlite"),
        )
    )
    payload = {
        "planned_order_id": "RISK:2026-07-16:600288",
        "trade_date": "2026-07-16",
        "symbol": "600288",
        "side": "SELL",
        "volume": 100,
        "status": "RISK_CANDIDATE",
        "trigger_price": 9.8,
    }

    adapter.record_planned_order(payload)
    adapter.mark_planned_order(
        payload["planned_order_id"],
        "RESOLVED_RISK",
        "risk conditions no longer persist",
        {"price": 10.0},
    )
    adapter.record_planned_order({**payload, "trigger_price": 9.7})
    row = adapter.planned_orders_snapshot()[0]

    assert row["status"] == "RISK_CANDIDATE"
    assert row["last_message"] == ""
    assert row["quote_json"] == "{}"


def test_local_sim_broker_requires_session_date(tmp_path):
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(tmp_path / "local_sim.sqlite"),
        )
    )

    result = adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.0,
            client_order_id="missing-date",
        )
    )

    assert result.reason_code is BrokerGuardCode.SESSION_DATE_REQUIRED
    assert result.accepted is False


def test_local_sim_broker_rejects_non_lot_and_insufficient_sell(tmp_path):
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(tmp_path / "local_sim.sqlite"),
        )
    )

    bad_lot = adapter.submit_order(BrokerOrderRequest(symbol="SHSE.600288", side=BrokerSide.BUY, volume=1, price=10.0, session_date="2026-07-15"))
    bad_sell = adapter.submit_order(BrokerOrderRequest(symbol="SHSE.600288", side=BrokerSide.SELL, volume=100, price=10.0, session_date="2026-07-15"))

    assert bad_lot.reason_code is BrokerGuardCode.INVALID_LOT_SIZE
    assert bad_sell.reason_code is BrokerGuardCode.INSUFFICIENT_POSITION


def test_local_sim_broker_client_order_id_is_idempotent(tmp_path):
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(tmp_path / "local_sim.sqlite"),
        )
    )
    request = BrokerOrderRequest(
        symbol="SHSE.600288",
        side=BrokerSide.BUY,
        volume=100,
        price=10.0,
        client_order_id="same-id",
        session_date="2026-07-15",
    )

    first = adapter.submit_order(request)
    second = adapter.submit_order(request)

    assert first.order_id == second.order_id
    assert second.message == "idempotent replay"
    assert adapter.account_snapshot()["cash"] == 998994.99


def test_local_sim_broker_rejects_client_order_id_conflict(tmp_path):
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(tmp_path / "local_sim.sqlite"),
        )
    )

    first = adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.0,
            client_order_id="same-id",
            session_date="2026-07-15",
        )
    )
    conflict = adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=200,
            price=10.0,
            client_order_id="same-id",
            session_date="2026-07-15",
        )
    )

    assert first.reason_code is BrokerGuardCode.OK
    assert conflict.reason_code is BrokerGuardCode.LOCAL_LEDGER_ERROR
    assert conflict.message == "client_order_id conflict"


def test_local_sim_broker_enforces_a_share_t_plus_one(tmp_path):
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(tmp_path / "local_sim.sqlite"),
        )
    )
    buy = BrokerOrderRequest(
        symbol="SHSE.600288",
        side=BrokerSide.BUY,
        volume=100,
        price=10.0,
        client_order_id="buy-1",
        session_date="2026-07-15",
    )
    same_day_sell = BrokerOrderRequest(
        symbol="SHSE.600288",
        side=BrokerSide.SELL,
        volume=100,
        price=10.1,
        client_order_id="sell-1",
        session_date="2026-07-15",
    )
    next_day_sell = BrokerOrderRequest(
        symbol="SHSE.600288",
        side=BrokerSide.SELL,
        volume=100,
        price=10.1,
        client_order_id="sell-2",
        session_date="2026-07-16",
    )

    assert adapter.submit_order(buy).reason_code is BrokerGuardCode.OK
    assert adapter.submit_order(same_day_sell).reason_code is BrokerGuardCode.T_PLUS_ONE_BLOCKED
    assert adapter.submit_order(next_day_sell).reason_code is BrokerGuardCode.OK
    assert adapter.account_snapshot()["positions"] == []


def test_local_sim_broker_blocks_same_day_reentry_after_sell(tmp_path):
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(tmp_path / "local_sim.sqlite"),
        )
    )

    assert adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=200,
            price=10.0,
            client_order_id="buy-1",
            session_date="2026-07-15",
        )
    ).reason_code is BrokerGuardCode.OK
    assert adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.SELL,
            volume=100,
            price=10.2,
            client_order_id="sell-1",
            session_date="2026-07-16",
        )
    ).reason_code is BrokerGuardCode.OK
    reentry = adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.1,
            client_order_id="buy-2",
            session_date="2026-07-16",
        )
    )

    assert reentry.reason_code is BrokerGuardCode.SAME_DAY_RISK_REENTRY_BLOCKED


def test_local_sim_broker_blocks_loss_add(tmp_path):
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(tmp_path / "local_sim.sqlite"),
        )
    )

    assert adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.0,
            client_order_id="buy-1",
            session_date="2026-07-15",
        )
    ).reason_code is BrokerGuardCode.OK
    add = adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=9.9,
            client_order_id="buy-2",
            session_date="2026-07-16",
        )
    )

    assert add.reason_code is BrokerGuardCode.LOSS_ADD_BLOCKED


def test_local_sim_broker_blocks_previous_session_risk_reentry(tmp_path):
    adapter = LocalSimBrokerAdapter(
        LocalSimBrokerConfig(
            account_id="local-test",
            initial_cash=1_000_000.0,
            ledger_path=str(tmp_path / "local_sim.sqlite"),
        )
    )
    assert adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=10.0,
            client_order_id="buy-risk-cooldown-1",
            session_date="2026-07-14",
        )
    ).accepted
    assert adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.SELL,
            volume=100,
            price=9.4,
            client_order_id="sell-risk-cooldown-1",
            session_date="2026-07-15",
            extra={"risk_reason_code": "hard_stop_loss"},
        )
    ).accepted

    reentry = adapter.submit_order(
        BrokerOrderRequest(
            symbol="SHSE.600288",
            side=BrokerSide.BUY,
            volume=100,
            price=9.5,
            client_order_id="buy-risk-cooldown-2",
            session_date="2026-07-16",
            extra={"previous_session_date": "2026-07-15"},
        )
    )

    assert reentry.reason_code is BrokerGuardCode.PREVIOUS_SESSION_RISK_REENTRY_BLOCKED
