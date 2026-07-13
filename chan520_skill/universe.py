from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .data import normalize_code


@dataclass(frozen=True)
class UniverseMember:
    as_of: date
    code: str
    name: str
    industry: str


def load_universe_snapshot(path: str | Path, as_of: date) -> list[UniverseMember]:
    """Load a point-in-time eligible universe, never a present-day substitute.

    Required CSV fields are ``as_of``, ``code`` and ``name``.  ``industry`` is
    required for portfolio backtests so an unknown-industry bucket cannot mask
    concentration risk.  ``eligible`` defaults to true and permits explicit
    exclusion of ST, suspended or short-history securities.
    """
    snapshot_path = Path(path)
    members: list[UniverseMember] = []
    with snapshot_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or [])
        missing = {"as_of", "code", "name", "industry"} - fields
        if missing:
            raise ValueError(f"universe snapshot missing fields: {', '.join(sorted(missing))}")
        for row in reader:
            if date.fromisoformat(row["as_of"]) != as_of:
                continue
            eligible = str(row.get("eligible", "true")).strip().lower()
            if eligible in {"0", "false", "no", "n"}:
                continue
            industry = str(row["industry"] or "").strip()
            if not industry:
                continue
            members.append(UniverseMember(as_of, normalize_code(row["code"]), str(row["name"] or ""), industry))
    if not members:
        raise ValueError(f"no eligible universe members for {as_of.isoformat()} in {snapshot_path}")
    return members


def industry_map(members: list[UniverseMember]) -> dict[str, str]:
    return {member.code: member.industry for member in members}
