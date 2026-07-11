from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_GLOBS = ("*.py", "*.md", "*.csv", "*.json", "*.yml", "*.yaml")
SCAN_DIRS = ("chan520_skill", "scripts", "tests", "reports/backtest/v7", ".github")
BAD_MARKERS = (
    chr(0xFFFD),
    b"\xe9\x94\x9f".decode("utf-8"),
    b"\xe9\x8d\x8f".decode("utf-8"),
    b"\xe7\x91\x99".decode("utf-8"),
    b"\xe6\xb6\x93\xe5\xb6\x85".decode("utf-8"),
    b"\xe9\x8f\x88".decode("utf-8"),
    b"\xe7\xbb\xa1".decode("utf-8"),
)


def iter_text_files() -> list[Path]:
    out: list[Path] = []
    for rel in SCAN_DIRS:
        base = ROOT / rel
        if not base.exists():
            continue
        for pattern in TEXT_GLOBS:
            out.extend(path for path in base.rglob(pattern) if path.is_file())
    return sorted(set(out))


def test_no_unicode_replacement_or_mojibake_markers() -> None:
    offenders: list[str] = []
    for path in iter_text_files():
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if any(marker in text for marker in BAD_MARKERS):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
