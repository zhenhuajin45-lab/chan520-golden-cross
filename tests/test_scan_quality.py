from __future__ import annotations

from chan520_skill.scan_quality import normalize_scan_quality


def test_research_coverage_does_not_mask_low_adjusted_execution_coverage():
    quality = normalize_scan_quality(
        {
            "universe": 4994,
            "success": 4975,
            "history_source_counts": {
                "sina_unadjusted": 4909,
                "tencent_qfq": 66,
            },
        }
    )

    assert quality["coverage_pass"] is True
    assert quality["research_coverage"] > 0.99
    assert quality["adjusted_success"] == 66
    assert quality["execution_coverage"] < 0.02
    assert quality["execution_coverage_pass"] is False


def test_adjusted_sources_can_pass_both_quality_gates():
    quality = normalize_scan_quality(
        {
            "universe": 100,
            "success": 95,
            "history_source_counts": {
                "tencent_qfq": 60,
                "eastmoney_qfq": 35,
            },
        }
    )

    assert quality["coverage_pass"] is True
    assert quality["execution_coverage_pass"] is True
    assert quality["unadjusted_success"] == 0
