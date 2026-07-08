from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean, pstdev

from .entry_filters import EntryFilterConfig, apply_four_no_entry, breakeven_win_rate
from .indicators import build_indicators, crossed_up, pct_change
from .data import DataError, eastmoney_history, normalize_code, tencent_history
from .microstructure import is_limit_down, is_limit_up, price_limit
from .models import IndicatorPoint, KLine, RegimeState, StockMeta
from .quality import ensure_data_quality
from .risk import AccountRiskState, RiskConfig, allowed_new_position_shares, time_stop_trigger, trailing_stop, update_account_risk
from .sector import DEFAULT_SECTOR_MAP, SectorState, industry_of, sector_state_from_members
from .strategy import analyze


COMMISSION_RATE = 0.00025
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 0.0005
TRANSFER_RATE = 0.00001
SLIPPAGE_BPS = 5.0


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
    pyramid_stage: int = 0


@dataclass(frozen=True)
class PendingOrder:
    code: str
    side: str
    reason: str
    shares: int = 0
    stop: float = 0.0
    rr: float = 0.0


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
                net = gross - sell_cost
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
                        costs=sell_cost,
                        net_pnl=net,
                        holding_days=(today.date - (entry_date or today.date)).days,
                        entry_reason=entry_reason,
                        exit_reason=pending_reason or "exit_rule",
                    )
                )
                shares = 0
                entry_price = 0.0
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
        sell_cost = _costs(meta.code, last.close, shares, "sell", config)
        gross = (last.close - entry_price) * shares
        net = gross - sell_cost
        trades.append(
            Trade(
                code=meta.code,
                name=meta.name,
                entry_date=entry_date or last.date,
                exit_date=last.date,
                entry_price=entry_price,
                exit_price=last.close,
                shares=shares,
                gross_pnl=gross,
                costs=sell_cost,
                net_pnl=net,
                holding_days=(last.date - (entry_date or last.date)).days,
                entry_reason=entry_reason,
                exit_reason="end_of_backtest",
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
) -> tuple[Path, Path, dict[str, float]]:
    config = config or BacktestConfig()
    return portfolio_backtest_symbols(symbols, start, end, output_dir, config=config)


def portfolio_backtest_symbols(
    symbols: list[str],
    start: date,
    end: date,
    output_dir: Path,
    config: BacktestConfig | None = None,
    risk_config: RiskConfig | None = None,
    entry_config: EntryFilterConfig | None = None,
    sector_map: dict[str, str] | None = None,
) -> tuple[Path, Path, dict[str, float]]:
    config = config or BacktestConfig()
    risk_config = risk_config or RiskConfig()
    entry_config = entry_config or EntryFilterConfig()
    sector_map = sector_map or DEFAULT_SECTOR_MAP
    metas: dict[str, StockMeta] = {}
    histories: dict[str, list[KLine]] = {}
    indicators: dict[str, list[IndicatorPoint]] = {}
    points_by_date: dict[str, dict[date, IndicatorPoint]] = {}
    rows_by_date: dict[str, dict[date, KLine]] = {}
    row_index_by_date: dict[str, dict[date, int]] = {}

    for idx, symbol in enumerate(symbols, 1):
        code = normalize_code(symbol)
        lookback = max((end - start).days + 620, 1000)
        try:
            meta, rows = tencent_history(code, end, lookback_days=lookback, adjust="hfq")
        except DataError:
            meta, rows = eastmoney_history(code, end, lookback_days=lookback, adjust=2)
        rows = [row for row in rows if row.date <= end]
        ensure_data_quality(rows, min_bars=61)
        metas[code] = meta
        histories[code] = rows
        pts = build_indicators(rows)
        indicators[code] = pts
        points_by_date[code] = {point.date: point for point in pts}
        rows_by_date[code] = {row.date: row for row in rows}
        row_index_by_date[code] = {row.date: row_idx for row_idx, row in enumerate(rows)}
        print(f"loaded {idx}/{len(symbols)} {code} rows={len(rows)}", flush=True)

    all_dates = sorted({row.date for rows in histories.values() for row in rows if start <= row.date <= end})
    regime_by_date = {day: _basket_regime_from_points(rows_by_date, points_by_date, day) for day in all_dates}
    sector_states_by_date = {
        day: _sector_states_from_points(histories, sector_map, rows_by_date, points_by_date, row_index_by_date, day)
        for day in all_dates
    }
    cash = config.initial_cash
    positions: dict[str, Position] = {}
    pending: list[PendingOrder] = []
    trades: list[Trade] = []
    equity_curve: list[tuple[date, float]] = []
    risk_state = AccountRiskState(peak_equity=config.initial_cash)
    discipline = {
        "entries": 0,
        "rejected_four_no": 0,
        "rejected_caps": 0,
        "limit_blocked": 0,
        "rr_values": [],
        "max_symbol_pct": 0.0,
        "max_sector_pct": 0.0,
        "max_exposure": 0.0,
        "regime_exposure": {},
    }

    previous_equity = config.initial_cash
    for day in all_dates:
        equity = cash + _positions_value(positions, rows_by_date, day)
        risk_state = update_account_risk(risk_state, equity, previous_equity, day.isocalendar()[:2], risk_config)
        previous_equity = equity

        # D+1 exits first.
        remaining_pending: list[PendingOrder] = []
        for order in pending:
            row = rows_by_date.get(order.code, {}).get(day)
            prev = _previous_row(histories[order.code], day)
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
                net = gross - sell_cost
                cash += fill * pos.shares - sell_cost
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
                        sell_cost,
                        net,
                        (day - pos.entry_date).days,
                        pos.entry_reason,
                        order.reason,
                    )
                )
                del positions[order.code]
            elif order.side == "buy":
                if order.code in positions or risk_state.halted_today or risk_state.stopped_for_drawdown:
                    continue
                if _open_limit_up(row, prev.close, order.code):
                    discipline["limit_blocked"] += 1
                    continue
                fill = _fill_price(row, config.fill) * (1 + config.slippage_bps / 10000)
                industry = industry_of(order.code, sector_map)
                current_equity = cash + _positions_value(positions, rows_by_date, day)
                current_gross = _positions_value(positions, rows_by_date, day)
                sector_values = _sector_values(positions, rows_by_date, day)
                execution_regime = regime_by_date.get(day, RegimeState("basket", "篮子", day, "down", False, "missing"))
                allowed_at_open = allowed_new_position_shares(
                    equity=current_equity,
                    cash=cash,
                    entry=fill,
                    stop=order.stop,
                    current_symbol_value=0.0,
                    current_sector_value=sector_values.get(industry, 0.0),
                    current_gross_value=current_gross,
                    regime=execution_regime.regime,
                    config=risk_config,
                    pyramid_stage=0,
                    sector_multiplier=0.5 if risk_state.next_week_half_size else 1.0,
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
                    highest_high=row.high,
                )
                discipline["entries"] += 1
                discipline["rr_values"].append(order.rr)
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
            pos.stop = max(pos.stop, trailing_stop(pos.entry_price, row.close, point.ma10, point.atr14, risk_config))
            if _exit_by_discipline(pos, row, histories[pos.code], day, point, risk_config):
                pending.append(PendingOrder(pos.code, "sell", "discipline_exit"))

        for code, rows in histories.items():
            if code in positions or risk_state.halted_today or risk_state.stopped_for_drawdown:
                continue
            row = rows_by_date[code].get(day)
            point = points_by_date[code].get(day)
            prev_point = _previous_point(indicators[code], day)
            if row is None or point is None or prev_point is None:
                continue
            verdict = _portfolio_signal(prev_point, point, row)
            industry = industry_of(code, sector_map)
            sector_state = sector_states.get(industry, SectorState(industry, day, "unknown", False, 0.0, "missing"))
            if not regime.regime_ok or not sector_state.sector_ok or verdict != "入选":
                continue
            stop = _initial_stop(row, point, risk_config)
            target = _target_price(rows, day, row.close)
            decision = apply_four_no_entry(verdict, row, point, code, row.close, stop, target, entry_config)
            if not decision.ok:
                discipline["rejected_four_no"] += 1
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
                sector_multiplier=0.5 if risk_state.next_week_half_size else 1.0,
            )
            if shares <= 0:
                discipline["rejected_caps"] += 1
                continue
            pending.append(PendingOrder(code, "buy", "standard_520_discipline", shares=shares, stop=stop, rr=decision.rr))
            planned_value = row.close * shares
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

    # Liquidate at final close for evidence.
    if all_dates:
        last_day = all_dates[-1]
        for code, pos in list(positions.items()):
            row = rows_by_date[code].get(last_day)
            if row is None:
                continue
            sell_cost = _costs(code, row.close, pos.shares, "sell", config)
            gross = (row.close - pos.entry_price) * pos.shares
            trades.append(
                Trade(
                    code,
                    pos.name,
                    pos.entry_date,
                    last_day,
                    pos.entry_price,
                    row.close,
                    pos.shares,
                    gross,
                    sell_cost,
                    gross - sell_cost,
                    (last_day - pos.entry_date).days,
                    pos.entry_reason,
                    "end_of_backtest",
                )
            )

    stem = "basket" if len(symbols) > 1 else normalize_code(symbols[0])
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_path = output_dir / f"trades_{stem}_{start.isoformat()}_{end.isoformat()}.csv"
    metrics_path = output_dir / f"metrics_{stem}_{start.isoformat()}_{end.isoformat()}.md"
    _write_trades(trades_path, trades)
    metrics = metrics_from_trades(trades, equity_curve, config.initial_cash, 0, split_date=config.split_date)
    _add_discipline_metrics(metrics, discipline)
    _write_metrics(metrics_path, metrics, config, discipline=discipline, risk_config=risk_config, entry_config=entry_config)
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
    metrics = {
        "total_return": total_return,
        "cagr": cagr,
        "expectancy": ev,
        "win_rate": win_rate,
        "payoff_ratio": payoff,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "trade_count": float(len(trades)),
        "avg_holding_days": mean([trade.holding_days for trade in trades]) if trades else 0.0,
        "exposure": exposure_days / len(equity_curve) if equity_curve else 0.0,
    }
    if split_date:
        is_trades = [trade for trade in trades if trade.entry_date < split_date]
        oos_trades = [trade for trade in trades if trade.entry_date >= split_date]
        metrics["is_trade_count"] = float(len(is_trades))
        metrics["oos_trade_count"] = float(len(oos_trades))
        metrics["is_expectancy"] = metrics_from_trades(is_trades, [], initial_cash)["expectancy"]
        metrics["oos_expectancy"] = metrics_from_trades(oos_trades, [], initial_cash)["expectancy"]
    return metrics


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


def _write_metrics(
    path: Path,
    metrics: dict[str, float],
    config: BacktestConfig,
    discipline: dict | None = None,
    risk_config: RiskConfig | None = None,
    entry_config: EntryFilterConfig | None = None,
) -> None:
    lines = [
        "# Backtest Metrics",
        "",
        f"- fill: `{config.fill}`",
        f"- commission: `{config.commission_rate}`; min commission: `{config.min_commission}`",
        f"- stamp tax sell only: `{config.stamp_tax_rate}`; transfer SH only: `{config.transfer_rate}`",
        f"- slippage bps: `{config.slippage_bps}`",
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
                f"- 盈亏比：实际入场最小 R:R {min_rr:.2f}，平均 R:R {avg_rr:.2f}，对应盈亏平衡胜率 {breakeven_win_rate(avg_rr):.2%}。",
                f"- 熔断/仓位：仓位被 cap 拒绝 {discipline.get('rejected_caps', 0)} 次，涨跌停阻断 {discipline.get('limit_blocked', 0)} 次。",
                "",
                "| Regime | Average Exposure |",
                "| --- | ---: |",
            ]
        )
        for regime, values in sorted(discipline.get("regime_exposure", {}).items()):
            lines.append(f"| {regime} | {(mean(values) if values else 0):.6f} |")
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
    rr_values = discipline.get("rr_values", [])
    metrics["avg_entry_rr"] = mean(rr_values) if rr_values else 0.0
    metrics["breakeven_win_rate"] = breakeven_win_rate(metrics["payoff_ratio"]) if metrics["payoff_ratio"] != math.inf else 0.0


def _positions_value(positions: dict[str, Position], rows_by_date: dict[str, dict[date, KLine]], day: date) -> float:
    return sum(_position_value(pos, rows_by_date, day) for pos in positions.values())


def _position_value(pos: Position, rows_by_date: dict[str, dict[date, KLine]], day: date) -> float:
    row = rows_by_date.get(pos.code, {}).get(day)
    return pos.shares * (row.close if row else pos.entry_price)


def _sector_values(positions: dict[str, Position], rows_by_date: dict[str, dict[date, KLine]], day: date) -> dict[str, float]:
    out: dict[str, float] = {}
    for pos in positions.values():
        out[pos.industry] = out.get(pos.industry, 0.0) + _position_value(pos, rows_by_date, day)
    return out


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


def _target_price(rows: list[KLine], day: date, close: float) -> float:
    history = [row for row in rows if row.date <= day]
    recent = history[-60:-1]
    if not recent:
        return close * 1.1
    return max(max(row.high for row in recent), close * 1.02)


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
) -> bool:
    holding_days = (day - pos.entry_date).days
    if row.close <= pos.stop:
        return True
    if time_stop_trigger(pos.entry_price, row.close, holding_days, config):
        return True
    history = [item for item in rows if item.date <= day]
    if len(history) >= 4 and holding_days >= 3:
        highs = [item.high for item in history[-4:]]
        if max(highs[1:]) <= highs[0]:
            return True
    if point.ma20 and row.close < point.ma20:
        return True
    return False


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
