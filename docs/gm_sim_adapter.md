# GM Simulation Adapter Scaffold

This document records the V5.2D GM simulation adapter boundary. It is a
scaffold only. It does not make the strategy shadow-ready and does not enable
live or simulation submissions by default.

## Reference Pattern

The adapter follows the GM contract shape used by the local second-board
production strategy:

- load sensitive token from `GM_TOKEN`, not from committed config;
- bind account explicitly with `set_account_id(account_id)` when available;
- submit through `order_volume(..., account=account_id)`;
- keep an environment guard in front of the final side effect;
- record structured order results instead of parsing natural-language logs.

## Current Guard Contract

`GMSimBrokerAdapter` is fail-closed:

- `dry_run=True` blocks all GM calls;
- `enable_submit=False` blocks all GM calls;
- `account_type` must be exactly `SIMULATION`;
- `account_id` is required only when actual submit is enabled;
- `GM_TOKEN` is required only when actual submit is enabled;
- identity, data, and reconciliation preflight gates must pass before submit;
- invalid symbol, price, or volume is rejected before GM access;
- GM SDK is lazily imported only after all local guards pass.

The adapter returns `BrokerOrderResult` with an ASCII `reason_code` from
`BrokerGuardCode`. Chinese labels should only be rendered by reports or UI
layers.

## Integration Boundary

The production side effect is intentionally narrow:

```python
adapter.submit_order(request, preflight)
```

Only after all guards pass can the adapter call:

```python
order_volume(
    symbol=request.symbol,
    volume=request.volume,
    side=OrderSide_Buy or OrderSide_Sell,
    order_type=OrderType_Limit,
    position_effect=PositionEffect_Open or PositionEffect_Close,
    price=request.price,
    account=config.account_id,
)
```

## Not Yet Done

- The portfolio open/close kernel is not yet wired to this adapter.
- GM callbacks are not yet mapped into paper-store fill and reconciliation
  events.
- `shadow_readiness` remains `false`.
- No Alpha, MA, entry threshold, R:R, ATR, risk, or exit parameters were changed.
