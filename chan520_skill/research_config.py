from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .entry_filters import EntryFilterConfig
from .portfolio_engine import PortfolioEngineConfig
from .risk import RiskConfig


@dataclass(frozen=True)
class AlphaConfig:
    trend_weight: int = 40
    relative_strength_weight: int = 20
    volume_quality_weight: int = 20
    risk_weight: int = 20
    hard_pass_threshold: int = 70


@dataclass(frozen=True)
class EntryModelConfig:
    pullback_entry_threshold: int = 60
    ma_fast: int = 20
    ma_slow: int = 60


@dataclass(frozen=True)
class MarketRegimeConfig:
    index_symbol: str = "000300"
    bull_breadth: float = 0.55
    bear_breadth: float = 0.35
    bear_volatility: float = 0.04


@dataclass(frozen=True)
class SectorConfig:
    source: str = "static_end_date"
    classification_level: str = "industry"
    point_in_time: bool = False
    min_members: int = 20
    max_members: int = 1200
    min_sample_members: int = 30


@dataclass(frozen=True)
class StatisticsConfig:
    ordinary_bootstrap_iterations: int = 500
    cluster_bootstrap_iterations: int = 2000
    moving_block_iterations: int = 500
    bootstrap_seed: int = 520
    min_trades: int = 100
    min_entry_week_clusters: int = 26
    min_full_years: int = 3


@dataclass(frozen=True)
class ResearchRunConfig:
    portfolio_engine: PortfolioEngineConfig = field(default_factory=PortfolioEngineConfig)
    backtest: dict[str, Any] = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    entry_filter: EntryFilterConfig = field(default_factory=EntryFilterConfig)
    alpha: AlphaConfig = field(default_factory=AlphaConfig)
    entry_model: EntryModelConfig = field(default_factory=EntryModelConfig)
    market_regime: MarketRegimeConfig = field(default_factory=MarketRegimeConfig)
    sector: SectorConfig = field(default_factory=SectorConfig)
    statistics: StatisticsConfig = field(default_factory=StatisticsConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
