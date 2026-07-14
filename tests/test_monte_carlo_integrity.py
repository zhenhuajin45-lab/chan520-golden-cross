from __future__ import annotations

import csv

import pytest

from chan520_skill.backtest import SelectionPolicy
from scripts.run_execution_faithful_monte_carlo import (
    _checkpoint_counts,
    _load_and_validate_rows,
    _p_ge,
    distribution_integrity,
    validate_distribution_rows,
)


def _row(policy: str, seed: int, context_hash: str) -> dict[str, object]:
    return {
        "policy": policy,
        "seed": seed,
        "prepared_context_hash": context_hash,
        "kernel_run_hash": f"kernel-{policy}-{seed}",
        "selected_set_hash": f"selected-{seed}",
        "fills_economic_hash": f"fills-{seed}",
        "trades_economic_hash": f"trades-{seed}",
        "selected_count": 1,
        "selected_overlap_with_ranked": 0.0,
        "total_return": 0.0,
        "cagr": 0.0,
        "max_drawdown": 0.0,
        "sharpe": 0.0,
        "calmar": 0.0,
        "profit_factor": 0.0,
        "win_rate": 0.0,
        "payoff_ratio": 0.0,
        "trade_count": 0.0,
        "fill_count": 0.0,
        "turnover": 0.0,
        "exposure": 0.0,
        "avg_exposure": 0.0,
        "max_exposure": 0.0,
        "elapsed_seconds": 0.1,
    }


def _write_distribution(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_checkpoint_preserves_random_and_tie_counts(tmp_path):
    context_hash = "ctx"
    _write_distribution(tmp_path / "random_distribution.csv", [_row(SelectionPolicy.RANDOM.value, i, context_hash) for i in range(3)])
    _write_distribution(tmp_path / "tie_distribution.csv", [_row(SelectionPolicy.RANDOM_WITHIN_TIES.value, i, context_hash) for i in range(2)])

    counts = _checkpoint_counts(tmp_path, 12.5)

    assert counts["completed_random"] == 3
    assert counts["completed_tie"] == 2


def test_resume_rejects_context_mismatch(tmp_path):
    path = tmp_path / "random_distribution.csv"
    _write_distribution(path, [_row(SelectionPolicy.RANDOM.value, 0, "old")])

    with pytest.raises(SystemExit):
        _load_and_validate_rows(path, SelectionPolicy.RANDOM, 1, "new")


def test_resume_deduplicates_seed():
    rows = [_row(SelectionPolicy.RANDOM.value, 0, "ctx"), _row(SelectionPolicy.RANDOM.value, 0, "ctx")]

    deduped, integrity = validate_distribution_rows(rows, SelectionPolicy.RANDOM, 1, "ctx")

    assert len(deduped) == 1
    assert integrity["duplicate_seed_count"] == 1
    assert integrity["missing_seed_count"] == 0


def test_empirical_pvalue_has_finite_sample_correction():
    assert _p_ge([1.0, 2.0, 3.0], 10.0) == pytest.approx(0.25)
    assert _p_ge([1.0, 2.0, 3.0], 2.0) == pytest.approx(0.75)


def test_distribution_seed_coverage():
    rows = [_row(SelectionPolicy.RANDOM.value, i, "ctx") for i in range(5)]

    integrity = distribution_integrity(rows, SelectionPolicy.RANDOM, 5, "ctx")

    assert integrity["missing_seed_count"] == 0
    assert integrity["duplicate_seed_count"] == 0
    assert integrity["out_of_range_seed_count"] == 0
    assert integrity["context_mismatch_count"] == 0
    assert integrity["seed_min"] == 0
    assert integrity["seed_max"] == 4
