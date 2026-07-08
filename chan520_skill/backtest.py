from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean, pstdev

from .data import DataError, eastmoney_history, normalize_code, tencent_history
from .microstructure import is_limit_down, is_limit_up
from .models import KLine, RegimeState, StockMeta
from .quality import ensure_data_quality
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


def _write_metrics(path: Path, metrics: dict[str, float], config: BacktestConfig) -> None:
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
    path.write_text("\n".join(lines), encoding="utf-8")


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
