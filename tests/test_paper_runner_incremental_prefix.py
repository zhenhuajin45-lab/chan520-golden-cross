from __future__ import annotations

import sys
from argparse import Namespace
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from chan520_skill.incremental_guard import DataAccessGuard, FutureDataAccessViolation
from paper_runner import assert_incremental_data_not_future, single_session_context_end


@dataclass(frozen=True)
class Row:
    date: date
    code: str = "600288"


def test_single_session_context_end_defaults_to_configured_end():
    args = Namespace(end="2026-07-09", incremental_prefix=False)

    assert single_session_context_end(args, date(2026, 1, 6)) == date(2026, 7, 9)


def test_single_session_context_end_incremental_prefix_caps_at_session_date():
    args = Namespace(end="2026-07-09", incremental_prefix=True)

    assert single_session_context_end(args, date(2026, 1, 6)) == date(2026, 1, 6)


def test_single_session_context_end_rejects_date_after_configured_end():
    args = Namespace(end="2026-01-05", incremental_prefix=True)

    with pytest.raises(SystemExit):
        single_session_context_end(args, date(2026, 1, 6))


def test_incremental_data_guard_rejects_future_daily_bar():
    data = SimpleNamespace(
        rows_by_code={"600288": [Row(date(2026, 1, 7))]},
        index_rows=[],
        eligible_by_date={date(2026, 1, 6): {"600288"}},
    )
    context = SimpleNamespace(all_dates=[date(2026, 1, 6)])
    guard = DataAccessGuard(date(2026, 1, 6))

    with pytest.raises(FutureDataAccessViolation):
        assert_incremental_data_not_future(data, context, {}, guard)


def test_incremental_data_guard_rejects_future_status_snapshot():
    data = SimpleNamespace(
        rows_by_code={"600288": [Row(date(2026, 1, 6))]},
        index_rows=[],
        eligible_by_date={date(2026, 1, 6): {"600288"}},
    )
    context = SimpleNamespace(all_dates=[date(2026, 1, 6)])
    status_by_date = {
        date(2026, 1, 7): {
            "600288": {
                "trade_date": "2026-01-07",
                "code": "600288",
            }
        }
    }
    guard = DataAccessGuard(date(2026, 1, 6))

    with pytest.raises(FutureDataAccessViolation):
        assert_incremental_data_not_future(data, context, status_by_date, guard)
