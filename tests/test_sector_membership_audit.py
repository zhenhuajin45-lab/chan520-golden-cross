from __future__ import annotations

from datetime import date

from chan520_skill.backtest import _write_sector_data_audit
from chan520_skill.sector_alpha import SectorAlpha


def test_sector_membership_audit_writes_mapping_counts(tmp_path) -> None:
    path = tmp_path / "sector_data_audit.md"
    heat = {
        date(2026, 1, 5): {
            "tech": SectorAlpha("tech", date(2026, 1, 5), 80.0, 30, 70.0, 80.0, 60.0, 65.0)
        }
    }

    _write_sector_data_audit(path, {"600001": "tech", "600002": ""}, heat)

    text = path.read_text(encoding="utf-8")
    assert "symbols_with_sector_label | 1" in text
    assert "daily_sector_heat_rows | 1" in text
    assert "sector_point_in_time: `false`" in text
    assert "mapped_static_end_date | 1" in text
