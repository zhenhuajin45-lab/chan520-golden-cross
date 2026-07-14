from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.evidence_manifest import stable_hash_json


REQUIRED_TRUE_INCREMENTAL_CAPABILITIES = (
    "open_phase_generates_fills_without_batch_payload",
    "close_phase_generates_pending_orders_without_batch_payload",
    "single_day_cli_uses_only_data_lte_session_date",
    "state_identity_survives_different_session_dates",
    "future_data_access_guard_enforced",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run true incremental shadow parity in isolated processes")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--paper-store", default="tmp/true_incremental_shadow.sqlite")
    parser.add_argument("--output", default="reports/paper/true_incremental_parity.json")
    parser.add_argument("--allow-prototype", action="store_true")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        raise SystemExit("--end must be >= --start")

    report = {
        "status": "FAIL_CLOSED",
        "reason": "TRUE_INCREMENTAL_KERNEL_NOT_EXTRACTED",
        "shadow_readiness": False,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "paper_store": args.paper_store,
        "required_capabilities": list(REQUIRED_TRUE_INCREMENTAL_CAPABILITIES),
        "command_hash": stable_hash_json(
            {
                "script": "scripts/run_true_incremental_parity.py",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "paper_store": args.paper_store,
            }
        ),
    }
    if not args.allow_prototype:
        write_report(Path(args.output), report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return 2

    report["status"] = "PROTOTYPE_MODE_REJECTED"
    report["reason"] = "allow_prototype is intentionally unsupported for shadow readiness"
    write_report(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 2


def run_subprocess(command: list[str], *, cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
