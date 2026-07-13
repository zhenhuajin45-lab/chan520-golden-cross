from __future__ import annotations

from datetime import date, timedelta

from chan520_skill.alpha_score import score_alpha
from chan520_skill.dynamic_universe import InstrumentStatus, build_dynamic_universe
from chan520_skill.entry_model import trend_pullback_entry
from chan520_skill.indicators import build_indicators
from chan520_skill.market_regime import build_market_regime
from chan520_skill.models import KLine
from chan520_skill.sector_alpha import build_sector_heat, sector_heat_bonus


def _bars(start: date, count: int, base: float = 10.0, step: float = 0.05, amount: float = 500_000_000) -> list[KLine]:
    rows = []
    prev = None
    for idx in range(count):
        close = base + idx * step
        open_price = close * 0.99
        day = start + timedelta(days=idx)
        rows.append(
            KLine(
                day,
                open_price,
                close,
                close * 1.02,
                close * 0.98,
                10_000_000,
                amount,
                4.0,
                (close / prev - 1) * 100 if prev else 0.0,
                close - prev if prev else 0.0,
                0.0,
            )
        )
        prev = close
    return rows


def test_dynamic_universe_uses_daily_status_and_liquidity() -> None:
    rows = _bars(date(2024, 1, 1), 260)
    day = rows[-1].date
    statuses = {
        day: {
            "600000": InstrumentStatus("600000", "OK", rows[0].date, None, False),
            "600001": InstrumentStatus("600001", "ST Bad", rows[0].date, None, False),
        }
    }
    universe = build_dynamic_universe({"600000": rows, "600001": rows}, statuses, [day])
    assert universe[day] == {"600000"}


def test_market_regime_outputs_state_and_multiplier() -> None:
    rows = _bars(date(2024, 1, 1), 160, base=100, step=0.2)
    day = rows[-1].date
    regimes = build_market_regime(rows, {day: 0.70}, [day])
    assert regimes[day].state == "BULL"
    assert regimes[day].position_multiplier == 1.0


def test_alpha_and_pullback_entry_score_positive_trend() -> None:
    rows = _bars(date(2024, 1, 1), 180)
    points = build_indicators(rows)
    idx = len(rows) - 1
    index_by_date = {row.date: row for row in rows}
    prior = {(rows[idx].date, 20): rows[idx - 20], (rows[idx].date, 60): rows[idx - 60]}
    alpha = score_alpha(rows, idx, points[idx], index_by_date, prior)
    entry = trend_pullback_entry(rows, idx, points[idx])
    assert alpha.total >= 50
    assert entry.score >= 40


def test_sector_heat_uses_visible_members_and_caps_bonus() -> None:
    rows = _bars(date(2024, 1, 1), 180, step=0.08)
    points = build_indicators(rows)
    day = rows[-1].date
    codes = [f"600{i:03d}" for i in range(30)]
    histories = {code: rows for code in codes}
    sector_map = {code: "软件服务" for code in codes}
    rows_by_date = {code: {row.date: row for row in rows} for code in codes}
    points_by_date = {code: {point.date: point for point in points} for code in codes}
    row_index_by_date = {code: {row.date: idx for idx, row in enumerate(rows)} for code in codes}
    heat = build_sector_heat(
        histories,
        sector_map,
        rows_by_date,
        points_by_date,
        row_index_by_date,
        {day: set(codes)},
        [day],
    )

    assert heat[day]["软件服务"].member_count == 30
    assert heat[day]["软件服务"].heat_score > 70
    assert 0 < sector_heat_bonus("600000", day, sector_map, heat) <= 6.0
