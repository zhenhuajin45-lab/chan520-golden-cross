from __future__ import annotations

from chan520_skill.risk import (
    AccountRiskState,
    RiskConfig,
    allowed_new_position_shares,
    max_total_exposure,
    position_size,
    time_stop_trigger,
    trailing_stop,
    update_account_risk,
)


def test_position_size_uses_risk_budget() -> None:
    assert position_size(100000, 10, 9, 0.01) == 1000
    assert position_size(100000, 10, 10, 0.01) == 0


def test_position_caps_and_regime_exposure() -> None:
    cfg = RiskConfig()
    assert max_total_exposure("trend_up", cfg) == 0.80
    assert max_total_exposure("range", cfg) == 0.50
    shares = allowed_new_position_shares(
        equity=100000,
        cash=100000,
        entry=10,
        stop=9,
        current_symbol_value=0,
        current_sector_value=0,
        current_gross_value=0,
        regime="trend_up",
        config=cfg,
        pyramid_stage=0,
    )
    assert shares == 1000
    realized_risk = (10 - 9) * shares / 100000
    assert 0.007 <= realized_risk <= 0.013


def test_sector_and_total_caps_bind() -> None:
    cfg = RiskConfig()
    shares = allowed_new_position_shares(100000, 100000, 10, 9, 0, 39900, 79000, "trend_up", cfg)
    assert shares == 0


def test_trailing_and_time_stop() -> None:
    cfg = RiskConfig()
    assert trailing_stop(10, 10.2, 12, 1, cfg, current_stop=9) == 9
    assert trailing_stop(10, 14, 12, 1, cfg, current_stop=9) == 12
    assert time_stop_trigger(10, 10.1, 7, cfg)


def test_no_token_lot_fallback_when_risk_budget_too_small() -> None:
    cfg = RiskConfig()
    shares = allowed_new_position_shares(100000, 100000, 180, 160, 0, 0, 0, "trend_up", cfg)
    assert shares == 0


def test_circuit_breakers() -> None:
    cfg = RiskConfig()
    state = AccountRiskState(peak_equity=100000)
    update_account_risk(state, 84000, 100000, (2024, 1), cfg)
    assert state.stopped_for_drawdown
    assert state.halted_today
