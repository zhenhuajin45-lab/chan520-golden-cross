from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from .models import KLine


@dataclass(frozen=True)
class InstrumentStatus:
    code: str
    name: str
    listed_date: date | None
    delisted_date: date | None
    is_suspended: bool = False


def build_dynamic_universe(
    histories: dict[str, list[KLine]],
    statuses_by_date: dict[date, dict[str, InstrumentStatus]],
    all_dates: list[date],
    min_history_bars: int = 250,
    min_avg_amount: float = 300_000_000.0,
    liquidity_window: int = 20,
) -> dict[date, set[str]]:
    """Build a point-in-time universe using only information known by each day."""
    wanted = set(all_dates)
    output: dict[date, set[str]] = {day: set() for day in all_dates}
    for code, rows in histories.items():
        rolling_amount = 0.0
        for idx, row in enumerate(rows):
            rolling_amount += row.amount
            if idx >= liquidity_window:
                rolling_amount -= rows[idx - liquidity_window].amount
            if row.date not in wanted or idx + 1 < min_history_bars or idx + 1 < liquidity_window:
                continue
            status = statuses_by_date.get(row.date, {}).get(code)
            if status is None or not _status_ok(status, row.date):
                continue
            avg_amount = rolling_amount / liquidity_window
            if avg_amount < min_avg_amount:
                continue
            output[row.date].add(code)
    return output


def static_universe_statuses(
    histories: dict[str, list[KLine]], names: dict[str, str] | None = None
) -> dict[date, dict[str, InstrumentStatus]]:
    """Fallback statuses when a data vendor does not provide daily metadata."""
    names = names or {}
    all_dates = sorted({row.date for rows in histories.values() for row in rows})
    first_date = {code: rows[0].date for code, rows in histories.items() if rows}
    output: dict[date, dict[str, InstrumentStatus]] = {}
    for day in all_dates:
        output[day] = {
            code: InstrumentStatus(code, names.get(code, code), first_date.get(code), None, False)
            for code, rows in histories.items()
            if rows and rows[0].date <= day <= rows[-1].date
        }
    return output


def _status_ok(status: InstrumentStatus, day: date) -> bool:
    if status.listed_date is not None and status.listed_date > day:
        return False
    if status.delisted_date is not None and status.delisted_date < day:
        return False
    if status.is_suspended:
        return False
    upper_name = status.name.upper()
    return "ST" not in upper_name and "退" not in status.name

