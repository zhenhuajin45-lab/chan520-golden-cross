from __future__ import annotations

import csv
import json
from datetime import date

from chan520_skill import scanner


def test_resolve_hs_universe_uses_latest_qualified_local_scan(tmp_path, monkeypatch):
    scan_dir = tmp_path / "scan_2026-07-20"
    scan_dir.mkdir()
    scan_path = scan_dir / "market_scan_2026-07-20.csv"
    with scan_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["code", "name"])
        writer.writeheader()
        for index in range(1000):
            writer.writerow({"code": f"{index:06d}", "name": f"样本{index}"})
    (scan_dir / "scan_quality_2026-07-20.json").write_text(
        json.dumps({"coverage_pass": True}), encoding="utf-8"
    )
    monkeypatch.setattr(scanner, "_fetch_eastmoney_universe", lambda _timeout: (_ for _ in ()).throw(RuntimeError("502")))
    monkeypatch.setattr(scanner, "_fetch_sina_universe", lambda _timeout: (_ for _ in ()).throw(RuntimeError("timeout")))

    stocks, metadata = scanner.resolve_hs_universe(
        target=date(2026, 7, 21), reports_root=tmp_path
    )

    assert len(stocks) == 1000
    assert metadata["universe_source"] == "qualified_local_scan"
    assert metadata["universe_asof"] == "2026-07-20"
    assert metadata["universe_fallback"] is True
    assert "502" in metadata["universe_errors"][0]


def test_cached_universe_rejects_same_day_snapshot(tmp_path, monkeypatch):
    scan_dir = tmp_path / "scan_2026-07-21"
    scan_dir.mkdir()
    (scan_dir / "market_scan_2026-07-21.csv").write_text("code,name\n000001,平安银行\n", encoding="utf-8")
    (scan_dir / "scan_quality_2026-07-21.json").write_text(
        json.dumps({"coverage_pass": True}), encoding="utf-8"
    )
    monkeypatch.setattr(scanner, "_fetch_eastmoney_universe", lambda _timeout: [])
    monkeypatch.setattr(scanner, "_fetch_sina_universe", lambda _timeout: [])
    monkeypatch.setattr(scanner, "_fetch_akshare_universe", lambda: [])

    try:
        scanner.resolve_hs_universe(target=date(2026, 7, 21), reports_root=tmp_path)
    except scanner.DataError as exc:
        assert "universe unavailable" in str(exc)
    else:
        raise AssertionError("same-day partial scan must not be used as a universe fallback")
