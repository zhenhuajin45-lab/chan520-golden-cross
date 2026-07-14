from __future__ import annotations

import json
import subprocess
import sys


def test_true_incremental_parity_fails_closed_until_kernel_extracted(tmp_path):
    output = tmp_path / "parity.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_true_incremental_parity.py",
            "--start",
            "2026-01-05",
            "--end",
            "2026-01-06",
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL_CLOSED"
    assert payload["shadow_readiness"] is False
    assert payload["reason"] == "TRUE_INCREMENTAL_KERNEL_NOT_EXTRACTED"
