from __future__ import annotations

from enum import StrEnum


class VerdictCode(StrEnum):
    ENTRY = "entry"
    OBSERVE = "observe"
    REJECT = "reject"


class ReasonCode(StrEnum):
    OK = "ok"
    NOT_IN_DYNAMIC_UNIVERSE = "not_in_dynamic_universe"
    ALPHA_THRESHOLD = "alpha_threshold"
    ENTRY_THRESHOLD = "entry_threshold"
    BEAR_MARKET = "bear_market"
    REGIME_REJECTED = "regime_rejected"
    SECTOR_REJECTED = "sector_rejected"
    INDUSTRY_UNMAPPED = "industry_unmapped"
    RISK_OR_POSITION = "risk_or_position"
    CAPACITY = "capacity"
    MISSING_ROW_OR_POINT = "missing_row_or_point"
    MISSING_SIGNAL_ROW = "missing_signal_row"
    STANDARD_TIER_REQUIRES_ENTRY = "standard_tier_requires_entry_verdict"
    ACUTE_MOVE = "acute_move"
    INVALID_STOP = "invalid_stop"
    STOP_DISTANCE_TOO_WIDE = "stop_distance_too_wide"
    RR_TOO_LOW = "rr_too_low"
    ATR_MISSING = "atr_missing"
    POSITION_CAP = "position_cap"


class SectorDataStatus(StrEnum):
    MAPPED = "mapped"
    MISSING = "missing"
    FALLBACK = "fallback"
    STATIC_END_DATE = "static_end_date"
