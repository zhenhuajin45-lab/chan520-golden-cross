from __future__ import annotations

from types import SimpleNamespace

from chan520_skill.broker_adapter import (
    BrokerGuardCode,
    BrokerOrderRequest,
    BrokerPreflight,
    BrokerSide,
    GMSimBrokerAdapter,
    GMSimulationConfig,
    InternalPaperBroker,
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
