from __future__ import annotations

from .models import KLine


def price_limit(code: str, is_st: bool = False) -> float:
    code = code.strip()
    if is_st:
        return 5.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("8", "4")):
        return 30.0
    return 10.0


def is_limit_up(row: KLine, prev_close: float, code: str, is_st: bool = False, tolerance: float = 0.3) -> bool:
    if prev_close <= 0:
        return False
    pct = (row.close / prev_close - 1) * 100
    return pct >= price_limit(code, is_st) - tolerance and row.close >= row.high * 0.999


def is_limit_down(row: KLine, prev_close: float, code: str, is_st: bool = False, tolerance: float = 0.3) -> bool:
    if prev_close <= 0:
        return False
    pct = (row.close / prev_close - 1) * 100
    return pct <= -price_limit(code, is_st) + tolerance and row.close <= row.low * 1.001


def is_new_stock(rows: list[KLine], min_history: int = 250) -> bool:
    return len(rows) < min_history
