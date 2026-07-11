from __future__ import annotations

from datetime import date, timedelta

from chan520_skill.backtest import CandidateSignal, Trade
from chan520_skill.models import KLine, RegimeState, StockMeta


def make_candidate(
    code: str,
    *,
    ranking: float = 80.0,
    entry: float = 70.0,
    rs: float = 10.0,
    hard_pass: bool = True,
) -> CandidateSignal:
    return CandidateSignal(
        code=code,
        name=code,
        date=date(2026, 1, 5),
        industry="tech",
        market_regime="NORMAL",
        eligible=True,
        market_ok=True,
        alpha_total=ranking,
        trend_score=30.0,
        relative_strength_score=rs,
        volume_quality_score=15.0,
        risk_score=20.0,
        sector_bonus=0.0,
        sector_heat_score=0.0,
        entry_score=entry,
        ranking_score=ranking,
        hard_pass=hard_pass,
        reasons=("unit",),
    )


def make_trade(idx: int, pnl: float) -> Trade:
    entry = date(2022, 1, 3) + timedelta(days=idx * 8)
    return Trade(
        code=f"600{idx % 999:03d}",
        name="sample",
        entry_date=entry,
        exit_date=entry + timedelta(days=3),
        entry_price=10.0,
        exit_price=10.0 + pnl / 100,
        shares=100,
        gross_pnl=pnl,
        costs=0.0,
        net_pnl=pnl,
        holding_days=3,
        entry_reason="test",
        exit_reason="test",
    )


def make_bars(start: date, count: int, *, base: float = 10.0, step: float = 0.08) -> list[KLine]:
    rows: list[KLine] = []
    prev = base
    for idx in range(count):
        close = base + idx * step
        day = start + timedelta(days=idx)
        rows.append(
            KLine(
                date=day,
                open=close * 0.99,
                close=close,
                high=close * 1.02,
                low=close * 0.98,
                volume=10_000_000,
                amount=500_000_000,
                amplitude=4.0,
                pct_chg=(close / prev - 1) * 100 if idx else 0.0,
                change=close - prev if idx else 0.0,
                turnover=2.0,
            )
        )
        prev = close
    return rows


def make_meta(code: str = "600000") -> StockMeta:
    return StockMeta(code, code, 1)


def make_regimes(rows: list[KLine]) -> dict[date, RegimeState]:
    return {row.date: RegimeState("000300", "HS300", row.date, "NORMAL", True, "unit") for row in rows}
