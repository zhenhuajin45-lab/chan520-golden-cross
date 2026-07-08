from __future__ import annotations

import math

from .models import KLine


class DataQualityError(ValueError):
    pass


def data_quality(rows: list[KLine], min_bars: int = 61) -> list[str]:
    issues: list[str] = []
    if len(rows) < min_bars:
        issues.append(f"insufficient bars: {len(rows)} < {min_bars}")
    seen = set()
    previous_date = None
    for idx, row in enumerate(rows):
        if row.date in seen:
            issues.append(f"duplicate date: {row.date.isoformat()}")
        seen.add(row.date)
        if previous_date is not None and row.date <= previous_date:
            issues.append(f"dates not strictly increasing at index {idx}: {row.date.isoformat()}")
        previous_date = row.date
        values = {
            "open": row.open,
            "close": row.close,
            "high": row.high,
            "low": row.low,
            "volume": row.volume,
        }
        for name, value in values.items():
            if value is None or isinstance(value, float) and math.isnan(value):
                issues.append(f"{row.date.isoformat()} {name} is NaN")
            elif name != "volume" and value <= 0:
                issues.append(f"{row.date.isoformat()} {name} <= 0")
            elif name == "volume" and value < 0:
                issues.append(f"{row.date.isoformat()} volume < 0")
        if row.high < row.low:
            issues.append(f"{row.date.isoformat()} high < low")
        if row.high < max(row.open, row.close):
            issues.append(f"{row.date.isoformat()} high < max(open, close)")
        if row.low > min(row.open, row.close):
            issues.append(f"{row.date.isoformat()} low > min(open, close)")
    return issues


def ensure_data_quality(rows: list[KLine], min_bars: int = 61) -> None:
    issues = data_quality(rows, min_bars=min_bars)
    if issues:
        raise DataQualityError("; ".join(issues[:8]))
