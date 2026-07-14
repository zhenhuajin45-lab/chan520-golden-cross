from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from types import SimpleNamespace
from typing import Any, Protocol


class BrokerSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class BrokerGuardCode(str, Enum):
    OK = "OK"
    PAPER_ACCEPTED = "PAPER_ACCEPTED"
    DRY_RUN = "DRY_RUN"
    SUBMIT_DISABLED = "SUBMIT_DISABLED"
    NON_SIM_ACCOUNT_TYPE = "NON_SIM_ACCOUNT_TYPE"
    ACCOUNT_ID_REQUIRED = "ACCOUNT_ID_REQUIRED"
    TOKEN_REQUIRED = "TOKEN_REQUIRED"
    IDENTITY_GATE_FAILED = "IDENTITY_GATE_FAILED"
    DATA_GATE_FAILED = "DATA_GATE_FAILED"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    INVALID_ORDER = "INVALID_ORDER"
    GM_UNAVAILABLE = "GM_UNAVAILABLE"
    GM_SUBMIT_EXCEPTION = "GM_SUBMIT_EXCEPTION"


class BrokerAdapterError(RuntimeError):
    """Raised for broker adapter integration defects, not market rejects."""


class GmClientProtocol(Protocol):
    def order_volume(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class BrokerPreflight:
    identity_pass: bool = False
    data_gate_pass: bool = False
    reconciliation_pass: bool = False
    message: str = ""

    def first_failure(self) -> BrokerGuardCode:
        if not self.identity_pass:
            return BrokerGuardCode.IDENTITY_GATE_FAILED
        if not self.data_gate_pass:
            return BrokerGuardCode.DATA_GATE_FAILED
        if not self.reconciliation_pass:
            return BrokerGuardCode.RECONCILIATION_FAILED
        return BrokerGuardCode.OK


@dataclass(frozen=True)
class BrokerOrderRequest:
    symbol: str
    side: BrokerSide
    volume: int
    price: float
    order_type: str = "LIMIT"
    position_effect: str = "OPEN"
    order_intent_id: str = ""
    run_id: str = ""
    session_date: str = ""
    client_order_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> BrokerGuardCode:
        if not self.symbol.strip():
            return BrokerGuardCode.INVALID_ORDER
        if self.volume <= 0:
            return BrokerGuardCode.INVALID_ORDER
        if self.price <= 0:
            return BrokerGuardCode.INVALID_ORDER
        if self.side not in {BrokerSide.BUY, BrokerSide.SELL}:
            return BrokerGuardCode.INVALID_ORDER
        return BrokerGuardCode.OK

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["side"] = self.side.value
        return payload


@dataclass(frozen=True)
class BrokerOrderResult:
    adapter: str
    submitted: bool
    accepted: bool
    reason_code: BrokerGuardCode
    order_id: str = ""
    cl_ord_id: str = ""
    counter_order_id: str = ""
    account_id_present: bool = False
    message: str = ""
    request: dict[str, Any] = field(default_factory=dict)
    raw_order_repr: str = ""

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_code"] = self.reason_code.value
        return payload


@dataclass(frozen=True)
class GMSimulationConfig:
    account_id: str = ""
    account_type: str = "SIMULATION"
    token_env: str = "GM_TOKEN"
    enable_submit: bool = False
    dry_run: bool = True
    require_identity_gate: bool = True
    require_data_gate: bool = True
    require_reconciliation: bool = True


class InternalPaperBroker:
    name = "internal_paper"

    def submit_order(self, request: BrokerOrderRequest, preflight: BrokerPreflight | None = None) -> BrokerOrderResult:
        reason = request.validate()
        if reason is not BrokerGuardCode.OK:
            return BrokerOrderResult(
                adapter=self.name,
                submitted=False,
                accepted=False,
                reason_code=reason,
                request=request.as_payload(),
            )
        return BrokerOrderResult(
            adapter=self.name,
            submitted=False,
            accepted=True,
            reason_code=BrokerGuardCode.PAPER_ACCEPTED,
            request=request.as_payload(),
        )


class GMSimBrokerAdapter:
    """Guarded GM simulation adapter.

    This scaffold mirrors the production GM contract shape while remaining
    fail-closed by default. It never imports or touches GM until submit is
    explicitly enabled and all guards pass.
    """

    name = "gm_simulation"

    def __init__(self, config: GMSimulationConfig, gm_client: GmClientProtocol | None = None) -> None:
        self.config = config
        self._gm_client = gm_client
        self._connected = False

    def submit_order(self, request: BrokerOrderRequest, preflight: BrokerPreflight | None = None) -> BrokerOrderResult:
        preflight = preflight or BrokerPreflight()
        guard = self.preflight_guard(request, preflight)
        if guard is not BrokerGuardCode.OK:
            return BrokerOrderResult(
                adapter=self.name,
                submitted=False,
                accepted=False,
                reason_code=guard,
                account_id_present=bool(self.config.account_id.strip()),
                request=request.as_payload(),
            )

        try:
            client = self._resolve_gm_client()
            self._connect_client(client)
            kwargs = self._order_volume_kwargs(request, client)
            raw_order = client.order_volume(**kwargs)
        except ImportError as exc:
            return self._blocked(request, BrokerGuardCode.GM_UNAVAILABLE, str(exc))
        except Exception as exc:  # pragma: no cover - exercised with tests via fake failures if needed
            return self._blocked(request, BrokerGuardCode.GM_SUBMIT_EXCEPTION, f"{type(exc).__name__}: {exc}")

        cl_ord_id = _first_attr(raw_order, "cl_ord_id", "clOrdId", "client_order_id")
        counter_order_id = _first_attr(raw_order, "counter_order_id", "counterOrderId", "order_id")
        order_id = cl_ord_id or counter_order_id or _first_attr(raw_order, "id")
        return BrokerOrderResult(
            adapter=self.name,
            submitted=True,
            accepted=bool(order_id),
            reason_code=BrokerGuardCode.OK,
            order_id=order_id,
            cl_ord_id=cl_ord_id,
            counter_order_id=counter_order_id,
            account_id_present=bool(self.config.account_id.strip()),
            request=request.as_payload(),
            raw_order_repr=repr(raw_order)[:240],
        )

    def preflight_guard(self, request: BrokerOrderRequest, preflight: BrokerPreflight) -> BrokerGuardCode:
        reason = request.validate()
        if reason is not BrokerGuardCode.OK:
            return reason
        if self.config.account_type.strip().upper() != "SIMULATION":
            return BrokerGuardCode.NON_SIM_ACCOUNT_TYPE
        if self.config.dry_run:
            return BrokerGuardCode.DRY_RUN
        if not self.config.enable_submit:
            return BrokerGuardCode.SUBMIT_DISABLED
        if not self.config.account_id.strip():
            return BrokerGuardCode.ACCOUNT_ID_REQUIRED
        if not os.environ.get(self.config.token_env, "").strip():
            return BrokerGuardCode.TOKEN_REQUIRED
        if self.config.require_identity_gate and not preflight.identity_pass:
            return BrokerGuardCode.IDENTITY_GATE_FAILED
        if self.config.require_data_gate and not preflight.data_gate_pass:
            return BrokerGuardCode.DATA_GATE_FAILED
        if self.config.require_reconciliation and not preflight.reconciliation_pass:
            return BrokerGuardCode.RECONCILIATION_FAILED
        return BrokerGuardCode.OK

    def _connect_client(self, client: GmClientProtocol) -> None:
        if self._connected:
            return
        token = os.environ.get(self.config.token_env, "").strip()
        set_token = getattr(client, "set_token", None)
        if callable(set_token):
            set_token(token)
        set_account_id = getattr(client, "set_account_id", None)
        if callable(set_account_id):
            set_account_id(self.config.account_id.strip())
        self._connected = True

    def _order_volume_kwargs(self, request: BrokerOrderRequest, client: GmClientProtocol) -> dict[str, Any]:
        kwargs = {
            "symbol": request.symbol,
            "volume": int(request.volume),
            "side": _gm_constant(client, "OrderSide_Buy" if request.side is BrokerSide.BUY else "OrderSide_Sell"),
            "order_type": _gm_constant(client, "OrderType_Limit"),
            "position_effect": _gm_constant(
                client,
                "PositionEffect_Open" if request.position_effect.upper() == "OPEN" else "PositionEffect_Close",
            ),
            "price": float(request.price),
            "account": self.config.account_id.strip(),
        }
        return kwargs

    def _resolve_gm_client(self) -> GmClientProtocol:
        if self._gm_client is not None:
            return self._gm_client
        try:
            from gm.api import order_volume, set_account_id, set_token
            from gm.enum import OrderSide_Buy, OrderSide_Sell, OrderType_Limit, PositionEffect_Close, PositionEffect_Open
        except Exception as exc:  # pragma: no cover - depends on optional local GM SDK
            raise ImportError("GM SDK is not available") from exc
        return SimpleNamespace(
            order_volume=order_volume,
            set_account_id=set_account_id,
            set_token=set_token,
            OrderSide_Buy=OrderSide_Buy,
            OrderSide_Sell=OrderSide_Sell,
            OrderType_Limit=OrderType_Limit,
            PositionEffect_Open=PositionEffect_Open,
            PositionEffect_Close=PositionEffect_Close,
        )

    def _blocked(self, request: BrokerOrderRequest, reason: BrokerGuardCode, message: str = "") -> BrokerOrderResult:
        return BrokerOrderResult(
            adapter=self.name,
            submitted=False,
            accepted=False,
            reason_code=reason,
            account_id_present=bool(self.config.account_id.strip()),
            message=message,
            request=request.as_payload(),
        )


def _gm_constant(client: GmClientProtocol, name: str) -> Any:
    return getattr(client, name, name)


def _first_attr(obj: Any, *names: str) -> str:
    if isinstance(obj, dict):
        for name in names:
            value = obj.get(name)
            if value:
                return str(value)
        return ""
    for name in names:
        value = getattr(obj, name, None)
        if value:
            return str(value)
    return ""
