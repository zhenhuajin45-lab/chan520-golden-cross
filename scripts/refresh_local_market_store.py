from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.market_store import DEFAULT_PATH, initialize, load_scan, stats, upsert_scan  # noqa: E402
from chan520_skill.scanner import scan_market  # noqa: E402
from scripts.generate_local_sim_core_plan import (  # noqa: E402
    read_scan,
    prefer_qualified_scan_evidence,
    resolve_market_regime,
    resolve_scan_quality,
    resolve_sector_map,
)


TZ = ZoneInfo("Asia/Shanghai")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the exact-date local market-data fallback store")
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--scan-if-missing", action="store_true")
    parser.add_argument("--scan-output-dir", default="", help="Write a recovery scan outside the standard scan directory")
    args = parser.parse_args()

    target = date.fromisoformat(args.trade_date)
    initialize(DEFAULT_PATH)
    scan_dir = (
        Path(args.scan_output_dir).expanduser().resolve()
        if args.scan_output_dir
        else ROOT / "reports" / f"scan_{target.isoformat()}"
    )
    scan_path = scan_dir / f"market_scan_{target.isoformat()}.csv"
    scan_stats = None
    if not scan_path.exists() and args.scan_if_missing:
        _csv, _markdown, scan_stats = scan_market(target, scan_path.parent, max_workers=16)
    if not scan_path.exists():
        raise SystemExit(f"scan missing: {scan_path}")
    rows = read_scan(scan_path)
    quality = resolve_scan_quality(scan_path, scan_stats)
    rows, quality, evidence_path = prefer_qualified_scan_evidence(
        rows,
        quality,
        scan_path,
        load_scan(target, path=DEFAULT_PATH),
    )
    upsert_scan(target, rows, quality, source="eod_qualified_scan", path=DEFAULT_PATH)
    sectors = resolve_sector_map(rows)
    regime = resolve_market_regime(target)
    payload = {
        "schema_version": "chan520_local_market_refresh_v1",
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "trade_date": target.isoformat(),
        "status": "PASS" if quality.get("coverage_pass") and regime.get("state") != "UNKNOWN" else "DEGRADED",
        "scan_rows": len(rows),
        "scan_evidence_path": str(evidence_path),
        "scan_quality": quality,
        "sector_count": len(set(sectors.values())),
        "market_regime": regime,
        "store": stats(path=DEFAULT_PATH),
    }
    report = ROOT / "reports" / "market_store" / target.strftime("%Y%m%d") / "refresh.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
