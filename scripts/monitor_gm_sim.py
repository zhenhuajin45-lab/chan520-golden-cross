from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.broker_adapter import (
    BrokerOrderRequest,
    BrokerPreflight,
    BrokerSide,
    GMSimBrokerAdapter,
    GMSimulationConfig,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect guarded GM simulation monitor evidence")
    parser.add_argument("--phase", required=True, choices=["preopen", "intraday", "eod", "manual"])
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--config", default=str(ROOT / "config" / "local_gm_sim.json"))
    parser.add_argument("--output-root", default=str(ROOT / "reports" / "gm_sim_monitor"))
    args = parser.parse_args()

    config_path = Path(args.config)
    output_dir = Path(args.output_root) / args.trade_date.replace("-", "")
    output_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    cfg = _read_json(config_path)
    snapshot = {
        "generated_at": now.isoformat(timespec="seconds"),
        "phase": args.phase,
        "trade_date": args.trade_date,
        "repo": str(ROOT),
        "config_path": str(config_path),
        "config_present": config_path.exists(),
        "account_id_present": bool(str(cfg.get("account_id", "") or "").strip()),
        "account_id_masked": _mask_id(str(cfg.get("account_id", "") or "")),
        "account_type": str(cfg.get("account_type", "") or ""),
        "enable_submit": bool(cfg.get("enable_submit", False)),
        "dry_run": bool(cfg.get("dry_run", True)),
        "token_env": str(cfg.get("token_env", "GM_TOKEN") or "GM_TOKEN"),
        "token_present": bool(os.environ.get(str(cfg.get("token_env", "GM_TOKEN") or "GM_TOKEN"), "").strip()),
        "git": _git_snapshot(),
        "gm_sdk": _gm_sdk_snapshot(),
        "broker_guard_probe": _broker_guard_probe(cfg, args.trade_date),
        "shadow_readiness": _shadow_readiness_snapshot(),
        "notes": [
            "monitor_only",
            "does_not_call_order_volume",
            "does_not_mark_shadow_readiness_true",
        ],
    }

    stamp = now.strftime("%H%M%S")
    snapshot_path = output_dir / f"snapshot_{args.phase}_{stamp}.json"
    latest_path = output_dir / "latest.json"
    event_path = output_dir / "monitor_events.jsonl"
    _write_json(snapshot_path, snapshot)
    _write_json(latest_path, snapshot)
    with event_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"ok": True, "snapshot": str(snapshot_path), "latest": str(latest_path)}, ensure_ascii=False), flush=True)
    return 0


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _git_snapshot() -> dict[str, Any]:
    return {
        "branch": _run_git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "commit": _run_git(["rev-parse", "HEAD"]),
        "dirty": bool(_run_git(["status", "--porcelain"])),
    }


def _run_git(args: list[str]) -> str:
    completed = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _gm_sdk_snapshot() -> dict[str, Any]:
    try:
        import gm.api as gm_api  # type: ignore
        import gm.enum as gm_enum  # type: ignore

        return {
            "available": True,
            "api_file": str(getattr(gm_api, "__file__", "")),
            "enum_file": str(getattr(gm_enum, "__file__", "")),
            "has_order_volume": hasattr(gm_api, "order_volume"),
            "has_set_account_id": hasattr(gm_api, "set_account_id"),
            "has_set_token": hasattr(gm_api, "set_token"),
        }
    except Exception as exc:
        return {"available": False, "error_type": type(exc).__name__, "error": str(exc)}


def _broker_guard_probe(cfg: dict[str, Any], trade_date: str) -> dict[str, Any]:
    sim_cfg = GMSimulationConfig(
        account_id=str(cfg.get("account_id", "") or ""),
        account_type=str(cfg.get("account_type", "SIMULATION") or "SIMULATION"),
        token_env=str(cfg.get("token_env", "GM_TOKEN") or "GM_TOKEN"),
        enable_submit=bool(cfg.get("enable_submit", False)),
        dry_run=bool(cfg.get("dry_run", True)),
    )
    request = BrokerOrderRequest(
        symbol="SHSE.600288",
        side=BrokerSide.BUY,
        volume=100,
        price=1.0,
        order_intent_id="monitor_probe",
        run_id="monitor_only",
        session_date=trade_date,
    )
    preflight = BrokerPreflight(identity_pass=False, data_gate_pass=False, reconciliation_pass=False)
    result = GMSimBrokerAdapter(sim_cfg).submit_order(request, preflight)
    payload = result.as_payload()
    payload["expected_submit"] = False
    return payload


def _shadow_readiness_snapshot() -> dict[str, Any]:
    path = ROOT / "reports" / "paper" / "readiness" / "true_incremental_parity.json"
    if not path.exists():
        return {"file_present": False, "shadow_readiness": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"file_present": True, "shadow_readiness": False, "error_type": type(exc).__name__}
    return {
        "file_present": True,
        "shadow_readiness": bool(payload.get("shadow_readiness", False)),
        "status": str(payload.get("status", "")),
        "reason": str(payload.get("reason", "")),
    }


def _mask_id(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


if __name__ == "__main__":
    raise SystemExit(main())
