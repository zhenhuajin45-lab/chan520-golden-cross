# Audit Schema Migration

- audit_schema_version: `2`
- scope: `V8/V9 research artifacts -> V10 prepared-kernel foundation artifacts`
- economic compatibility: `PASS`

## Summary

V10 preserves the economic execution contract used for parity:

- selected candidates
- pending orders
- economic fills
- economic trades
- daily equity

V10 changes the audit/reporting surface for candidate funnel and discipline
summaries because the prepared kernel now uses pre-indexed daily candidates and
SUMMARY/CORE can skip full audit materialization. These are non-economic
research-evidence changes and do not change order selection, fills, trades, or
equity.

## V8 Funnel/Discipline

V8 funnel and discipline artifacts were emitted from the full artifact path on
every run. They mixed research-gate counts, portfolio-selection counts, and
runtime discipline summaries from a fully materialized audit stream.

## V10 Funnel/Discipline

V10 separates:

- research gate candidate evidence prepared once in `PreparedBacktestContext`
- portfolio selection and fills in `execute_prepared_context`
- SUMMARY/CORE metrics without full audit rows or bootstrap
- economic hashes separate from full evidence hashes

The full-market golden parity run therefore treats funnel/discipline hash
differences as audit-schema differences, while selected candidates, pending
orders, economic fills, economic trades, daily equity, and frozen metrics remain
hard parity requirements.

## Invariants

The following invariants must hold across schema versions:

- selected candidate IDs match
- pending order economic fields match
- fill economic fields match: date, code, side, price, shares, fee
- trade economic fields match: code, entry/exit dates, prices, shares, net PnL, exit reason
- daily equity curve matches
- frozen headline metrics match
- selected-to-fill-to-trade lifecycle remains attributable

The following differences are allowed under audit schema version `2`:

- candidate funnel file hash may differ when count definitions are split
- discipline markdown hash may differ when generated from the V10 metrics writer
- evidence hashes may include additional non-economic fields

## Reconciliation

V10 full-market parity records both classes explicitly:

- `economic_parity: PASS`
- `non_economic_audit_parity: FAIL` when funnel/discipline hashes differ

This prevents audit schema churn from being confused with execution drift.
