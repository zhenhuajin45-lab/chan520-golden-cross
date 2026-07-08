from __future__ import annotations

from chan520_skill.risk import RiskConfig, allowed_new_position_shares


def test_realized_risk_tracks_budget_without_token_lot() -> None:
    cfg = RiskConfig()
    scenarios = [(10.0, 9.0), (20.0, 18.0), (50.0, 45.0)]
    for entry, stop in scenarios:
        shares = allowed_new_position_shares(100000, 100000, entry, stop, 0, 0, 0, "trend_up", cfg)
        assert shares >= 100
        realized = (entry - stop) * shares / 100000
        assert 0.007 <= realized <= 0.013


def test_caps_reduce_position_when_they_bind() -> None:
    cfg = RiskConfig()
    shares = allowed_new_position_shares(100000, 100000, 10, 9.8, 19500, 0, 0, "trend_up", cfg)
    assert shares == 0
    shares = allowed_new_position_shares(100000, 100000, 10, 9.8, 0, 39500, 0, "trend_up", cfg)
    assert shares == 0
    shares = allowed_new_position_shares(100000, 100000, 10, 9.8, 0, 0, 79500, "trend_up", cfg)
    assert shares == 0
