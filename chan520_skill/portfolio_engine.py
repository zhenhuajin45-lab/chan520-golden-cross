from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from .backtest import BacktestConfig, portfolio_backtest_symbols
from .models import KLine, StockMeta
from .risk import RiskConfig


HistoryLoader = Callable[..., tuple[StockMeta, list[KLine]]]


@dataclass(frozen=True)
class PortfolioEngineConfig:
    initial_cash: float = 100000.0
    max_positions: int = 5
    max_position_pct: float = 0.20
    max_sector_pct: float = 0.40
    cash_reserve_pct: float = 0.20
    strategy_mode: str = "strategy_v5_alpha_ranked"


def run_alpha_portfolio(
    symbols: list[str],
    start: date,
    end: date,
    output_dir: Path,
    history_loader: HistoryLoader,
    sector_map: dict[str, str],
    index_rows: list[KLine],
    eligible_by_date: dict[date, set[str]],
    config: PortfolioEngineConfig | None = None,
) -> tuple[Path, Path, dict[str, float]]:
    config = config or PortfolioEngineConfig()
    backtest_config = BacktestConfig(
        initial_cash=config.initial_cash,
        strategy_mode=config.strategy_mode,
        split_date=date(2022, 1, 1),
        regime_index="000300",
        require_industry=False,
    )
    risk_config = RiskConfig(
        max_position_pct=config.max_position_pct,
        max_sector_pct=config.max_sector_pct,
        cash_reserve_pct=config.cash_reserve_pct,
    )
    return portfolio_backtest_symbols(
        symbols,
        start,
        end,
        output_dir,
        config=backtest_config,
        risk_config=risk_config,
        sector_map=sector_map,
        index_rows=index_rows,
        history_loader=history_loader,
        eligible_by_date=eligible_by_date,
        max_positions=config.max_positions,
    )
