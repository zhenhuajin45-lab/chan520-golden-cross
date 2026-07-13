from __future__ import annotations

from datetime import date

from chan520_skill.backtest import Fill, Trade, opening_gap_parts


def test_lifecycle_ids_link_candidate_order_fill_position_trade() -> None:
    fill = Fill(
        date=date(2026, 1, 6),
        code="600001",
        side="buy",
        price=10.0,
        shares=100,
        fee=5.0,
        reason="entry",
        signal_date=date(2026, 1, 5),
        fill_id="fill_1",
        candidate_id="cand_1",
        order_intent_id="intent_1",
        pending_order_id="pend_1",
        requested_shares=100,
        allowed_at_open_shares=100,
        signal_close=9.8,
        next_open=10.0,
        opening_gap=0.020408,
    )
    trade = Trade(
        code="600001",
        name="sample",
        entry_date=date(2026, 1, 6),
        exit_date=date(2026, 1, 20),
        entry_price=10.0,
        exit_price=12.0,
        shares=100,
        gross_pnl=200.0,
        costs=10.0,
        net_pnl=190.0,
        holding_days=14,
        entry_reason="entry",
        exit_reason="target",
        trade_id="trade_1",
        position_id="pos_1",
        entry_fill_id=fill.fill_id,
        candidate_id=fill.candidate_id,
        order_intent_id=fill.order_intent_id,
        pending_order_id=fill.pending_order_id,
        signal_close=fill.signal_close,
        next_open=fill.next_open,
        opening_gap=fill.opening_gap,
        planned_stop=9.0,
        planned_target=13.0,
        ex_ante_rr=3.0,
        initial_risk_cash=100.0,
        realized_r_multiple=1.9,
    )
    assert trade.candidate_id == fill.candidate_id
    assert trade.order_intent_id == fill.order_intent_id
    assert trade.pending_order_id == fill.pending_order_id
    assert trade.entry_fill_id == fill.fill_id
    assert trade.realized_r_multiple == trade.net_pnl / trade.initial_risk_cash


def test_opening_gap_parts_split_slippage_from_raw_gap() -> None:
    raw_gap, execution_slippage, all_in_entry_move = opening_gap_parts(10.0, 10.0, 10.005)

    assert raw_gap == 0.0
    assert round(execution_slippage, 6) == 0.0005
    assert round(all_in_entry_move, 6) == 0.0005
