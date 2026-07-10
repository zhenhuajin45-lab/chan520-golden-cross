from __future__ import annotations

from dataclasses import dataclass, field
from math import floor


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade: float = 0.01
    first_tranche_pct: float = 0.15
    max_position_pct: float = 0.20
    max_sector_pct: float = 0.40
    cash_reserve_pct: float = 0.20
    regime_exposure: dict[str, float] = field(
        default_factory=lambda: {"trend_up": 0.80, "range": 0.50, "down": 0.30}
    )
    # Values are incremental cash caps after the initial tranche, not multipliers.
    # The default therefore means 15% initial capital plus one 5% add, capped at 20%.
    pyramid_increment_pcts: tuple[float, ...] = (0.05,)
    atr_k: float = 2.0
    target_atr_k: float = 3.0
    trail_activation: float = 0.04
    time_stop_days: int = 7
    max_dd_stop: float = 0.15
    daily_loss_stop: float = 0.015
    weekly_loss_stop: float = 0.04
    min_sector_members: int = 8


@dataclass
class AccountRiskState:
    peak_equity: float
    current_week: tuple[int, int] | None = None
    week_start_equity: float | None = None
    halted_next_session: bool = False
    stopped_for_drawdown: bool = False
    active_week_size_multiplier: float = 1.0
    next_week_size_multiplier: float = 1.0


def position_size(equity: float, entry: float, stop: float, risk_per_trade: float = 0.01, lot: int = 100) -> int:
    if equity <= 0 or entry <= 0 or stop <= 0 or entry <= stop or risk_per_trade <= 0:
        return 0
    raw = equity * risk_per_trade / (entry - stop)
    return int(floor(raw / lot) * lot)


def cap_shares_by_value(equity: float, price: float, pct: float, lot: int = 100) -> int:
    if equity <= 0 or price <= 0 or pct <= 0:
        return 0
    return int(floor((equity * pct / price) / lot) * lot)


def max_total_exposure(regime: str, config: RiskConfig) -> float:
    return min(config.regime_exposure.get(regime, 0.30), 1 - config.cash_reserve_pct)


def allowed_new_position_shares(
    equity: float,
    cash: float,
    entry: float,
    stop: float,
    current_symbol_value: float,
    current_sector_value: float,
    current_gross_value: float,
    regime: str,
    config: RiskConfig,
    lot: int = 100,
    pyramid_stage: int = 0,
    size_multiplier: float = 1.0,
) -> int:
    if cash <= 0 or entry <= stop or size_multiplier <= 0:
        return 0
    risk_shares = position_size(equity, entry, stop, config.risk_per_trade * size_multiplier, lot)
    single_remaining = max(equity * config.max_position_pct - current_symbol_value, 0)
    sector_remaining = max(equity * config.max_sector_pct - current_sector_value, 0)
    exposure_remaining = max(equity * max_total_exposure(regime, config) - current_gross_value, 0)
    cash_remaining = max(cash - equity * config.cash_reserve_pct, 0)
    tranche_pct = config.first_tranche_pct if pyramid_stage <= 0 else _pyramid_increment_pct(config, pyramid_stage)
    tranche_remaining = max(equity * tranche_pct * size_multiplier, 0)
    cap_by_value = min(single_remaining, sector_remaining, exposure_remaining, cash_remaining, tranche_remaining)
    cap_shares = int(floor((cap_by_value / entry) / lot) * lot)
    return min(risk_shares, cap_shares)


def _pyramid_increment_pct(config: RiskConfig, pyramid_stage: int) -> float:
    steps = config.pyramid_increment_pcts
    if not steps:
        return 0.0
    # stage 1 is the first add, so it maps to increments[0].
    idx = max(0, min(pyramid_stage - 1, len(steps) - 1))
    return max(steps[idx], 0)


def trailing_stop(
    entry: float,
    close: float,
    ma10: float | None,
    atr14: float | None,
    config: RiskConfig,
    current_stop: float | None = None,
) -> float:
    if close < entry * (1 + config.trail_activation):
        return current_stop if current_stop is not None else max(entry - config.atr_k * atr14, 0.01) if atr14 else entry * 0.92
    candidates = [current_stop if current_stop is not None else entry]
    if ma10 is not None:
        candidates.append(ma10)
    if atr14 is not None and atr14 > 0:
        candidates.append(close - config.atr_k * atr14)
    return max(candidates)


def time_stop_trigger(entry: float, close: float, holding_days: int, config: RiskConfig, min_gain: float = 0.02) -> bool:
    return holding_days >= config.time_stop_days and close < entry * (1 + min_gain)


def update_account_risk(
    state: AccountRiskState,
    equity: float,
    previous_equity: float,
    iso_year_week: tuple[int, int],
    config: RiskConfig,
) -> AccountRiskState:
    if state.current_week is None:
        begin_trading_session(state, iso_year_week, previous_equity)
    state.peak_equity = max(state.peak_equity, equity)
    drawdown = 1 - equity / state.peak_equity if state.peak_equity > 0 else 0
    # A portfolio-level drawdown halt is latched until an explicit restart.
    state.stopped_for_drawdown = state.stopped_for_drawdown or drawdown >= config.max_dd_stop
    day_loss = (previous_equity - equity) / previous_equity if previous_equity > 0 else 0
    state.halted_next_session = day_loss >= config.daily_loss_stop
    if state.week_start_equity:
        week_loss = (state.week_start_equity - equity) / state.week_start_equity
        if week_loss >= config.weekly_loss_stop:
            state.next_week_size_multiplier = 0.5
    return state


def begin_trading_session(
    state: AccountRiskState, iso_year_week: tuple[int, int], previous_close_equity: float
) -> AccountRiskState:
    """Apply a prior-week reduction before the first open of a new week."""
    if state.current_week != iso_year_week:
        state.current_week = iso_year_week
        state.week_start_equity = previous_close_equity
        state.active_week_size_multiplier = state.next_week_size_multiplier
        state.next_week_size_multiplier = 1.0
    return state
