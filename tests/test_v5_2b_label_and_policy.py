from __future__ import annotations

from datetime import date, timedelta

from chan520_skill.backtest import rank_candidate_signals_by_policy
from scripts.selection_attribution import build_labels, label_window

from .v5_1_helpers import make_candidate


def _bar(day: date, price: float) -> dict:
    return {
        "date": day,
        "open": price,
        "close": price + 0.5,
        "high": price + 1.0,
        "low": price - 0.5,
        "amount": 1_000_000.0,
        "turnover": 1.0,
    }


def test_label_window_uses_nth_holding_day_close() -> None:
    calendar = [date(2026, 1, 5) + timedelta(days=idx) for idx in range(8)]
    index = {day: idx for idx, day in enumerate(calendar)}

    quality, horizon, days = label_window(calendar[1], 5, calendar, index)

    assert quality == "ok"
    assert horizon == calendar[5]
    assert days == calendar[1:6]


def test_build_labels_censors_incomplete_horizon_without_short_substitution() -> None:
    days = [date(2026, 1, 5) + timedelta(days=idx) for idx in range(7)]
    candidate = make_candidate("600001")
    row = {
        "candidate_id": candidate.candidate_id,
        "date": days[0].isoformat(),
        "code": candidate.code,
        "industry": candidate.industry,
        "ranking_score": "80",
        "alpha_total": "80",
        "entry_score": "70",
        "trend_score": "30",
        "relative_strength_score": "10",
        "volume_quality_score": "15",
        "risk_score": "20",
        "sector_heat_score": "0",
        "planned_stop": "9",
        "planned_target": "13",
        "ex_ante_rr": "3",
    }
    bars = {"600001": [_bar(day, 10 + idx) for idx, day in enumerate(days[:4])]}
    index_rows = [_bar(day, 100 + idx) for idx, day in enumerate(days)]

    labels = build_labels(
        {candidate.candidate_id: row},
        bars,
        index_rows,
        {"600001": "tech"},
        {},
        days,
    )

    label = labels[0]
    assert label["entry_reference_date"] == days[1].isoformat()
    assert label["horizon_date_3d"] == days[3].isoformat()
    assert label["data_complete_3d"] == 1
    assert label["forward_return_3d"] != ""
    assert label["data_complete_5d"] == 0
    assert label["forward_return_5d"] == ""
    assert label["label_quality_code_5d"] == "missing_horizon_bar"


def test_random_within_ties_preserves_bucket_order_and_is_seeded() -> None:
    day = date(2026, 1, 5)
    candidates = [
        make_candidate("600001", ranking=90, entry=80, rs=20),
        make_candidate("600002", ranking=80, entry=70, rs=10),
        make_candidate("600003", ranking=80, entry=70, rs=10),
    ]

    first = rank_candidate_signals_by_policy(candidates, day=day, policy="RANDOM_WITHIN_TIES", seed=7)
    second = rank_candidate_signals_by_policy(candidates, day=day, policy="RANDOM_WITHIN_TIES", seed=7)

    assert [item.code for item in first] == [item.code for item in second]
    assert first[0].code == "600001"
    assert {item.code for item in first[1:]} == {"600002", "600003"}
