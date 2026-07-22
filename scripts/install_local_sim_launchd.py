from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv311" / "bin" / "python"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = ROOT / "reports" / "local_sim_launchd"
DOMAIN = f"gui/{subprocess.check_output(['id', '-u'], text=True).strip()}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Chan520 local simulated trading launchd jobs")
    parser.add_argument("--force", action="store_true", help="Replace changed Chan520 plist files")
    parser.add_argument("--no-bootstrap", action="store_true", help="Only write plist files")
    args = parser.parse_args()

    if not PYTHON.exists():
        raise SystemExit(f"missing Python runtime: {PYTHON}")
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for label, payload in build_agents(ROOT, LOG_DIR).items():
        path = LAUNCH_AGENTS / f"{label}.plist"
        content = plistlib.dumps(payload, sort_keys=False)
        action = write_plist(path, content, force=args.force)
        loaded = False
        if not args.no_bootstrap:
            loaded = ensure_loaded(label, path, reload=action == "updated")
            if label.endswith(".dashboard"):
                subprocess.run(
                    ["launchctl", "kickstart", "-k", f"{DOMAIN}/{label}"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
        results.append({"label": label, "path": str(path), "action": action, "loaded": loaded})

    print(json.dumps({"domain": DOMAIN, "jobs": results}, ensure_ascii=False, sort_keys=True))
    return 0


def build_agents(root: Path, log_dir: Path) -> dict[str, dict[str, Any]]:
    daily = root / "scripts" / "run_local_sim_daily.py"
    python = root / ".venv311" / "bin" / "python"
    common = {
        "WorkingDirectory": str(root),
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "TZ": "Asia/Shanghai",
        },
    }

    def scheduled(
        label: str, phase: str, times: list[tuple[int, int]], feishu: str, extra_args: list[str] | None = None
    ) -> dict[str, Any]:
        return {
            "Label": label,
            "ProgramArguments": [
                str(python),
                str(daily),
                "--phase",
                phase,
                "--trade-date",
                "auto",
                "--feishu",
                feishu,
                "--continue-on-error",
                *(extra_args or []),
            ],
            "StartCalendarInterval": weekday_intervals(times),
            "StandardOutPath": str(log_dir / f"{label}.stdout.log"),
            "StandardErrorPath": str(log_dir / f"{label}.stderr.log"),
            **common,
        }

    dashboard_label = "com.tonyyu.chan520.dashboard"
    plan_label = "com.tonyyu.chan520.plan"
    preopen_label = "com.tonyyu.chan520.preopen"
    intraday_label = "com.tonyyu.chan520.intraday"
    eod_label = "com.tonyyu.chan520.eod"
    return {
        dashboard_label: {
            "Label": dashboard_label,
            "ProgramArguments": [
                str(python),
                "-m",
                "http.server",
                "8768",
                "--bind",
                "127.0.0.1",
                "--directory",
                str(root / "web_dashboard"),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": str(log_dir / f"{dashboard_label}.stdout.log"),
            "StandardErrorPath": str(log_dir / f"{dashboard_label}.stderr.log"),
            **common,
        },
        plan_label: scheduled(plan_label, "plan", [(8, 0), (8, 15)], "send", ["--skip-if-plan-ready"]),
        preopen_label: scheduled(preopen_label, "preopen", [(9, 20)], "off"),
        intraday_label: scheduled(
            intraday_label,
            "intraday",
            [
                (9, 30), (9, 32), (9, 34), (9, 36), (9, 38), (9, 40), (9, 42), (9, 44),
                (9, 51), (10, 31), (11, 15), (13, 11), (14, 1), (14, 31), (14, 51),
            ],
            "send",
        ),
        eod_label: scheduled(eod_label, "eod", [(15, 20)], "send"),
    }


def weekday_intervals(times: list[tuple[int, int]]) -> list[dict[str, int]]:
    return [
        {"Weekday": weekday, "Hour": hour, "Minute": minute}
        for weekday in range(1, 6)
        for hour, minute in times
    ]


def write_plist(path: Path, content: bytes, *, force: bool) -> str:
    if path.exists():
        current = path.read_bytes()
        if current == content:
            return "unchanged"
        if not force:
            raise RuntimeError(f"refusing to overwrite changed launch agent without --force: {path}")
        path.write_bytes(content)
        return "updated"
    path.write_bytes(content)
    return "created"


def ensure_loaded(label: str, path: Path, *, reload: bool = False) -> bool:
    probe = subprocess.run(
        ["launchctl", "print", f"{DOMAIN}/{label}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0 and not reload:
        return True
    if probe.returncode == 0:
        unloaded = subprocess.run(
            ["launchctl", "bootout", f"{DOMAIN}/{label}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if unloaded.returncode != 0:
            raise RuntimeError(f"launchctl bootout failed for {label}: {unloaded.stderr.strip()}")
    loaded = subprocess.run(
        ["launchctl", "bootstrap", DOMAIN, str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if loaded.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed for {label}: {loaded.stderr.strip()}")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
