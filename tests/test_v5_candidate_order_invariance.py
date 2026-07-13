from __future__ import annotations

import random

from chan520_skill.backtest import rank_candidate_signals

from .v5_1_helpers import make_candidate


def test_v5_candidate_order_invariance_for_ranked_selection() -> None:
    base = [
        make_candidate("600001", ranking=81, entry=70, rs=5),
        make_candidate("600002", ranking=83, entry=65, rs=8),
        make_candidate("600003", ranking=83, entry=65, rs=7),
        make_candidate("600004", ranking=79, entry=90, rs=9),
    ]
    expected = [item.code for item in rank_candidate_signals(base)]

    for seed in (520, 2026, 42):
        shuffled = list(base)
        random.Random(seed).shuffle(shuffled)
        assert [item.code for item in rank_candidate_signals(shuffled)] == expected

    assert [item.code for item in rank_candidate_signals(list(reversed(base)))] == expected
