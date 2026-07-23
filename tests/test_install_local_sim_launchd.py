from __future__ import annotations

from scripts.install_local_sim_launchd import build_agents, intraday_times


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
    assert len(agents["com.tonyyu.chan520.plan"]["StartCalendarInterval"]) == 10
    assert "--skip-if-plan-ready" in agents["com.tonyyu.chan520.plan"]["ProgramArguments"]
    assert intraday_times()[:3] == [(9, 30), (9, 32), (9, 34)]
    assert intraday_times()[-3:] == [(14, 54), (14, 56), (14, 58)]
    assert len(intraday_times()) == 120
    assert len(agents["com.tonyyu.chan520.intraday"]["StartCalendarInterval"]) == 600
    opening = {
        (item["Hour"], item["Minute"])
        for item in agents["com.tonyyu.chan520.intraday"]["StartCalendarInterval"]
        if item["Weekday"] == 1 and item["Hour"] == 9
    }
    assert opening == {(9, minute) for minute in range(30, 60, 2)}
    assert agents["com.tonyyu.chan520.preopen"]["ProgramArguments"][-2:] == ["off", "--continue-on-error"]
    assert agents["com.tonyyu.chan520.eod"]["ProgramArguments"][-2:] == ["send", "--continue-on-error"]
    eod_times = {
        (item["Hour"], item["Minute"])
        for item in agents["com.tonyyu.chan520.eod"]["StartCalendarInterval"]
        if item["Weekday"] == 1
    }
    assert eod_times == {(15, 20), (18, 0)}
