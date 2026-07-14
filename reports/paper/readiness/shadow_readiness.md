# Paper Ledger Prototype Readiness

```json
{
  "acceptance_scope": "prototype_ledger_not_shadow_ready",
  "authoritative_state_restore": true,
  "batch_event_persistence_parity": true,
  "batch_reported_equity_persistence_parity": true,
  "ci_local": "UNVERIFIED_IN_PROTOTYPE",
  "d_d1_clock": "UNVERIFIED_IN_PROTOTYPE",
  "duplicate_id_count_zero": true,
  "fail_closed_data_gate": true,
  "idempotency": true,
  "orphan_count_zero": true,
  "paper_ledger_prototype_readiness": true,
  "process_session_open_close_non_noop": true,
  "shadow_readiness": false,
  "synthetic_pending_order_count": 3,
  "synthetic_pending_zero": false,
  "table_counts": {
    "candidate_snapshots": 4,
    "data_snapshots": 5,
    "equity_snapshots": 5,
    "fills": 7,
    "ledger_events": 15,
    "order_intents": 8,
    "paper_runs": 1,
    "paper_session_attempts": 0,
    "paper_sessions": 10,
    "pending_orders": 8,
    "portfolio_state_snapshots": 10,
    "position_fill_links": 7,
    "positions": 7,
    "reconciliation_results": 1,
    "trades": 3
  },
  "transaction_rollback_probe": true
}
```
