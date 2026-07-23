from __future__ import annotations

from datetime import date

import pytest

from chan520_skill import scanner
from chan520_skill.models import KLine, StockMeta


def bar(day: date, close: float) -> KLine:
    return KLine(day, close, close, close + 0.1, close - 0.1, 1000, 0, 0, 0, 0, 0)


def test_hybrid_history_appends_exact_day_only_when_boundary_matches(monkeypatch):
    stock = scanner.UniverseStock("600001", "示例", 1)
    qfq = [bar(date(2026, 7, 22), 10.0)]
    raw = [bar(date(2026, 7, 22), 10.0), bar(date(2026, 7, 23), 10.5)]
    monkeypatch.setattr(scanner, "sina_history", lambda *_args, **_kwargs: (StockMeta("600001", "示例", 1), raw))

    rows = scanner._append_sina_exact_to_qfq(stock, qfq, date(2026, 7, 23))

    assert [row.date for row in rows] == [date(2026, 7, 22), date(2026, 7, 23)]
    assert rows[-1].pct_chg == pytest.approx(5.0)


def test_hybrid_history_rejects_possible_corporate_action_boundary(monkeypatch):
    stock = scanner.UniverseStock("600001", "示例", 1)
    qfq = [bar(date(2026, 7, 22), 9.5)]
    raw = [bar(date(2026, 7, 22), 10.0), bar(date(2026, 7, 23), 10.5)]
    monkeypatch.setattr(scanner, "sina_history", lambda *_args, **_kwargs: (StockMeta("600001", "示例", 1), raw))

    with pytest.raises(scanner.DataError, match="price boundary mismatch"):
        scanner._append_sina_exact_to_qfq(stock, qfq, date(2026, 7, 23))
