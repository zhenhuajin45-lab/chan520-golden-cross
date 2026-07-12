from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import IndicatorPoint, KLine
from .sector import industry_of


@dataclass(frozen=True)
class SectorAlpha:
    sector: str
    date: date
    heat_score: float
    member_count: int
    breadth_score: float
    relative_strength_score: float
    amount_score: float
    momentum_score: float


@dataclass(frozen=True)
class SectorHeatBuildResult:
    heat_by_date: dict[date, dict[str, SectorAlpha]]
    exclusions: list[dict[str, str | int]]

    def __getitem__(self, day: date) -> dict[str, SectorAlpha]:
        return self.heat_by_date[day]

    def get(self, day: date, default=None):
        return self.heat_by_date.get(day, default)

    def values(self):
        return self.heat_by_date.values()

    def items(self):
        return self.heat_by_date.items()


def build_sector_heat(
    histories: dict[str, list[KLine]],
    sector_map: dict[str, str],
    rows_by_date: dict[str, dict[date, KLine]],
    points_by_date: dict[str, dict[date, IndicatorPoint]],
    row_index_by_date: dict[str, dict[date, int]],
    eligible_by_date: dict[date, set[str]],
    all_dates: list[date],
    *,
    min_members: int = 20,
    max_members: int = 1200,
    min_sample_members: int = 30,
) -> SectorHeatBuildResult:
    """Daily sector heat from history available at that date.

    This mirrors the production strategy's idea: sector is a small Alpha prior,
    not a hard gate.  The score blends participation, breadth, relative
    strength, liquidity and same-day momentum.
    """

    heat_by_date: dict[date, dict[str, SectorAlpha]] = {}
    exclusions: list[dict[str, str | int]] = []
    for day in all_dates:
        grouped: dict[str, list[str]] = {}
        eligible = eligible_by_date.get(day, set(histories))
        for code in eligible:
            if code not in histories:
                continue
            sector = industry_of(code, sector_map)
            grouped.setdefault(sector, []).append(code)

        day_heat: dict[str, SectorAlpha] = {}
        amount_values: list[float] = []
        raw_stats: dict[str, dict[str, float]] = {}
        for sector, codes in grouped.items():
            if len(codes) < min_members:
                exclusions.append(
                    {
                        "date": day.isoformat(),
                        "sector": sector,
                        "reason_code": "below_min_members",
                        "member_count": len(codes),
                        "sample_count": 0,
                    }
                )
                continue
            if len(codes) > max_members:
                exclusions.append(
                    {
                        "date": day.isoformat(),
                        "sector": sector,
                        "reason_code": "above_max_members",
                        "member_count": len(codes),
                        "sample_count": 0,
                    }
                )
                continue
            samples = []
            missing_rows = 0
            missing_indicators = 0
            for code in codes:
                row = rows_by_date.get(code, {}).get(day)
                point = points_by_date.get(code, {}).get(day)
                idx = row_index_by_date.get(code, {}).get(day)
                if row is None:
                    missing_rows += 1
                    continue
                if point is None or idx is None or idx < 20:
                    missing_indicators += 1
                    continue
                prior = histories[code][idx - 20]
                prev = histories[code][idx - 1] if idx > 0 else None
                samples.append((row, point, prior, prev))
            if len(samples) < min_sample_members:
                reason = "below_min_sample_members"
                if missing_rows and not samples:
                    reason = "missing_rows"
                elif missing_indicators and not samples:
                    reason = "missing_indicators"
                exclusions.append(
                    {
                        "date": day.isoformat(),
                        "sector": sector,
                        "reason_code": reason,
                        "member_count": len(codes),
                        "sample_count": len(samples),
                    }
                )
                continue

            breadth = sum(1 for row, point, _prior, _prev in samples if point.ma20 and row.close > point.ma20) / len(samples)
            avg_ret20 = sum(row.close / prior.close - 1 for row, _point, prior, _prev in samples if prior.close > 0) / len(samples)
            momentum = sum(1 for row, _point, _prior, prev in samples if prev and row.close > prev.close) / len(samples)
            avg_amount = sum(row.amount for row, _point, _prior, _prev in samples) / len(samples)
            amount_values.append(avg_amount)
            raw_stats[sector] = {
                "member_count": float(len(samples)),
                "breadth": breadth,
                "ret20": avg_ret20,
                "momentum": momentum,
                "amount": avg_amount,
            }

        for sector, stats in raw_stats.items():
            count_score = min(100.0, stats["member_count"] / max(1, min_sample_members) * 100.0)
            breadth_score = _clip01(stats["breadth"]) * 100.0
            rs_score = _clip01((stats["ret20"] + 0.10) / 0.20) * 100.0
            momentum_score = _clip01(stats["momentum"]) * 100.0
            amount_score = _percentile_score(stats["amount"], amount_values)
            heat = (
                0.30 * count_score
                + 0.25 * rs_score
                + 0.25 * breadth_score
                + 0.10 * amount_score
                + 0.10 * momentum_score
            )
            day_heat[sector] = SectorAlpha(
                sector=sector,
                date=day,
                heat_score=max(0.0, min(100.0, heat)),
                member_count=int(stats["member_count"]),
                breadth_score=breadth_score,
                relative_strength_score=rs_score,
                amount_score=amount_score,
                momentum_score=momentum_score,
            )
        heat_by_date[day] = day_heat
    return SectorHeatBuildResult(heat_by_date=heat_by_date, exclusions=exclusions)


def sector_heat_bonus(
    code: str,
    day: date,
    sector_map: dict[str, str],
    heat_by_date: dict[date, dict[str, SectorAlpha]],
    *,
    gate: float = 70.0,
    cap: float = 6.0,
    scale: float = 0.06,
) -> float:
    sector = industry_of(code, sector_map)
    heat = heat_by_date.get(day, {}).get(sector)
    if heat is None:
        return 0.0
    return max(0.0, min(cap, (heat.heat_score - gate) * scale))


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _percentile_score(value: float, values: list[float]) -> float:
    if not values:
        return 0.0
    ranked = sorted(values)
    le_count = sum(1 for item in ranked if item <= value)
    return 100.0 * le_count / len(ranked)
