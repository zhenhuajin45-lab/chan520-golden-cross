from __future__ import annotations

from scripts.install_local_sim_launchd import build_agents


def test_launchd_agents_cover_dashboard_and_daily_phases(tmp_path):
    agents = build_agents(tmp_path, tmp_path / "logs")

    assert set(agents) == {
        "com.tonyyu.chan520.dashboard",
        "com.tonyyu.chan520.plan",
        "com.tonyyu.chan520.preopen",
        "com.tonyyu.chan520.intraday",
        "com.tonyyu.chan520.eod",
    }
    assert agents["com.tonyyu.chan520.dashboard"]["KeepAlive"] is True
    assert "8768" in agents["com.tonyyu.chan520.dashboard"]["ProgramArguments"]
    assert len(agents["com.tonyyu.chan520.plan"]["StartCalendarInterval"]) == 5
    assert len(agents["com.tonyyu.chan520.intraday"]["StartCalendarInterval"]) == 75
    opening = {
        (item["Hour"], item["Minute"])
        for item in agents["com.tonyyu.chan520.intraday"]["StartCalendarInterval"]
        if item["Weekday"] == 1 and item["Hour"] == 9 and item["Minute"] <= 44
    }
    assert opening == {(9, minute) for minute in range(30, 45, 2)}
    assert agents["com.tonyyu.chan520.preopen"]["ProgramArguments"][-2:] == ["off", "--continue-on-error"]
    assert agents["com.tonyyu.chan520.eod"]["ProgramArguments"][-2:] == ["send", "--continue-on-error"]
