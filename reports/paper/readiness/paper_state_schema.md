# Paper State Schema

- paper_state_version: `2`
- audit_schema_version: `2`
- storage: SQLite
- write contract: transaction-scoped writes with primary keys for session phase, candidate, order, fill, position link, trade, equity, reconciliation, and data snapshots.

| Table | Purpose |
|---|---|
| `paper_runs` | cohort and config identity |
| `paper_sessions` | open/close/reconcile phase status, state hashes, and idempotency |
| `portfolio_state_snapshots` | authoritative portfolio state after each committed phase |
| `ledger_events` | phase-scoped event stream |
| `candidate_snapshots` | ranked candidate evidence |
| `order_intents` | candidate to order lifecycle |
| `pending_orders` | D+1 order state |
| `fills` | execution-faithful fills |
| `positions` | position snapshots |
| `position_fill_links` | fill to position lifecycle |
| `trades` | closed trade lifecycle |
| `equity_snapshots` | cash/equity/account equation |
| `reconciliation_results` | daily reconciliation checks |
| `data_snapshots` | fail-closed data gate evidence |
