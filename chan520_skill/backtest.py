from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median, mean, pstdev
from typing import Callable

from .alpha_score import score_alpha
from .entry_filters import EntryFilterConfig, apply_four_no_entry, breakeven_win_rate
from .entry_model import trend_pullback_entry
from .indicators import build_indicators, crossed_up, pct_change
from .data import DataError, eastmoney_history, normalize_code, tencent_history
from .market_regime import breadth_above_ma60, build_market_regime, to_regime_states
from .microstructure import is_limit_down, is_limit_up, price_limit
from .models import IndicatorPoint, KLine, RegimeState, StockMeta
from .quality import ensure_data_quality
from .regime import index_history
from .risk import (
    AccountRiskState,
    RiskConfig,
    allowed_new_position_shares,
    begin_trading_session,
    time_stop_trigger,
    trailing_stop,
    update_account_risk,
)
from .sector import DEFAULT_SECTOR_MAP, SectorState, industry_of, sector_state_from_members
from .sector_alpha import SectorAlpha, build_sector_heat, sector_heat_bonus
from .strategy import analyze
from .strategy_modular import evaluate_modular, modular_regime_by_date


COMMISSION_RATE = 0.00025
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 0.0005
TRANSFER_RATE = 0.00001
SLIPPAGE_BPS = 5.0
ENTRY_VERDICT = "��ѡ"
OBSERVE_VERDICT = "�۲�"
V5_STRATEGY_MODES = {
    "strategy_v5_alpha",
    "strategy_v5_alpha_ranked",
    "strategy_v5_alpha_first_fit_frozen",
}


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 100000.0
    fill: str = "next_open"
    commission_rate: float = COMMISSION_RATE
    min_commission: float = MIN_COMMISSION
    stamp_tax_rate: float = STAMP_TAX_RATE
    transfer_rate: float = TRANSFER_RATE
    slippage_bps: float = SLIPPAGE_BPS
    split_date: date | None = None
    min_history: int = 250
    regime_index: str = "000300"
    use_sector: bool = False
    require_industry: bool = True
    signal_adjust: str = "none"
    strategy_mode: str = "strategy_v1_baseline"


HistoryLoader = Callable[..., tuple[StockMeta, list[KLine]]]


@dataclass(frozen=True)
class Trade:
    code: str
    name: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    shares: int
    gross_pnl: float
    costs: float
    net_pnl: float
    holding_days: int
    entry_reason: str
    exit_reason: str
    entry_costs: float = 0.0
    exit_costs: float = 0.0
    entry_count: int = 1


@dataclass
class Position:
    code: str
    name: str
    industry: str
    shares: int
    entry_price: float
    entry_date: date
    entry_reason: str
    stop: float
    highest_high: float
    target: float = 0.0
    pyramid_stage: int = 0
    entry_costs: float = 0.0
    entry_count: int = 1
    holding_bars: int = 0


@dataclass(frozen=True)
class PendingOrder:
    code: str
    side: str
    reason: str
    shares: int = 0
    stop: float = 0.0
    target: float = 0.0
    rr: float = 0.0
    signal_date: date | None = None
    signal_regime: str = "down"


@dataclass(frozen=True)
class Fill:
    date: date
    code: str
    side: str
    price: float
    shares: int
    fee: float
    reason: str
    signal_date: date | None
    stop: float = 0.0
    target: float = 0.0


@dataclass(frozen=True)
class CandidateSignal:
    code: str
    name: str
    date: date
    industry: str
    market_regime: str
    eligible: bool
    market_ok: bool
    alpha_total: float
    trend_score: float
    relative_strength_score: float
    volume_quality_score: float
    risk_score: float
    sector_bonus: float
    sector_heat_score: float
    entry_score: float
    ranking_score: float
    hard_pass: bool
    reasons: tuple[str, ...]

    @property
    def verdict(self) -> str:
        return ENTRY_VERDICT if self.hard_pass else OBSERVE_VERDICT


def is_v5_strategy_mode(strategy_mode: str) -> bool:
    return strategy_mode in V5_STRATEGY_MODES


def is_ranked_v5_strategy_mode(strategy_mode: str) -> bool:
    return strategy_mode in {"strategy_v5_alpha", "strategy_v5_alpha_ranked"}


def rank_candidate_signals(candidates: list[CandidateSignal]) -> list[CandidateSignal]:
    return sorted(
        candidates,
        key=lambda item: (
            -item.ranking_score,
            -item.entry_score,
            -item.relative_strength_score,
            item.code,
        ),
    )


def is_entry_verdict(verdict: str) -> bool:
    return verdict in {ENTRY_VERDICT, "入选", "��ѡ"}


def backtest_code(
    meta: StockMeta,
    rows: list[KLine],
    start: date,
    end: date,
    config: BacktestConfig | None = None,
    regime_state: RegimeState | None = None,
) -> tuple[list[Trade], dict[str, float]]:
    config = config or BacktestConfig()
    ensure_data_quality(rows, min_bars=61)
    rows = [row for row in rows if row.date <= end]
    cash = config.initial_cash
    shares = 0
    entry_price = 0.0
    entry_costs = 0.0
    entry_date: date | None = None
    entry_reason = ""
    trades: list[Trade] = []
    equity_curve: list[tuple[date, float]] = []
    exposure_days = 0
    pending_entry = False
    pending_exit = False
    pending_reason = ""

    for idx in range(61, len(rows)):
        today = rows[idx]
        prev = rows[idx - 1]
        if today.date < start:
            continue

        if pending_exit and shares > 0:
            if not is_limit_down(today, prev.close, meta.code):
                fill_price = _fill_price(today, config.fill) * (1 - config.slippage_bps / 10000)
                sell_cost = _costs(meta.code, fill_price, shares, "sell", config)
                gross = (fill_price - entry_price) * shares
                total_cost = entry_costs + sell_cost
                net = gross - total_cost
                cash += fill_price * shares - sell_cost
                trades.append(
                    Trade(
                        code=meta.code,
                        name=meta.name,
                        entry_date=entry_date or today.date,
                        exit_date=today.date,
                        entry_price=entry_price,
                        exit_price=fill_price,
                        shares=shares,
                        gross_pnl=gross,
                        costs=total_cost,
                        net_pnl=net,
                        holding_days=(today.date - (entry_date or today.date)).days,
                        entry_reason=entry_reason,
                        exit_reason=pending_reason or "exit_rule",
                        entry_costs=entry_costs,
                        exit_costs=sell_cost,
                    )
                )
                shares = 0
                entry_price = 0.0
                entry_costs = 0.0
                entry_date = None
                entry_reason = ""
                pending_exit = False
            equity_curve.append((today.date, cash + shares * today.close))
            continue

        if pending_entry and shares == 0:
            pending_entry = False
            if not is_limit_up(today, prev.close, meta.code):
                raw_price = _fill_price(today, config.fill)
                fill_price = raw_price * (1 + config.slippage_bps / 10000)
                buy_cash = cash * 0.95
                buy_shares = int(buy_cash // (fill_price * 100)) * 100
                if buy_shares > 0:
                    buy_cost = _costs(meta.code, fill_price, buy_shares, "buy", config)
                    total = fill_price * buy_shares + buy_cost
                    if total <= cash:
                        cash -= total
                        shares = buy_shares
                        entry_price = fill_price
                        entry_costs = buy_cost
                        entry_date = today.date
                        entry_reason = pending_reason or "entry_signal"

        history = rows[: idx + 1]
        report = analyze(meta, history, today.date, regime_state=regime_state, min_history=config.min_history)
        if shares > 0:
            exposure_days += 1
            if _exit_signal(report):
                pending_exit = True
                pending_reason = report.verdict
        elif _entry_signal(report):
            pending_entry = True
            pending_reason = report.verdict

        equity_curve.append((today.date, cash + shares * today.close))

    if shares > 0 and rows:
        last = rows[-1]
        fill_price = last.close * (1 - config.slippage_bps / 10000)
        sell_cost = _costs(meta.code, fill_price, shares, "sell", config)
        gross = (fill_price - entry_price) * shares
        total_cost = entry_costs + sell_cost
        net = gross - total_cost
        trades.append(
            Trade(
                code=meta.code,
                name=meta.name,
                entry_date=entry_date or last.date,
                exit_date=last.date,
                entry_price=entry_price,
                exit_price=fill_price,
                shares=shares,
                gross_pnl=gross,
                costs=total_cost,
                net_pnl=net,
                holding_days=(last.date - (entry_date or last.date)).days,
                entry_reason=entry_reason,
                exit_reason="end_of_backtest",
                entry_costs=entry_costs,
                exit_costs=sell_cost,
            )
        )
    metrics = metrics_from_trades(trades, equity_curve, config.initial_cash, exposure_days, split_date=config.split_date)
    return trades, metrics


def backtest_symbols(
    symbols: list[str],
    start: date,
    end: date,
    output_dir: Path,
    config: BacktestConfig | None = None,
    sector_map: dict[str, str] | None = None,
) -> tuple[Path, Path, dict[str, float]]:
    config = config or BacktestConfig()
    return portfolio_backtest_symbols(symbols, start, end, output_dir, config=config, sector_map=sector_map)


def portfolio_backtest_symbols(
    symbols: list[str],
    start: date,
    end: date,
    output_dir: Path,
    config: BacktestConfig | None = None,
    risk_config: RiskConfig | None = None,
    entry_config: EntryFilterConfig | None = None,
    sector_map: dict[str, str] | None = None,
    index_rows: list[KLine] | None = None,
    history_loader: HistoryLoader | None = None,
    eligible_by_date: dict[date, set[str]] | None = None,
    max_positions: int | None = None,
) -> tuple[Path, Path, dict[str, float]]:
    config = config or BacktestConfig()
    risk_config = risk_config or RiskConfig()
    entry_config = entry_config or EntryFilterConfig()
    sector_map = sector_map or DEFAULT_SECTOR_MAP
    if config.signal_adjust not in {"none", "qfq", "hfq"}:
        raise ValueError("signal_adjust must be one of: none, qfq, hfq")
    metas: dict[str, StockMeta] = {}
    # Signal and execution series are distinct contracts.  The default signal
    # series is unadjusted to avoid endpoint-normalized qfq lookahead; callers
    # may inject a separately audited signal adjustment series for research.
    signal_histories: dict[str, list[KLine]] = {}
    execution_histories: dict[str, list[KLine]] = {}
    indicators: dict[str, list[IndicatorPoint]] = {}
    points_by_date: dict[str, dict[date, IndicatorPoint]] = {}
    rows_by_date: dict[str, dict[date, KLine]] = {}
    signal_rows_by_date: dict[str, dict[date, KLine]] = {}
    row_index_by_date: dict[str, dict[date, int]] = {}
    verdicts_by_date: dict[str, dict[date, str]] = {}
    candidate_signals_by_date: dict[str, dict[date, CandidateSignal]] = {}

    for idx, symbol in enumerate(symbols, 1):
        code = normalize_code(symbol)
        lookback = max((end - start).days + 620, 1000)
        if history_loader is not None:
            meta, execution_rows = history_loader(code, end, lookback_days=lookback, adjust="none")
            if config.signal_adjust == "none":
                signal_rows = list(execution_rows)
            else:
                _signal_meta, signal_rows = history_loader(
                    code, end, lookback_days=lookback, adjust=config.signal_adjust
                )
        else:
            try:
                meta, execution_rows = tencent_history(code, end, lookback_days=lookback, adjust="none")
                if config.signal_adjust == "none":
                    signal_rows = list(execution_rows)
                else:
                    _signal_meta, signal_rows = tencent_history(code, end, lookback_days=lookback, adjust=config.signal_adjust)
            except Exception:
                meta, execution_rows = eastmoney_history(code, end, lookback_days=lookback, adjust=0)
                adjust_map = {"none": 0, "qfq": 1, "hfq": 2}
                if config.signal_adjust == "none":
                    signal_rows = list(execution_rows)
                else:
                    _signal_meta, signal_rows = eastmoney_history(
                        code, end, lookback_days=lookback, adjust=adjust_map.get(config.signal_adjust, 0)
                    )
        execution_rows = [row for row in execution_rows if row.date <= end]
        signal_rows = [row for row in signal_rows if row.date <= end]
        common_dates = {row.date for row in execution_rows} & {row.date for row in signal_rows}
        execution_rows = [row for row in execution_rows if row.date in common_dates]
        signal_rows = [row for row in signal_rows if row.date in common_dates]
        ensure_data_quality(execution_rows, min_bars=61)
        ensure_data_quality(signal_rows, min_bars=61)
        metas[code] = meta
        execution_histories[code] = execution_rows
        signal_histories[code] = signal_rows
        pts = build_indicators(signal_rows)
        indicators[code] = pts
        points_by_date[code] = {point.date: point for point in pts}
        rows_by_date[code] = {row.date: row for row in execution_rows}
        signal_rows_by_date[code] = {row.date: row for row in signal_rows}
        row_index_by_date[code] = {row.date: row_idx for row_idx, row in enumerate(signal_rows)}
        if idx == 1 or idx == len(symbols) or idx % 100 == 0:
            print(f"loaded {idx}/{len(symbols)} {code} signal_rows={len(signal_rows)} execution_rows={len(execution_rows)}", flush=True)

    if config.strategy_mode not in {"strategy_v1_baseline", "strategy_v2_modular"} | V5_STRATEGY_MODES:
        raise ValueError(
            "strategy_mode must be strategy_v1_baseline, strategy_v2_modular, "
            "strategy_v5_alpha, strategy_v5_alpha_ranked, or strategy_v5_alpha_first_fit_frozen"
        )
    all_dates = sorted({row.date for rows in execution_histories.values() for row in rows if start <= row.date <= end})
    if index_rows is None:
        lookback = max((end - start).days + 620, 1000)
        index_rows = index_history(config.regime_index, end, lookback_days=lookback)
    if eligible_by_date is None:
        eligible_by_date = {day: set(signal_histories) for day in all_dates}
    if is_v5_strategy_mode(config.strategy_mode):
        breadth = breadth_above_ma60(signal_histories, points_by_date, rows_by_date, eligible_by_date, all_dates)
        market_points = build_market_regime(index_rows, breadth, all_dates)
        regime_by_date = to_regime_states(config.regime_index, market_points)
    elif config.strategy_mode == "strategy_v2_modular":
        regime_by_date = modular_regime_by_date(config.regime_index, index_rows, all_dates)
    else:
        regime_by_date = _regime_by_date_from_index(config.regime_index, index_rows, all_dates)
    index_rows_by_date, index_prior_by_date = _index_reference_maps(index_rows, (20, 60))
    sector_heat_by_date: dict[date, dict[str, SectorAlpha]] = {}
    if is_v5_strategy_mode(config.strategy_mode):
        print(f"precompute sector heat dates={len(all_dates)} symbols={len(signal_histories)}", flush=True)
        sector_heat_by_date = build_sector_heat(
            signal_histories,
            sector_map,
            signal_rows_by_date,
            points_by_date,
            row_index_by_date,
            eligible_by_date,
            all_dates,
        )
        print("precompute sector heat complete", flush=True)
    print(f"precompute regime dates={len(regime_by_date)} strategy={config.strategy_mode}", flush=True)
    for verdict_idx, (code, rows) in enumerate(signal_histories.items(), 1):
        if is_v5_strategy_mode(config.strategy_mode):
            candidate_signals_by_date[code] = _analyze_v5_candidate_signals(
                metas[code],
                rows,
                all_dates,
                regime_by_date,
                indicators[code],
                index_rows_by_date,
                index_prior_by_date,
                sector_map,
                sector_heat_by_date,
                eligible_by_date,
            )
            verdicts_by_date[code] = {
                day: signal.verdict for day, signal in candidate_signals_by_date[code].items()
            }
        elif config.strategy_mode == "strategy_v2_modular":
            verdicts_by_date[code] = _analyze_verdicts(
                metas[code],
                rows,
                all_dates,
                config,
                regime_by_date,
                indicators[code],
                index_rows_by_date,
                index_prior_by_date,
                sector_map,
                sector_heat_by_date,
            )
        else:
            verdicts_by_date[code] = _analyze_verdicts(metas[code], rows, all_dates, config)
        if verdict_idx == len(signal_histories) or verdict_idx % 50 == 0:
            print(f"precompute verdicts {verdict_idx}/{len(signal_histories)}", flush=True)
    print(f"precompute sectors dates={len(all_dates)} symbols={len(signal_histories)}", flush=True)
    sector_states_by_date = {
        day: _sector_states_from_points(signal_histories, sector_map, signal_rows_by_date, points_by_date, row_index_by_date, day)
        for day in all_dates
    }
    print("precompute sectors complete", flush=True)
    cash = config.initial_cash
    positions: dict[str, Position] = {}
    pending: list[PendingOrder] = []
    trades: list[Trade] = []
    fills: list[Fill] = []
    equity_curve: list[tuple[date, float]] = []
    risk_state = AccountRiskState(peak_equity=config.initial_cash)
    discipline = {
        "entries": 0,
        "standard_signals": 0,
        "regime_rejected": 0,
        "sector_rejected": 0,
        "industry_unmapped": 0,
        "rejected_four_no": 0,
        "four_no_reasons": {},
        "rejected_caps": 0,
        "limit_blocked": 0,
        "adds": 0,
        "rr_values": [],
        "entry_risk_pct": [],
        "max_symbol_pct": 0.0,
        "max_sector_pct": 0.0,
        "max_exposure": 0.0,
        "regime_exposure": {},
    }
    selection_audit_rows: list[dict[str, str | int | float]] = []
    signal_snapshot_rows: list[dict[str, str | int | float]] = []
    funnel_by_date: dict[date, dict[str, int]] = {}

    previous_close_equity = config.initial_cash
    for day_idx, day in enumerate(all_dates, 1):
        if day_idx <= 3 or day_idx % 20 == 0:
            print(
                f"portfolio start day {day_idx}/{len(all_dates)} {day.isoformat()} "
                f"positions={len(positions)} pending={len(pending)}",
                flush=True,
            )
        begin_trading_session(risk_state, day.isocalendar()[:2], previous_close_equity)
        # Open processing may only use the preceding close.  Today's close is
        # not available until all D+1 orders have been handled.
        # D+1 exits first.
        remaining_pending: list[PendingOrder] = []
        for order in pending:
            row = rows_by_date.get(order.code, {}).get(day)
            prev = _previous_row(execution_histories[order.code], day)
            if row is None or prev is None:
                remaining_pending.append(order)
                continue
            if order.side == "sell":
                pos = positions.get(order.code)
                if pos is None:
                    continue
                if _open_limit_down(row, prev.close, order.code):
                    remaining_pending.append(order)
                    discipline["limit_blocked"] += 1
                    continue
                fill = _fill_price(row, config.fill) * (1 - config.slippage_bps / 10000)
                sell_cost = _costs(order.code, fill, pos.shares, "sell", config)
                gross = (fill - pos.entry_price) * pos.shares
                total_cost = pos.entry_costs + sell_cost
                net = gross - total_cost
                cash += fill * pos.shares - sell_cost
                fills.append(Fill(day, order.code, "sell", fill, pos.shares, sell_cost, order.reason, order.signal_date, pos.stop, pos.target))
                trades.append(
                    Trade(
                        order.code,
                        pos.name,
                        pos.entry_date,
                        day,
                        pos.entry_price,
                        fill,
                        pos.shares,
                        gross,
                        total_cost,
                        net,
                        (day - pos.entry_date).days,
                        pos.entry_reason,
                        order.reason,
                        pos.entry_costs,
                        sell_cost,
                        pos.entry_count,
                    )
                )
                del positions[order.code]
            elif order.side in {"buy", "add"}:
                pos = positions.get(order.code)
                if order.side == "buy" and pos is not None:
                    continue
                if order.side == "add" and pos is None:
                    continue
                if risk_state.halted_next_session or risk_state.stopped_for_drawdown:
                    continue
                if _open_limit_up(row, prev.close, order.code):
                    discipline["limit_blocked"] += 1
                    continue
                fill = _fill_price(row, config.fill) * (1 + config.slippage_bps / 10000)
                industry = industry_of(order.code, sector_map)
                current_equity = cash + _positions_value_before_open(positions, execution_histories, day)
                current_gross = _positions_value_before_open(positions, execution_histories, day)
                sector_values = _sector_values_before_open(positions, execution_histories, day)
                allowed_at_open = allowed_new_position_shares(
                    equity=current_equity,
                    cash=cash,
                    entry=fill,
                    stop=order.stop,
                    current_symbol_value=pos.shares * prev.close if pos else 0.0,
                    current_sector_value=sector_values.get(industry, 0.0),
                    current_gross_value=current_gross,
                    regime=order.signal_regime,
                    config=risk_config,
                    pyramid_stage=(pos.pyramid_stage + 1) if pos else 0,
                    size_multiplier=risk_state.active_week_size_multiplier,
                )
                shares = min(order.shares, allowed_at_open, int((cash // (fill * 100)) * 100))
                if shares <= 0:
                    discipline["rejected_caps"] += 1
                    continue
                buy_cost = _costs(order.code, fill, shares, "buy", config)
                total = fill * shares + buy_cost
                if total > cash:
                    discipline["rejected_caps"] += 1
                    continue
                cash -= total
                fills.append(Fill(day, order.code, order.side, fill, shares, buy_cost, order.reason, order.signal_date, order.stop, order.target))
                if pos is not None:
                    new_total = pos.shares + shares
                    pos.entry_price = (pos.entry_price * pos.shares + fill * shares) / new_total
                    pos.shares = new_total
                    pos.stop = max(pos.stop, order.stop)
                    pos.target = max(pos.target, order.target)
                    pos.highest_high = max(pos.highest_high, row.high)
                    pos.pyramid_stage += 1
                    pos.entry_costs += buy_cost
                    pos.entry_count += 1
                    discipline["adds"] += 1
                else:
                    meta = metas[order.code]
                    positions[order.code] = Position(
                        code=order.code,
                        name=meta.name,
                        industry=industry,
                        shares=shares,
                        entry_price=fill,
                        entry_date=day,
                        entry_reason=order.reason,
                        stop=order.stop,
                        target=order.target,
                        highest_high=row.high,
                        entry_costs=buy_cost,
                    )
                    discipline["entries"] += 1
                discipline["rr_values"].append(order.rr)
                if current_equity > 0:
                    active = positions[order.code]
                    discipline["entry_risk_pct"].append(
                        max(active.entry_price - active.stop, 0) * active.shares / current_equity
                    )
        pending = remaining_pending

        sector_states = sector_states_by_date.get(day, {})
        regime = regime_by_date.get(day, RegimeState("basket", "篮子", day, "down", False, "missing"))
        equity = cash + _positions_value(positions, rows_by_date, day)
        current_gross = _positions_value(positions, rows_by_date, day)
        sector_values = _sector_values(positions, rows_by_date, day)
        planned_cash = cash
        planned_gross = current_gross
        planned_sector_values = dict(sector_values)
        discipline["max_exposure"] = max(discipline["max_exposure"], current_gross / equity if equity else 0.0)
        discipline["regime_exposure"].setdefault(regime.regime, []).append(current_gross / equity if equity else 0.0)
        for pos in positions.values():
            row = rows_by_date.get(pos.code, {}).get(day)
            point = points_by_date.get(pos.code, {}).get(day)
            if row is None or point is None:
                continue
            pos.highest_high = max(pos.highest_high, row.high)
            pos.stop = max(pos.stop, trailing_stop(pos.entry_price, row.close, point.ma10, point.atr14, risk_config, current_stop=pos.stop))
            pos.holding_bars += 1
            exit_reason = _exit_by_discipline(pos, row, signal_histories[pos.code], day, point, risk_config)
            if exit_reason:
                pending.append(PendingOrder(pos.code, "sell", exit_reason, signal_date=day, signal_regime=regime.regime))
                continue
            if row.close >= pos.target:
                pending.append(PendingOrder(pos.code, "sell", "target_close_confirmed", signal_date=day, signal_regime=regime.regime))
                continue
            signal_row = _row_on_date(signal_histories[pos.code], day)
            if signal_row and _add_signal(pos, signal_row, signal_histories[pos.code], day, point) and not any(
                order.code == pos.code and order.side in {"add", "sell"} for order in pending
            ):
                add_stop = max(pos.stop, _to_execution_price(_initial_stop(signal_row, point, risk_config), signal_row, row))
                add_target = _to_execution_price(
                    _target_price_with_point(signal_histories[pos.code], day, signal_row.close, risk_config, point),
                    signal_row,
                    row,
                )
                add_decision = apply_four_no_entry("入选", row, point, pos.code, row.close, add_stop, add_target, entry_config)
                if add_decision.ok:
                    shares = allowed_new_position_shares(
                        equity=equity,
                        cash=planned_cash,
                        entry=row.close,
                        stop=add_stop,
                        current_symbol_value=_position_value(pos, rows_by_date, day),
                        current_sector_value=planned_sector_values.get(pos.industry, 0.0),
                        current_gross_value=planned_gross,
                        regime=regime.regime,
                        config=risk_config,
                        pyramid_stage=pos.pyramid_stage + 1,
                        size_multiplier=risk_state.active_week_size_multiplier,
                    )
                    if shares > 0:
                        pending.append(PendingOrder(pos.code, "add", "pyramid_add", shares=shares, stop=add_stop, target=add_target, rr=add_decision.rr, signal_date=day, signal_regime=regime.regime))
                        planned_value = row.close * shares
                        planned_cash -= planned_value
                        planned_gross += planned_value
                        planned_sector_values[pos.industry] = planned_sector_values.get(pos.industry, 0.0) + planned_value

        if is_v5_strategy_mode(config.strategy_mode):
            funnel_by_date[day] = _candidate_funnel_for_day(
                day,
                signal_histories,
                candidate_signals_by_date,
                eligible_by_date.get(day, set(signal_histories)),
            )
        entry_items = _daily_entry_items(
            day,
            signal_histories,
            candidate_signals_by_date,
            positions,
            config.strategy_mode,
        )
        for rank, code, rows, candidate_signal in entry_items:
            if code in positions or risk_state.halted_next_session or risk_state.stopped_for_drawdown:
                if candidate_signal is not None:
                    selection_audit_rows.append(_candidate_audit_row(candidate_signal, rank, False, "risk_or_position", None))
                continue
            if max_positions is not None and len(positions) + sum(1 for order in pending if order.side == "buy") >= max_positions:
                if is_ranked_v5_strategy_mode(config.strategy_mode):
                    if candidate_signal is not None:
                        selection_audit_rows.append(_candidate_audit_row(candidate_signal, rank, False, "capacity", None))
                        funnel_by_date.setdefault(day, {})["capacity_rejected"] = funnel_by_date.setdefault(day, {}).get("capacity_rejected", 0) + 1
                    continue
                break
            if code not in eligible_by_date.get(day, set(signal_histories)):
                continue
            row = rows_by_date[code].get(day)
            point = points_by_date[code].get(day)
            if row is None or point is None:
                continue
            verdict = verdicts_by_date.get(code, {}).get(day, "不入选")
            if candidate_signal is not None:
                verdict = candidate_signal.verdict
            industry = industry_of(code, sector_map)
            if industry == "未知行业" and config.require_industry:
                discipline["industry_unmapped"] += 1
                if candidate_signal is not None:
                    selection_audit_rows.append(_candidate_audit_row(candidate_signal, rank, False, "industry_unmapped", None))
                continue
            sector_state = sector_states.get(industry, SectorState(industry, day, "unknown", False, 0.0, "missing"))
            sector_ok = sector_state.sector_ok if config.use_sector else True
            if not is_entry_verdict(verdict):
                continue
            discipline["standard_signals"] += 1
            if not regime.regime_ok:
                discipline["regime_rejected"] += 1
                if candidate_signal is not None:
                    selection_audit_rows.append(_candidate_audit_row(candidate_signal, rank, False, "regime", None))
                continue
            if not sector_ok:
                discipline["sector_rejected"] += 1
                if candidate_signal is not None:
                    selection_audit_rows.append(_candidate_audit_row(candidate_signal, rank, False, "sector", None))
                continue
            signal_row = _row_on_date(rows, day)
            if signal_row is None:
                if candidate_signal is not None:
                    selection_audit_rows.append(_candidate_audit_row(candidate_signal, rank, False, "missing_signal_row", None))
                continue
            stop = _to_execution_price(_initial_stop(signal_row, point, risk_config), signal_row, row)
            target = _to_execution_price(
                _target_price_with_point(rows, day, signal_row.close, risk_config, point), signal_row, row
            )
            decision = apply_four_no_entry(verdict, row, point, code, row.close, stop, target, entry_config)
            if not decision.ok:
                discipline["rejected_four_no"] += 1
                if candidate_signal is not None:
                    selection_audit_rows.append(
                        _candidate_audit_row(candidate_signal, rank, False, "four_no:" + "|".join(decision.reasons), None)
                    )
                for reason in decision.reasons:
                    key = reason.split("：", 1)[0]
                    counts = discipline["four_no_reasons"]
                    counts[key] = counts.get(key, 0) + 1
                continue
            shares = allowed_new_position_shares(
                equity=equity,
                cash=planned_cash,
                entry=row.close,
                stop=stop,
                current_symbol_value=0.0,
                current_sector_value=planned_sector_values.get(industry, 0.0),
                current_gross_value=planned_gross,
                regime=regime.regime,
                config=risk_config,
                pyramid_stage=0,
                size_multiplier=risk_state.active_week_size_multiplier,
            )
            if shares <= 0:
                discipline["rejected_caps"] += 1
                if candidate_signal is not None:
                    selection_audit_rows.append(_candidate_audit_row(candidate_signal, rank, False, "position_cap", None))
                continue
            pending.append(
                PendingOrder(
                    code,
                    "buy",
                    f"{config.strategy_mode}_entry",
                    shares=shares,
                    stop=stop,
                    target=target,
                    rr=decision.rr,
                    signal_date=day,
                    signal_regime=regime.regime,
                )
            )
            planned_value = row.close * shares
            if candidate_signal is not None:
                order_for_audit = pending[-1]
                selection_audit_rows.append(_candidate_audit_row(candidate_signal, rank, True, "", order_for_audit))
                signal_snapshot_rows.append(_candidate_audit_row(candidate_signal, rank, True, "", order_for_audit))
                funnel_by_date.setdefault(day, {})["selected"] = funnel_by_date.setdefault(day, {}).get("selected", 0) + 1
            planned_cash -= planned_value
            planned_gross += planned_value
            planned_sector_values[industry] = planned_sector_values.get(industry, 0.0) + planned_value

        equity = cash + _positions_value(positions, rows_by_date, day)
        if equity > 0:
            for pos in positions.values():
                value = _position_value(pos, rows_by_date, day)
                discipline["max_symbol_pct"] = max(discipline["max_symbol_pct"], value / equity)
            for value in _sector_values(positions, rows_by_date, day).values():
                discipline["max_sector_pct"] = max(discipline["max_sector_pct"], value / equity)
        equity_curve.append((day, equity))
        risk_state = update_account_risk(risk_state, equity, previous_close_equity, day.isocalendar()[:2], risk_config)
        previous_close_equity = equity
        if day_idx == len(all_dates) or day_idx % 20 == 0:
            print(
                f"portfolio days {day_idx}/{len(all_dates)} positions={len(positions)} "
                f"pending={len(pending)} trades={len(trades)}",
                flush=True,
            )

    # Liquidate at final close for evidence.
    if all_dates:
        last_day = all_dates[-1]
        for code, pos in list(positions.items()):
            row = rows_by_date[code].get(last_day)
            if row is None:
                continue
            fill = row.close * (1 - config.slippage_bps / 10000)
            sell_cost = _costs(code, fill, pos.shares, "sell", config)
            gross = (fill - pos.entry_price) * pos.shares
            total_cost = pos.entry_costs + sell_cost
            fills.append(Fill(last_day, code, "sell", fill, pos.shares, sell_cost, "end_of_backtest", last_day, pos.stop, pos.target))
            cash += fill * pos.shares - sell_cost
            trades.append(
                Trade(
                    code,
                    pos.name,
                    pos.entry_date,
                    last_day,
                    pos.entry_price,
                    fill,
                    pos.shares,
                    gross,
                    total_cost,
                    gross - total_cost,
                    (last_day - pos.entry_date).days,
                    pos.entry_reason,
                    "end_of_backtest",
                    pos.entry_costs,
                    sell_cost,
                    pos.entry_count,
                )
            )
            del positions[code]
        if equity_curve:
            equity_curve[-1] = (last_day, cash)

    stem = "basket" if len(symbols) > 1 else normalize_code(symbols[0])
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_path = output_dir / f"trades_{stem}_{start.isoformat()}_{end.isoformat()}.csv"
    fills_path = output_dir / f"fills_{stem}_{start.isoformat()}_{end.isoformat()}.csv"
    metrics_path = output_dir / f"metrics_{stem}_{start.isoformat()}_{end.isoformat()}.md"
    equity_path = output_dir / f"equity_curve_{stem}_{start.isoformat()}_{end.isoformat()}.csv"
    yearly_path = output_dir / f"yearly_report_{stem}_{start.isoformat()}_{end.isoformat()}.csv"
    drawdown_path = output_dir / f"drawdown_report_{stem}_{start.isoformat()}_{end.isoformat()}.csv"
    trade_records_path = output_dir / f"trade_records_{stem}_{start.isoformat()}_{end.isoformat()}.csv"
    sector_heat_path = output_dir / f"sector_heat_{stem}_{start.isoformat()}_{end.isoformat()}.csv"
    candidate_funnel_daily_path = output_dir / "candidate_funnel_daily.csv"
    candidate_funnel_summary_path = output_dir / "candidate_funnel_summary.md"
    candidate_selection_audit_path = output_dir / "candidate_selection_audit.csv"
    signal_snapshots_path = output_dir / "signal_snapshots.csv"
    trade_attribution_path = output_dir / "trade_attribution.csv"
    alpha_decile_path = output_dir / "alpha_decile_analysis.csv"
    alpha_ic_path = output_dir / "alpha_ic_report.md"
    sector_data_audit_path = output_dir / "sector_data_audit.md"
    controlled_comparison_csv_path = output_dir / "controlled_comparison_2026.csv"
    controlled_comparison_md_path = output_dir / "controlled_comparison_2026.md"
    _write_trades(trades_path, trades)
    _write_trades(trade_records_path, trades)
    _write_fills(fills_path, fills)
    metrics = metrics_from_trades(trades, equity_curve, config.initial_cash, 0, split_date=config.split_date)
    annual_stats = annual_stats_from_trades(trades, equity_curve, config.initial_cash)
    _write_equity_curve(equity_path, equity_curve)
    _write_yearly_report(yearly_path, annual_stats)
    _write_drawdown_report(drawdown_path, equity_curve)
    if sector_heat_by_date:
        _write_sector_heat_report(sector_heat_path, sector_heat_by_date)
    if is_v5_strategy_mode(config.strategy_mode):
        _write_candidate_funnel_daily(candidate_funnel_daily_path, funnel_by_date)
        _write_candidate_funnel_summary(candidate_funnel_summary_path, funnel_by_date)
        _write_dict_rows(candidate_selection_audit_path, selection_audit_rows)
        _write_dict_rows(signal_snapshots_path, signal_snapshot_rows)
        _write_trade_attribution(trade_attribution_path, trades, signal_snapshot_rows)
        _write_alpha_decile_analysis(alpha_decile_path, selection_audit_rows)
        _write_alpha_ic_report(alpha_ic_path, selection_audit_rows, trades)
        _write_sector_data_audit(sector_data_audit_path, sector_map, sector_heat_by_date)
        _write_controlled_comparison_note(controlled_comparison_csv_path, controlled_comparison_md_path, config)
    _add_discipline_metrics(metrics, discipline)
    metrics["fill_count"] = float(len(fills))
    _write_metrics(
        metrics_path,
        metrics,
        config,
        discipline=discipline,
        risk_config=risk_config,
        entry_config=entry_config,
        annual_stats=annual_stats,
    )
    return trades_path, metrics_path, metrics


def _legacy_backtest_symbols(
    symbols: list[str],
    start: date,
    end: date,
    output_dir: Path,
    config: BacktestConfig | None = None,
) -> tuple[Path, Path, dict[str, float]]:
    config = config or BacktestConfig()
    all_trades: list[Trade] = []
    for idx, symbol in enumerate(symbols, 1):
        lookback = max((end - start).days + 520, 800)
        code = normalize_code(symbol)
        try:
            meta, rows = tencent_history(code, end, lookback_days=lookback, adjust="hfq")
        except DataError:
            meta, rows = eastmoney_history(code, end, lookback_days=lookback, adjust=2)
        trades, _metrics = backtest_code(meta, rows, start, end, config=config)
        all_trades.extend(trades)
        print(f"backtest progress: {idx}/{len(symbols)} {code} trades={len(trades)}", flush=True)
    stem = "basket" if len(symbols) > 1 else normalize_code(symbols[0])
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_path = output_dir / f"trades_{stem}_{start.isoformat()}_{end.isoformat()}.csv"
    metrics_path = output_dir / f"metrics_{stem}_{start.isoformat()}_{end.isoformat()}.md"
    _write_trades(trades_path, all_trades)
    metrics = metrics_from_trades(all_trades, [], config.initial_cash * max(1, len(symbols)), 0, split_date=config.split_date)
    _write_metrics(metrics_path, metrics, config)
    return trades_path, metrics_path, metrics


def metrics_from_trades(
    trades: list[Trade],
    equity_curve: list[tuple[date, float]] | None = None,
    initial_cash: float = 100000.0,
    exposure_days: int = 0,
    split_date: date | None = None,
) -> dict[str, float]:
    if not equity_curve and trades:
        equity = initial_cash
        equity_curve = []
        for trade in sorted(trades, key=lambda item: item.exit_date):
            equity += trade.net_pnl
            equity_curve.append((trade.exit_date, equity))
    wins = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
    losses = [-trade.net_pnl for trade in trades if trade.net_pnl < 0]
    total_net = sum(trade.net_pnl for trade in trades)
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_win = mean(wins) if wins else 0.0
    avg_loss = mean(losses) if losses else 0.0
    ev = win_rate * avg_win - (1 - win_rate) * avg_loss if trades else 0.0
    profit_factor = sum(wins) / sum(losses) if losses and sum(losses) else (math.inf if wins else 0.0)
    payoff = avg_win / avg_loss if avg_loss else (math.inf if avg_win else 0.0)
    max_dd = _max_drawdown(equity_curve or [(date.today(), initial_cash + total_net)])
    returns = _daily_returns(equity_curve or [])
    sharpe = (mean(returns) / pstdev(returns) * math.sqrt(252)) if len(returns) > 1 and pstdev(returns) > 0 else 0.0
    years = _years(equity_curve)
    total_return = total_net / initial_cash if initial_cash else 0.0
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 and total_return > -1 else total_return
    calmar = cagr / abs(max_dd) if max_dd < 0 else (math.inf if cagr > 0 else 0.0)
    metrics = {
        "total_return": total_return,
        "cagr": cagr,
        "expectancy": ev,
        "trimmed_expectancy_5pct": _trimmed_mean([trade.net_pnl for trade in trades], trim_pct=0.05),
        "median_trade_pnl": median([trade.net_pnl for trade in trades]) if trades else 0.0,
        "win_rate": win_rate,
        "payoff_ratio": payoff,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "calmar": calmar,
        "trade_count": float(len(trades)),
        "avg_holding_days": mean([trade.holding_days for trade in trades]) if trades else 0.0,
        "exposure": exposure_days / len(equity_curve) if equity_curve else 0.0,
    }
    lo, hi = _bootstrap_mean_ci([trade.net_pnl for trade in trades])
    metrics["expectancy_ci95_low"] = lo
    metrics["expectancy_ci95_high"] = hi
    independent_clusters = _independent_trade_clusters(trades)
    metrics["trade_count_sufficient"] = 1.0 if len(trades) >= 100 else 0.0
    metrics["independent_cluster_count"] = float(independent_clusters)
    metrics["expectancy_ci95_positive"] = 1.0 if lo > 0 else 0.0
    metrics["statistically_supported"] = (
        1.0 if len(trades) >= 100 and independent_clusters >= 26 and lo > 0 and years >= 3 else 0.0
    )
    metrics["sample_sufficient"] = metrics["statistically_supported"]
    metrics["validation_label"] = 1.0 if split_date and years >= 3 else 0.0
    if split_date:
        is_trades = [trade for trade in trades if trade.entry_date < split_date]
        oos_trades = [trade for trade in trades if trade.entry_date >= split_date]
        metrics["is_trade_count"] = float(len(is_trades))
        metrics["oos_trade_count"] = float(len(oos_trades))
        metrics["is_expectancy"] = metrics_from_trades(is_trades, [], initial_cash)["expectancy"]
        metrics["oos_expectancy"] = metrics_from_trades(oos_trades, [], initial_cash)["expectancy"]
    return metrics


def _independent_trade_clusters(trades: list[Trade]) -> int:
    return len({trade.entry_date.isocalendar()[:2] for trade in trades})


def annual_stats_from_trades(
    trades: list[Trade], equity_curve: list[tuple[date, float]], initial_cash: float
) -> list[dict[str, float | int]]:
    """Return calendar-year results from the daily equity curve."""
    if not equity_curve:
        return []
    by_year: dict[int, list[tuple[date, float]]] = {}
    for day, equity in equity_curve:
        by_year.setdefault(day.year, []).append((day, equity))
    stats: list[dict[str, float | int]] = []
    previous_equity = initial_cash
    for year in sorted(by_year):
        points = by_year[year]
        peak = previous_equity
        max_dd = 0.0
        for _day, equity in points:
            peak = max(peak, equity)
            if peak > 0:
                max_dd = min(max_dd, equity / peak - 1)
        end_equity = points[-1][1]
        year_trades = [trade for trade in trades if trade.exit_date.year == year]
        wins = sum(1 for trade in year_trades if trade.net_pnl > 0)
        stats.append(
            {
                "year": year,
                "trades": len(year_trades),
                "return": end_equity / previous_equity - 1 if previous_equity else 0.0,
                "max_drawdown": max_dd,
                "win_rate": wins / len(year_trades) if year_trades else 0.0,
            }
        )
        previous_equity = end_equity
    return stats


def _entry_signal(report) -> bool:
    return report.verdict == "入选" or report.verdict == "观察（轻仓试探）"


def _exit_signal(report) -> bool:
    return report.verdict == "回避/减仓观察" or any(item.status == "WARN" and item.score <= -2 for item in report.exit_rules)


def _fill_price(row: KLine, fill: str) -> float:
    if fill == "next_open":
        return row.open
    if fill == "next_vwap_proxy":
        return (row.open + row.high + row.low + row.close) / 4
    raise ValueError("fill must be next_open or next_vwap_proxy")


def _costs(code: str, price: float, shares: int, side: str, config: BacktestConfig) -> float:
    notional = price * shares
    commission = max(notional * config.commission_rate, config.min_commission)
    stamp = notional * config.stamp_tax_rate if side == "sell" else 0.0
    transfer = notional * config.transfer_rate if code.startswith("6") else 0.0
    return commission + stamp + transfer


def _write_trades(path: Path, trades: list[Trade]) -> None:
    fields = list(Trade.__dataclass_fields__.keys())
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for trade in trades:
            writer.writerow({field: getattr(trade, field) for field in fields})


def _write_fills(path: Path, fills: list[Fill]) -> None:
    fields = list(Fill.__dataclass_fields__.keys())
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for fill in fills:
            writer.writerow({field: getattr(fill, field) for field in fields})


def _write_equity_curve(path: Path, equity_curve: list[tuple[date, float]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "equity"])
        writer.writeheader()
        for day, equity in equity_curve:
            writer.writerow({"date": day.isoformat(), "equity": f"{equity:.6f}"})


def _write_yearly_report(path: Path, annual_stats: list[dict[str, float | int]]) -> None:
    fields = ["year", "trades", "return", "max_drawdown", "win_rate"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for item in annual_stats:
            writer.writerow({field: item.get(field, 0) for field in fields})


def _write_drawdown_report(path: Path, equity_curve: list[tuple[date, float]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "equity", "peak", "drawdown"])
        writer.writeheader()
        peak = 0.0
        for day, equity in equity_curve:
            peak = max(peak, equity)
            drawdown = equity / peak - 1 if peak else 0.0
            writer.writerow(
                {
                    "date": day.isoformat(),
                    "equity": f"{equity:.6f}",
                    "peak": f"{peak:.6f}",
                    "drawdown": f"{drawdown:.6f}",
                }
            )


def _daily_entry_items(
    day: date,
    signal_histories: dict[str, list[KLine]],
    candidate_signals_by_date: dict[str, dict[date, CandidateSignal]],
    positions: dict[str, Position],
    strategy_mode: str,
) -> list[tuple[int, str, list[KLine], CandidateSignal | None]]:
    if is_ranked_v5_strategy_mode(strategy_mode):
        candidates = [
            signal
            for code, signals in candidate_signals_by_date.items()
            if code not in positions
            for signal in [signals.get(day)]
            if signal is not None and signal.hard_pass
        ]
        return [
            (rank, signal.code, signal_histories[signal.code], signal)
            for rank, signal in enumerate(rank_candidate_signals(candidates), 1)
        ]
    return [
        (rank, code, rows, candidate_signals_by_date.get(code, {}).get(day))
        for rank, (code, rows) in enumerate(signal_histories.items(), 1)
    ]


def _candidate_funnel_for_day(
    day: date,
    signal_histories: dict[str, list[KLine]],
    candidate_signals_by_date: dict[str, dict[date, CandidateSignal]],
    eligible: set[str],
) -> dict[str, int]:
    signals = [signals_by_day[day] for signals_by_day in candidate_signals_by_date.values() if day in signals_by_day]
    hard_pass = [signal for signal in signals if signal.hard_pass]
    return {
        "eligible": len(eligible),
        "evaluated": len(signals),
        "hard_pass": len(hard_pass),
        "selected": 0,
        "capacity_rejected": 0,
        "all_symbols": len(signal_histories),
    }


def _candidate_audit_row(
    signal: CandidateSignal,
    rank: int,
    selected: bool,
    rejection_reason: str,
    order: PendingOrder | None,
) -> dict[str, str | int | float]:
    return {
        "date": signal.date.isoformat(),
        "code": signal.code,
        "name": signal.name,
        "industry": signal.industry,
        "market_regime": signal.market_regime,
        "eligible": int(signal.eligible),
        "market_ok": int(signal.market_ok),
        "alpha_total": f"{signal.alpha_total:.6f}",
        "sector_bonus": f"{signal.sector_bonus:.6f}",
        "ranking_score": f"{signal.ranking_score:.6f}",
        "trend_score": f"{signal.trend_score:.6f}",
        "relative_strength_score": f"{signal.relative_strength_score:.6f}",
        "volume_quality_score": f"{signal.volume_quality_score:.6f}",
        "risk_score": f"{signal.risk_score:.6f}",
        "sector_heat_score": f"{signal.sector_heat_score:.6f}",
        "entry_score": f"{signal.entry_score:.6f}",
        "hard_pass": int(signal.hard_pass),
        "rank": rank,
        "selected": int(selected),
        "rejection_reason": rejection_reason,
        "shares": order.shares if order else 0,
        "stop": f"{order.stop:.6f}" if order else "",
        "target": f"{order.target:.6f}" if order else "",
        "rr": f"{order.rr:.6f}" if order else "",
        "reasons": "|".join(signal.reasons),
    }


def _plan_new_entry_order(
    *,
    code: str,
    rows: list[KLine],
    day: date,
    row: KLine,
    point: IndicatorPoint,
    industry: str,
    verdict: str,
    equity: float,
    planned_cash: float,
    planned_gross: float,
    planned_sector_values: dict[str, float],
    regime: RegimeState,
    risk_state: AccountRiskState,
    config: BacktestConfig,
    risk_config: RiskConfig,
    entry_config: EntryFilterConfig,
) -> tuple[PendingOrder | None, str, float]:
    signal_row = _row_on_date(rows, day)
    if signal_row is None:
        return None, "missing_signal_row", 0.0
    stop = _to_execution_price(_initial_stop(signal_row, point, risk_config), signal_row, row)
    target = _to_execution_price(
        _target_price_with_point(rows, day, signal_row.close, risk_config, point), signal_row, row
    )
    decision = apply_four_no_entry(verdict, row, point, code, row.close, stop, target, entry_config)
    if not decision.ok:
        return None, "four_no:" + "|".join(decision.reasons), decision.rr
    shares = allowed_new_position_shares(
        equity=equity,
        cash=planned_cash,
        entry=row.close,
        stop=stop,
        current_symbol_value=0.0,
        current_sector_value=planned_sector_values.get(industry, 0.0),
        current_gross_value=planned_gross,
        regime=regime.regime,
        config=risk_config,
        pyramid_stage=0,
        size_multiplier=risk_state.active_week_size_multiplier,
    )
    if shares <= 0:
        return None, "position_cap", decision.rr
    return (
        PendingOrder(
            code,
            "buy",
            f"{config.strategy_mode}_entry",
            shares=shares,
            stop=stop,
            target=target,
            rr=decision.rr,
            signal_date=day,
            signal_regime=regime.regime,
        ),
        "selected",
        row.close * shares,
    )


def _write_sector_heat_report(path: Path, sector_heat_by_date: dict[date, dict[str, SectorAlpha]]) -> None:
    fields = [
        "date",
        "sector",
        "heat_score",
        "member_count",
        "breadth_score",
        "relative_strength_score",
        "amount_score",
        "momentum_score",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for day in sorted(sector_heat_by_date):
            sectors = sorted(
                sector_heat_by_date[day].values(),
                key=lambda item: (item.heat_score, item.member_count),
                reverse=True,
            )
            for item in sectors:
                writer.writerow(
                    {
                        "date": day.isoformat(),
                        "sector": item.sector,
                        "heat_score": f"{item.heat_score:.6f}",
                        "member_count": item.member_count,
                        "breadth_score": f"{item.breadth_score:.6f}",
                        "relative_strength_score": f"{item.relative_strength_score:.6f}",
                        "amount_score": f"{item.amount_score:.6f}",
                        "momentum_score": f"{item.momentum_score:.6f}",
                    }
                )


def _write_dict_rows(path: Path, rows: list[dict[str, str | int | float]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_candidate_funnel_daily(path: Path, funnel_by_date: dict[date, dict[str, int]]) -> None:
    fields = ["date", "all_symbols", "eligible", "evaluated", "hard_pass", "selected", "capacity_rejected"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for day in sorted(funnel_by_date):
            row = funnel_by_date[day]
            writer.writerow({"date": day.isoformat(), **{field: row.get(field, 0) for field in fields if field != "date"}})


def _write_candidate_funnel_summary(path: Path, funnel_by_date: dict[date, dict[str, int]]) -> None:
    totals: dict[str, int] = {}
    for row in funnel_by_date.values():
        for key, value in row.items():
            totals[key] = totals.get(key, 0) + int(value)
    lines = [
        "# Candidate Funnel Summary",
        "",
        "| Stage | Count |",
        "| --- | ---: |",
    ]
    for key in ("all_symbols", "eligible", "evaluated", "hard_pass", "selected", "capacity_rejected"):
        lines.append(f"| {key} | {totals.get(key, 0)} |")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_trade_attribution(path: Path, trades: list[Trade], signal_rows: list[dict[str, str | int | float]]) -> None:
    by_code: dict[str, list[dict[str, str | int | float]]] = {}
    for row in signal_rows:
        by_code.setdefault(str(row["code"]), []).append(row)
    output: list[dict[str, str | int | float]] = []
    for trade in trades:
        signals = by_code.get(trade.code, [])
        eligible_signals = [
            row for row in signals if str(row.get("date", "")) <= trade.entry_date.isoformat()
        ]
        chosen = max(eligible_signals, key=lambda row: str(row.get("date", ""))) if eligible_signals else {}
        output.append(
            {
                "code": trade.code,
                "entry_date": trade.entry_date.isoformat(),
                "exit_date": trade.exit_date.isoformat(),
                "net_pnl": f"{trade.net_pnl:.6f}",
                "exit_reason": trade.exit_reason,
                "signal_date": chosen.get("date", ""),
                "rank": chosen.get("rank", ""),
                "ranking_score": chosen.get("ranking_score", ""),
                "entry_score": chosen.get("entry_score", ""),
                "relative_strength_score": chosen.get("relative_strength_score", ""),
                "reasons": chosen.get("reasons", ""),
            }
        )
    _write_dict_rows(path, output)


def _write_alpha_decile_analysis(path: Path, audit_rows: list[dict[str, str | int | float]]) -> None:
    hard_pass = [row for row in audit_rows if int(row.get("hard_pass", 0)) == 1]
    ranked = sorted(hard_pass, key=lambda row: float(row.get("ranking_score", 0)), reverse=True)
    rows: list[dict[str, str | int | float]] = []
    if ranked:
        bucket_size = max(1, math.ceil(len(ranked) / 10))
        for decile in range(10):
            bucket = ranked[decile * bucket_size : (decile + 1) * bucket_size]
            if not bucket:
                continue
            rows.append(
                {
                    "decile": decile + 1,
                    "candidate_count": len(bucket),
                    "selected_count": sum(int(item.get("selected", 0)) for item in bucket),
                    "avg_ranking_score": f"{mean(float(item.get('ranking_score', 0)) for item in bucket):.6f}",
                    "avg_entry_score": f"{mean(float(item.get('entry_score', 0)) for item in bucket):.6f}",
                }
            )
    _write_dict_rows(path, rows)


def _write_alpha_ic_report(path: Path, audit_rows: list[dict[str, str | int | float]], trades: list[Trade]) -> None:
    selected = [row for row in audit_rows if int(row.get("selected", 0)) == 1]
    lines = [
        "# Alpha IC Report",
        "",
        "- Scope: selected signal snapshots matched to realized trades when available.",
        "- Interpretation: retrospective research evidence, not untouched out-of-sample proof.",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| selected_signals | {len(selected)} |",
        f"| realized_trades | {len(trades)} |",
        f"| statistically_supported | 0 |",
        "",
        "A full cross-sectional IC requires next-period returns for every candidate, including non-selected names. "
        "This file records the limitation explicitly so the report cannot overstate alpha evidence.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_sector_data_audit(
    path: Path,
    sector_map: dict[str, str],
    sector_heat_by_date: dict[date, dict[str, SectorAlpha]],
) -> None:
    mapped = sum(1 for value in sector_map.values() if value)
    unmapped = len(sector_map) - mapped
    sector_days = sum(len(items) for items in sector_heat_by_date.values())
    lines = [
        "# Sector Data Audit",
        "",
        "| Item | Value |",
        "| --- | ---: |",
        f"| symbols_with_sector_label | {mapped} |",
        f"| symbols_without_sector_label | {unmapped} |",
        f"| daily_sector_heat_rows | {sector_days} |",
        "",
        "Sector heat is used only as a capped prior. Missing sector data never creates an artificial hot-sector boost.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_controlled_comparison_note(csv_path: Path, md_path: Path, config: BacktestConfig) -> None:
    rows = [
        {
            "variant": config.strategy_mode,
            "selection_policy": "ranked" if is_ranked_v5_strategy_mode(config.strategy_mode) else "first_fit_frozen",
            "parameter_tuning": "none",
            "status": "current_run",
        }
    ]
    _write_dict_rows(csv_path, rows)
    md_path.write_text(
        "\n".join(
            [
                "# Controlled Comparison",
                "",
                "This run records the current variant only. Frozen first-fit and ranked variants must be executed on the same data snapshot before comparing returns.",
                "",
                f"- Current strategy: `{config.strategy_mode}`",
                "- Parameter tuning: `none`",
            ]
        ),
        encoding="utf-8",
    )


def _write_metrics(
    path: Path,
    metrics: dict[str, float],
    config: BacktestConfig,
    discipline: dict | None = None,
    risk_config: RiskConfig | None = None,
    entry_config: EntryFilterConfig | None = None,
    annual_stats: list[dict[str, float | int]] | None = None,
) -> None:
    lines = [
        "# Backtest Metrics",
        "",
        f"- fill: `{config.fill}`",
        f"- signal adjustment: `{config.signal_adjust}`; execution adjustment: `none`",
        f"- commission: `{config.commission_rate}`; min commission: `{config.min_commission}`",
        f"- stamp tax sell only: `{config.stamp_tax_rate}`; transfer SH only: `{config.transfer_rate}`",
        f"- slippage bps: `{config.slippage_bps}`",
        f"- strategy: `{config.strategy_mode}`",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key, value in metrics.items():
        lines.append(f"| {key} | {value:.6f} |")
    if discipline and risk_config and entry_config:
        rr_values = discipline.get("rr_values", [])
        min_rr = min(rr_values) if rr_values else 0.0
        avg_rr = mean(rr_values) if rr_values else 0.0
        lines.extend(
            [
                "",
                "## 纪律合规",
                "",
                f"- 单股上限：默认 {risk_config.max_position_pct:.0%}，实测最大 {discipline.get('max_symbol_pct', 0):.2%}。",
                f"- 单行业上限：默认 {risk_config.max_sector_pct:.0%}，实测最大 {discipline.get('max_sector_pct', 0):.2%}。",
                f"- 总仓位上限：trend_up/range/down = 80%/50%/30%，实测最大 {discipline.get('max_exposure', 0):.2%}。",
                f"- 四不做：拒绝 {discipline.get('rejected_four_no', 0)} 次；入场档位 `{entry_config.entry_tier}`；最小 R:R {entry_config.min_rr:.2f}。",
                f"- 四不做原因：{_format_reason_counts(discipline.get('four_no_reasons', {}))}。",
                f"- 信号漏斗：标准520 {discipline.get('standard_signals', 0)}，市场过滤拒绝 {discipline.get('regime_rejected', 0)}，行业过滤拒绝 {discipline.get('sector_rejected', 0)}，四不做拒绝 {discipline.get('rejected_four_no', 0)}，实际建仓 {discipline.get('entries', 0)}。",
                f"- 数据门控：行业映射缺失拒绝 {discipline.get('industry_unmapped', 0)} 次；默认不把未知行业合并为同一风险桶。",
                f"- 盈亏比：实际入场最小 R:R {min_rr:.2f}，平均 R:R {avg_rr:.2f}，对应盈亏平衡胜率 {breakeven_win_rate(avg_rr):.2%}。",
                f"- 实测风险：平均 {metrics.get('avg_entry_risk_pct', 0):.2%}，最大 {metrics.get('max_entry_risk_pct', 0):.2%}，目标 {risk_config.risk_per_trade:.2%}。",
                f"- 金字塔：加仓 {discipline.get('adds', 0)} 次。",
                f"- 熔断/仓位：仓位被 cap/整手约束拒绝 {discipline.get('rejected_caps', 0)} 次，涨跌停阻断 {discipline.get('limit_blocked', 0)} 次。",
                f"- 样本判定：交易笔数 {metrics.get('trade_count', 0):.0f}，独立周簇 {metrics.get('independent_cluster_count', 0):.0f}，CI95 下沿 {metrics.get('expectancy_ci95_low', 0):.2f}，统计支持={'是' if metrics.get('statistically_supported', 0) else '否'}。",
                "",
                "| Regime | Average Exposure |",
                "| --- | ---: |",
            ]
        )
        for regime, values in sorted(discipline.get("regime_exposure", {}).items()):
            lines.append(f"| {regime} | {(mean(values) if values else 0):.6f} |")
    if annual_stats is not None:
        lines.extend(
            [
                "",
                "## Annual Statistics",
                "",
                "| Year | Trades | Return | Max Drawdown | Win Rate |",
                "| ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for item in annual_stats:
            lines.append(
                f"| {item['year']} | {item['trades']} | {item['return']:.6f} | "
                f"{item['max_drawdown']:.6f} | {item['win_rate']:.6f} |"
            )
    path.write_text("\n".join(lines), encoding="utf-8")


def _add_discipline_metrics(metrics: dict[str, float], discipline: dict) -> None:
    regime_values = [value for values in discipline.get("regime_exposure", {}).values() for value in values]
    avg_exposure = mean(regime_values) if regime_values else 0.0
    metrics["exposure"] = avg_exposure
    metrics["avg_exposure"] = avg_exposure
    metrics["max_symbol_pct"] = discipline.get("max_symbol_pct", 0.0)
    metrics["max_sector_pct"] = discipline.get("max_sector_pct", 0.0)
    metrics["max_exposure"] = discipline.get("max_exposure", 0.0)
    metrics["discipline_entries"] = float(discipline.get("entries", 0))
    metrics["standard_signal_count"] = float(discipline.get("standard_signals", 0))
    metrics["regime_rejected_count"] = float(discipline.get("regime_rejected", 0))
    metrics["sector_rejected_count"] = float(discipline.get("sector_rejected", 0))
    metrics["industry_unmapped_count"] = float(discipline.get("industry_unmapped", 0))
    metrics["four_no_reason_count"] = float(sum(discipline.get("four_no_reasons", {}).values()))
    rr_values = discipline.get("rr_values", [])
    risk_values = discipline.get("entry_risk_pct", [])
    metrics["avg_entry_rr"] = mean(rr_values) if rr_values else 0.0
    metrics["avg_entry_risk_pct"] = mean(risk_values) if risk_values else 0.0
    metrics["max_entry_risk_pct"] = max(risk_values) if risk_values else 0.0
    metrics["pyramid_adds"] = float(discipline.get("adds", 0))
    metrics["breakeven_win_rate"] = breakeven_win_rate(metrics["payoff_ratio"]) if metrics["payoff_ratio"] != math.inf else 0.0


def _format_reason_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "无"
    return "；".join(f"{reason} {count}" for reason, count in sorted(counts.items()))


def _trimmed_mean(values: list[float], trim_pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    cut = int(len(ordered) * trim_pct)
    trimmed = ordered[cut : len(ordered) - cut] if cut and len(ordered) > 2 * cut else ordered
    return mean(trimmed) if trimmed else 0.0


def _bootstrap_mean_ci(values: list[float], iterations: int = 500, seed: int = 520) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iterations):
        sample = [values[rng.randrange(len(values))] for _item in values]
        means.append(mean(sample))
    means.sort()
    low_idx = int(iterations * 0.025)
    high_idx = min(iterations - 1, int(iterations * 0.975))
    return means[low_idx], means[high_idx]


def _positions_value(positions: dict[str, Position], rows_by_date: dict[str, dict[date, KLine]], day: date) -> float:
    return sum(_position_value(pos, rows_by_date, day) for pos in positions.values())


def _position_value(pos: Position, rows_by_date: dict[str, dict[date, KLine]], day: date) -> float:
    row = rows_by_date.get(pos.code, {}).get(day)
    if row is None:
        prior_dates = [item_day for item_day in rows_by_date.get(pos.code, {}) if item_day < day]
        row = rows_by_date[pos.code].get(max(prior_dates)) if prior_dates else None
    return pos.shares * (row.close if row else pos.entry_price)


def _positions_value_before_open(positions: dict[str, Position], histories: dict[str, list[KLine]], day: date) -> float:
    total = 0.0
    for pos in positions.values():
        previous = _previous_row(histories[pos.code], day)
        total += pos.shares * (previous.close if previous else pos.entry_price)
    return total


def _sector_values(positions: dict[str, Position], rows_by_date: dict[str, dict[date, KLine]], day: date) -> dict[str, float]:
    out: dict[str, float] = {}
    for pos in positions.values():
        out[pos.industry] = out.get(pos.industry, 0.0) + _position_value(pos, rows_by_date, day)
    return out


def _sector_values_before_open(positions: dict[str, Position], histories: dict[str, list[KLine]], day: date) -> dict[str, float]:
    out: dict[str, float] = {}
    for pos in positions.values():
        previous = _previous_row(histories[pos.code], day)
        value = pos.shares * (previous.close if previous else pos.entry_price)
        out[pos.industry] = out.get(pos.industry, 0.0) + value
    return out


def _row_on_date(rows: list[KLine], day: date) -> KLine | None:
    for row in rows:
        if row.date == day:
            return row
        if row.date > day:
            break
    return None


def _to_execution_price(signal_price: float, signal_row: KLine, execution_row: KLine) -> float:
    if signal_price <= 0 or signal_row.close <= 0 or execution_row.close <= 0:
        return 0.0
    return signal_price * execution_row.close / signal_row.close


def _previous_row(rows: list[KLine], day: date) -> KLine | None:
    prev = None
    for row in rows:
        if row.date >= day:
            return prev
        prev = row
    return prev


def _previous_point(points: list[IndicatorPoint], day: date) -> IndicatorPoint | None:
    prev = None
    for point in points:
        if point.date >= day:
            return prev
        prev = point
    return prev


def _open_limit_up(row: KLine, prev_close: float, code: str) -> bool:
    limit = price_limit(code)
    return prev_close > 0 and row.open >= prev_close * (1 + (limit - 0.3) / 100)


def _open_limit_down(row: KLine, prev_close: float, code: str) -> bool:
    limit = price_limit(code)
    return prev_close > 0 and row.open <= prev_close * (1 - (limit - 0.3) / 100)


def _portfolio_signal(prev: IndicatorPoint, point: IndicatorPoint, row: KLine) -> str:
    if (
        crossed_up(prev.ma5, prev.ma20, point.ma5, point.ma20)
        and point.ma5
        and point.ma20
        and row.close > point.ma5 > point.ma20
        and point.macd_dif is not None
        and point.macd_dea is not None
        and point.macd_dif > point.macd_dea
        and not (point.rsi14 is not None and point.rsi14 > 72)
    ):
        return "入选"
    return "观察"


def _initial_stop(row: KLine, point: IndicatorPoint, config: RiskConfig) -> float:
    if point.atr14:
        return max(row.close - config.atr_k * point.atr14, row.low * 0.98)
    return row.close * 0.92


def _target_price(
    rows: list[KLine], day: date, close: float, config: RiskConfig, point: IndicatorPoint | None = None
) -> float:
    history = [row for row in rows if row.date <= day]
    if not history:
        return close * 1.1
    point = point or build_indicators(history)[-1]
    recent = history[-80:-1]
    overhead_highs = sorted({row.high for row in recent if row.high > close})
    swing_high = overhead_highs[0] if overhead_highs else None
    atr_target = close + (point.atr14 or close * 0.03) * config.target_atr_k
    measured_target = close + max(close - min(row.low for row in history[-20:]), close * 0.04)
    # The first executable objective must be conservative.  Taking the
    # farthest historical high made the R:R gate optimistic and untestable.
    candidates = [value for value in (swing_high, atr_target, measured_target) if value and value > close]
    return min(candidates) if candidates else close * 1.04


def _target_price_with_point(
    rows: list[KLine], day: date, close: float, config: RiskConfig, point: IndicatorPoint
) -> float:
    try:
        return _target_price(rows, day, close, config, point=point)
    except TypeError as exc:
        if "unexpected keyword argument 'point'" not in str(exc):
            raise
        return _target_price(rows, day, close, config)


def _basket_regime(histories: dict[str, list[KLine]], day: date) -> RegimeState:
    valid = []
    for rows in histories.values():
        trimmed = [row for row in rows if row.date <= day]
        if len(trimmed) >= 61:
            point = build_indicators(trimmed)[-1]
            valid.append((trimmed, point))
    if not valid:
        return RegimeState("basket", "篮子", day, "down", False, "篮子regime数据不足")
    above60 = sum(1 for rows, point in valid if point.ma60 is not None and rows[-1].close > point.ma60)
    breadth = above60 / len(valid)
    regime = "trend_up" if breadth >= 0.60 else ("range" if breadth >= 0.40 else "down")
    return RegimeState("basket", "篮子", day, regime, regime == "trend_up", f"basket breadth60={breadth:.2%}")


def _sector_states_for_day(
    histories: dict[str, list[KLine]], sector_map: dict[str, str], day: date
) -> dict[str, SectorState]:
    grouped: dict[str, dict[str, list[KLine]]] = {}
    for code, rows in histories.items():
        industry = industry_of(code, sector_map)
        grouped.setdefault(industry, {})[code] = rows
    return {industry: sector_state_from_members(industry, member_rows, day) for industry, member_rows in grouped.items()}


def _analyze_verdicts(
    meta: StockMeta,
    rows: list[KLine],
    all_dates: list[date],
    config: BacktestConfig,
    regime_by_date: dict[date, RegimeState] | None = None,
    points: list[IndicatorPoint] | None = None,
    index_rows_by_date: dict[date, KLine] | None = None,
    index_prior_by_date: dict[tuple[date, int], KLine] | None = None,
    sector_map: dict[str, str] | None = None,
    sector_heat_by_date: dict[date, dict[str, SectorAlpha]] | None = None,
) -> dict[date, str]:
    wanted = set(all_dates)
    verdicts: dict[date, str] = {}
    indicators = points or build_indicators(rows)
    for idx, row in enumerate(rows):
        if row.date not in wanted or idx + 1 < 61:
            continue
        if is_v5_strategy_mode(config.strategy_mode):
            state = regime_by_date.get(row.date) if regime_by_date else None
            if state is not None and state.regime == "BEAR":
                verdicts[row.date] = OBSERVE_VERDICT
                continue
            alpha = score_alpha(
                rows,
                idx,
                indicators[idx],
                index_rows_by_date or {},
                index_prior_by_date or {},
            )
            entry = trend_pullback_entry(rows, idx, indicators[idx])
            heat_bonus = sector_heat_bonus(meta.code, row.date, sector_map or {}, sector_heat_by_date or {})
            verdicts[row.date] = ENTRY_VERDICT if alpha.total + heat_bonus >= 70 and entry.score >= 60 else OBSERVE_VERDICT
            continue
        if config.strategy_mode == "strategy_v2_modular":
            state = regime_by_date.get(row.date) if regime_by_date else None
            prior_ma60 = indicators[idx - 5].ma60 if idx >= 5 else None
            decision = evaluate_modular(
                rows[: idx + 1],
                row.date,
                state,
                point=indicators[idx],
                previous=indicators[idx - 1] if idx else None,
                prior_ma60=prior_ma60,
            )
            verdicts[row.date] = decision.verdict
            continue
        if not _could_be_standard_520(rows, indicators, idx):
            verdicts[row.date] = "不入选"
            continue
        report = analyze(meta, rows[: idx + 1], row.date, regime_state=None, min_history=config.min_history)
        verdicts[row.date] = report.verdict
    return verdicts


def _analyze_v5_candidate_signals(
    meta: StockMeta,
    rows: list[KLine],
    all_dates: list[date],
    regime_by_date: dict[date, RegimeState],
    points: list[IndicatorPoint],
    index_rows_by_date: dict[date, KLine],
    index_prior_by_date: dict[tuple[date, int], KLine],
    sector_map: dict[str, str],
    sector_heat_by_date: dict[date, dict[str, SectorAlpha]],
    eligible_by_date: dict[date, set[str]],
) -> dict[date, CandidateSignal]:
    wanted = set(all_dates)
    signals: dict[date, CandidateSignal] = {}
    industry = industry_of(meta.code, sector_map)
    for idx, row in enumerate(rows):
        if row.date not in wanted or idx + 1 < 61:
            continue
        point = points[idx]
        state = regime_by_date.get(row.date, RegimeState("basket", "basket", row.date, "down", False, "missing"))
        alpha = score_alpha(rows, idx, point, index_rows_by_date, index_prior_by_date)
        entry = trend_pullback_entry(rows, idx, point)
        heat = sector_heat_by_date.get(row.date, {}).get(industry)
        heat_score = heat.heat_score if heat is not None else 0.0
        heat_bonus = sector_heat_bonus(meta.code, row.date, sector_map, sector_heat_by_date)
        eligible = meta.code in eligible_by_date.get(row.date, set())
        market_ok = state.regime != "BEAR"
        ranking_score = alpha.total + heat_bonus
        hard_pass = eligible and market_ok and ranking_score >= 70 and entry.score >= 60
        reasons = list(alpha.reasons) + list(entry.reasons)
        if not eligible:
            reasons.append("not_in_dynamic_universe")
        if not market_ok:
            reasons.append("bear_market")
        if ranking_score < 70:
            reasons.append("alpha_threshold")
        if entry.score < 60:
            reasons.append("entry_threshold")
        signals[row.date] = CandidateSignal(
            code=meta.code,
            name=meta.name,
            date=row.date,
            industry=industry,
            market_regime=state.regime,
            eligible=eligible,
            market_ok=market_ok,
            alpha_total=float(alpha.total),
            trend_score=float(alpha.trend),
            relative_strength_score=float(alpha.relative_strength),
            volume_quality_score=float(alpha.volume_quality),
            risk_score=float(alpha.risk),
            sector_bonus=float(heat_bonus),
            sector_heat_score=float(heat_score),
            entry_score=float(entry.score),
            ranking_score=float(ranking_score),
            hard_pass=hard_pass,
            reasons=tuple(reasons),
        )
    return signals


def _could_be_standard_520(rows: list[KLine], points: list[IndicatorPoint], idx: int) -> bool:
    row = rows[idx]
    point = points[idx]
    cross_age = _recent_520_cross_age(points, idx, "ma5", "ma20", lookback=3)
    if cross_age is None or not _confirmation_holds_at_index(rows, points, idx, cross_age):
        return False
    if not (point.ma5 and point.ma20 and row.close > point.ma5 and row.close > point.ma20):
        return False
    macd_age = _recent_520_cross_age(points, idx, "macd_dif", "macd_dea", lookback=3)
    if not (
        point.macd_dif is not None
        and point.macd_dea is not None
        and point.macd_dif > point.macd_dea > 0
        and macd_age is not None
        and abs(cross_age - macd_age) <= 2
    ):
        return False
    if pct_change(rows[idx - 60].close, row.close) < 5:
        return False
    if (point.volume_expansion or point.volume_ratio or 0) < 1.2:
        return False
    if point.rsi14 is not None and point.rsi14 > 72:
        return False
    return True


def _recent_520_cross(points: list[IndicatorPoint], idx: int, lookback: int) -> bool:
    return _recent_520_cross_age(points, idx, "ma5", "ma20", lookback) is not None


def _recent_520_cross_age(
    points: list[IndicatorPoint], idx: int, left: str, right: str, lookback: int
) -> int | None:
    start = max(1, idx - lookback)
    for pos in range(idx, start - 1, -1):
        prev = points[pos - 1]
        cur = points[pos]
        if crossed_up(getattr(prev, left), getattr(prev, right), getattr(cur, left), getattr(cur, right)):
            return idx - pos
    return None


def _confirmation_holds_at_index(rows: list[KLine], points: list[IndicatorPoint], idx: int, cross_age: int) -> bool:
    start = idx - cross_age
    for pos in range(start, idx + 1):
        point = points[pos]
        if point.ma5 is None or point.ma20 is None:
            return False
        if rows[pos].close <= point.ma5 or rows[pos].close <= point.ma20:
            return False
    return True


def _regime_by_date_from_index(symbol: str, rows: list[KLine], all_dates: list[date]) -> dict[date, RegimeState]:
    if not rows:
        raise DataError("index regime rows are empty")
    sorted_rows = sorted(rows, key=lambda row: row.date)
    points = build_indicators(sorted_rows)
    out: dict[date, RegimeState] = {}
    row_idx = 0
    for day in all_dates:
        while row_idx + 1 < len(sorted_rows) and sorted_rows[row_idx + 1].date <= day:
            row_idx += 1
        current = sorted_rows[row_idx]
        if current.date > day:
            out[day] = RegimeState(symbol, symbol, day, "down", False, "指数数据不足")
            continue
        if row_idx < 60:
            out[day] = RegimeState(symbol, symbol, day, "down", False, "指数regime数据不足")
            continue
        point = points[row_idx]
        rise60 = pct_change(sorted_rows[row_idx - 60].close, current.close)
        ma20_slope = _point_slope(points, row_idx, "ma20", 5)
        above20 = point.ma20 is not None and current.close > point.ma20
        above60 = point.ma60 is not None and current.close > point.ma60
        if above20 and above60 and rise60 >= 3 and ma20_slope >= 0:
            regime, ok = "trend_up", True
        elif not above60 and rise60 <= -5:
            regime, ok = "down", False
        else:
            regime, ok = "range", False
        ma20_text = f"{point.ma20:.2f}" if point.ma20 is not None else "N/A"
        ma60_text = f"{point.ma60:.2f}" if point.ma60 is not None else "N/A"
        detail = f"{symbol} {day.isoformat()} regime={regime}; close={current.close:.2f}, ma20={ma20_text}, ma60={ma60_text}, rise60={rise60:.2f}%"
        out[day] = RegimeState(symbol, symbol, day, regime, ok, detail)
    return out


def _index_reference_maps(rows: list[KLine], windows: tuple[int, ...]) -> tuple[dict[date, KLine], dict[tuple[date, int], KLine]]:
    sorted_rows = sorted(rows, key=lambda row: row.date)
    by_date = {row.date: row for row in sorted_rows}
    prior: dict[tuple[date, int], KLine] = {}
    for idx, row in enumerate(sorted_rows):
        for window in windows:
            if idx >= window:
                prior[(row.date, window)] = sorted_rows[idx - window]
    return by_date, prior


def _point_slope(points: list[IndicatorPoint], idx: int, name: str, window: int) -> float:
    if idx + 1 < window:
        return 0.0
    values = [getattr(point, name) for point in points[idx + 1 - window : idx + 1]]
    if any(value is None for value in values):
        return 0.0
    first = values[0]
    last = values[-1]
    return (last / first - 1) * 100 if first else 0.0


def _basket_regime_from_points(
    rows_by_date: dict[str, dict[date, KLine]],
    points_by_date: dict[str, dict[date, IndicatorPoint]],
    day: date,
) -> RegimeState:
    valid = []
    for code, dated_rows in rows_by_date.items():
        row = dated_rows.get(day)
        point = points_by_date.get(code, {}).get(day)
        if row is not None and point is not None and point.ma60 is not None:
            valid.append((row, point))
    if not valid:
        return RegimeState("basket", "篮子", day, "down", False, "篮子regime数据不足")
    above60 = sum(1 for row, point in valid if row.close > (point.ma60 or 0))
    breadth = above60 / len(valid)
    regime = "trend_up" if breadth >= 0.60 else ("range" if breadth >= 0.40 else "down")
    return RegimeState("basket", "篮子", day, regime, regime == "trend_up", f"basket breadth60={breadth:.2%}")


def _sector_states_from_points(
    histories: dict[str, list[KLine]],
    sector_map: dict[str, str],
    rows_by_date: dict[str, dict[date, KLine]],
    points_by_date: dict[str, dict[date, IndicatorPoint]],
    row_index_by_date: dict[str, dict[date, int]],
    day: date,
) -> dict[str, SectorState]:
    grouped: dict[str, list[str]] = {}
    for code in histories:
        grouped.setdefault(industry_of(code, sector_map), []).append(code)

    states: dict[str, SectorState] = {}
    for industry, codes in grouped.items():
        valid: list[tuple[KLine, IndicatorPoint, float]] = []
        for code in codes:
            row = rows_by_date.get(code, {}).get(day)
            point = points_by_date.get(code, {}).get(day)
            idx = row_index_by_date.get(code, {}).get(day)
            if row is None or point is None or point.ma20 is None or idx is None or idx < 60:
                continue
            old = histories[code][idx - 60].close
            valid.append((row, point, pct_change(old, row.close)))
        if not valid:
            states[industry] = SectorState(industry, day, "unknown", False, 0.0, "行业数据不足")
            continue
        above20 = sum(1 for row, point, _rise in valid if row.close > (point.ma20 or 0))
        breadth = above20 / len(valid)
        rise60 = mean([rise for _row, _point, rise in valid])
        sector_ok = breadth >= 0.55 and rise60 >= 0
        regime = "trend" if sector_ok else "weak"
        detail = f"{industry} breadth={breadth:.2%}, rise60={rise60:.2f}%, members={len(valid)}"
        states[industry] = SectorState(industry, day, regime, sector_ok, breadth, detail)
    return states


def _exit_by_discipline(
    pos: Position,
    row: KLine,
    rows: list[KLine],
    day: date,
    point: IndicatorPoint,
    config: RiskConfig,
) -> str | None:
    if row.close <= pos.stop:
        return "close_stop"
    if time_stop_trigger(pos.entry_price, row.close, pos.holding_bars, config):
        return "time_stop"
    history = [item for item in rows if item.date <= day]
    if len(history) >= 4 and pos.holding_bars >= 3:
        highs = [item.high for item in history[-4:]]
        if max(highs[1:]) <= highs[0]:
            return "three_bars_no_high"
    if point.ma20 and row.close < point.ma20:
        return "ma20_break"
    return None


def _add_signal(pos: Position, row: KLine, rows: list[KLine], day: date, point: IndicatorPoint) -> bool:
    if pos.pyramid_stage >= 1 or row.close <= pos.entry_price or row.close <= pos.stop:
        return False
    history = [item for item in rows if item.date <= day]
    if len(history) < 21:
        return False
    prev = history[-2]
    recent_high = max(item.high for item in history[-21:-1])
    volume_ok = (point.volume_expansion or point.volume_ratio or 0) >= 1.0
    breakout = row.high > recent_high and row.close > prev.close and volume_ok
    pullback_ma = False
    for ma in (point.ma5, point.ma10, point.ma20):
        if ma and row.low <= ma * 1.02 and row.close >= ma and row.close >= prev.close and volume_ok:
            pullback_ma = True
            break
    return breakout or pullback_ma


def _max_drawdown(equity_curve: list[tuple[date, float]]) -> float:
    peak = -math.inf
    max_dd = 0.0
    for _day, equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, equity / peak - 1)
    return max_dd


def _daily_returns(equity_curve: list[tuple[date, float]]) -> list[float]:
    out: list[float] = []
    for idx in range(1, len(equity_curve)):
        prev = equity_curve[idx - 1][1]
        cur = equity_curve[idx][1]
        if prev:
            out.append(cur / prev - 1)
    return out


def _years(equity_curve: list[tuple[date, float]] | None) -> float:
    if not equity_curve or len(equity_curve) < 2:
        return 0.0
    return max((equity_curve[-1][0] - equity_curve[0][0]).days / 365.25, 1 / 365.25)
