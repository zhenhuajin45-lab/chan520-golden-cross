from __future__ import annotations

from chan520_skill.backtest import rank_candidate_signals

from .v5_1_helpers import make_candidate


def test_v5_candidate_ranking_uses_score_then_code_tie_break() -> None:
    candidates = [
        make_candidate("600003", ranking=80, entry=80, rs=8),
        make_candidate("600002", ranking=82, entry=60, rs=4),
        make_candidate("600001", ranking=80, entry=80, rs=8),
    ]

    ranked = rank_candidate_signals(candidates)

    assert [item.code for item in ranked] == ["600002", "600001", "600003"]
