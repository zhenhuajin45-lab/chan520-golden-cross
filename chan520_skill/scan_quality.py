from __future__ import annotations

from collections import Counter
from typing import Any, Iterable


DEFAULT_MINIMUM_COVERAGE = 0.85
UNADJUSTED_HISTORY_SOURCES = frozenset({"sina_unadjusted"})


def execution_history_adjusted(source: Any) -> bool:
    value = str(source or "legacy_adjusted_unknown").strip()
    return value not in UNADJUSTED_HISTORY_SOURCES


def normalize_scan_quality(
    quality: dict[str, Any] | None,
    *,
    rows: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = dict(quality or {})
    materialized = list(rows) if rows is not None else None
    counts = _source_counts(payload, materialized)
    universe = _positive_int(payload.get("universe") or payload.get("universe_count") or payload.get("expected_count"))
    research_success = _positive_int(
        payload.get("success")
        or payload.get("rows")
        or payload.get("successful_count")
        or payload.get("usable_count")
    )
    supplied_population = universe > 0 or research_success > 0 or isinstance(payload.get("history_source_counts"), dict)
    supplied_coverage = _optional_float(payload.get("coverage"))
    if research_success == 0 and counts:
        research_success = sum(counts.values())
    if research_success == 0 and materialized is not None:
        research_success = len(materialized)
    if universe == 0:
        universe = research_success

    adjusted_success = sum(count for source, count in counts.items() if execution_history_adjusted(source))
    if not counts and payload.get("execution_coverage_pass") is None:
        adjusted_success = research_success if payload.get("coverage_pass") is True else 0

    minimum_research = _positive_float(payload.get("minimum_coverage"), DEFAULT_MINIMUM_COVERAGE)
    minimum_execution = _positive_float(payload.get("minimum_execution_coverage"), minimum_research)
    research_coverage = (
        supplied_coverage
        if supplied_coverage is not None and not supplied_population
        else research_success / universe if universe else 0.0
    )
    execution_coverage = adjusted_success / universe if universe else 0.0
    if supplied_coverage is not None and not supplied_population and payload.get("coverage_pass") is not True:
        execution_coverage = 0.0

    return {
        **payload,
        "history_source_counts": dict(sorted(counts.items())),
        "universe": universe,
        "success": research_success,
        "rows": _positive_int(payload.get("rows")) or research_success,
        "coverage": research_coverage,
        "research_coverage": research_coverage,
        "minimum_coverage": minimum_research,
        "coverage_pass": research_coverage >= minimum_research,
        "research_coverage_pass": research_coverage >= minimum_research,
        "adjusted_success": adjusted_success,
        "unadjusted_success": max(research_success - adjusted_success, 0),
        "execution_coverage": execution_coverage,
        "minimum_execution_coverage": minimum_execution,
        "execution_coverage_pass": execution_coverage >= minimum_execution,
        "quality_semantics": "research_coverage_and_adjusted_execution_coverage_v1",
    }


def _source_counts(
    payload: dict[str, Any],
    rows: list[dict[str, Any]] | None,
) -> Counter[str]:
    supplied = payload.get("history_source_counts")
    if isinstance(supplied, dict):
        return Counter(
            {
                str(source or "legacy_adjusted_unknown"): _positive_int(count)
                for source, count in supplied.items()
                if _positive_int(count) > 0
            }
        )
    if rows is None:
        return Counter()
    return Counter(str(row.get("history_source") or "legacy_adjusted_unknown") for row in rows)


def _positive_int(value: Any) -> int:
    try:
        return max(int(float(value or 0)), 0)
    except (TypeError, ValueError):
        return 0


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
