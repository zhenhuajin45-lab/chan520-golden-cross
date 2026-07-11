from __future__ import annotations

from chan520_skill.backtest import PendingOrder, _candidate_audit_row

from .v5_1_helpers import make_candidate


def test_trade_signal_snapshot_keeps_scores_and_risk_plan() -> None:
    signal = make_candidate("600001", ranking=88.0, entry=76.0, rs=12.0)
    order = PendingOrder("600001", "buy", "strategy_v5_alpha_ranked_entry", shares=1000, stop=9.5, target=12.5, rr=2.5)

    row = _candidate_audit_row(signal, 1, True, "", order)

    assert row["selected"] == 1
    assert row["ranking_score"] == "88.000000"
    assert row["entry_score"] == "76.000000"
    assert row["shares"] == 1000
    assert row["stop"] == "9.500000"
    assert row["target"] == "12.500000"
