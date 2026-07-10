from __future__ import annotations

from datetime import date

import pytest

from chan520_skill.universe import industry_map, load_universe_snapshot


def test_snapshot_uses_exact_historical_date_and_excludes_ineligible(tmp_path) -> None:
    path = tmp_path / "universe.csv"
    path.write_text(
        "as_of,code,name,industry,eligible\n"
        "2024-01-01,600288,大恒科技,软件,true\n"
        "2024-01-01,000001,平安银行,银行,false\n"
        "2024-02-01,000001,平安银行,银行,true\n",
        encoding="utf-8",
    )
    members = load_universe_snapshot(path, date(2024, 1, 1))
    assert [member.code for member in members] == ["600288"]
    assert industry_map(members) == {"600288": "软件"}


def test_snapshot_requires_industry_for_point_in_time_risk_control(tmp_path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("as_of,code,name\n2024-01-01,600288,大恒科技\n", encoding="utf-8")
    with pytest.raises(ValueError, match="industry"):
        load_universe_snapshot(path, date(2024, 1, 1))
