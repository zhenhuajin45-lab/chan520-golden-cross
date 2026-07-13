"""Optional 掘金量化 data adapter.

The token is read from ``GM_TOKEN`` or passed by the caller and is never
persisted by this module.  The adapter converts GM daily bars into the local
``KLine`` contract so the existing causal portfolio engine can be reused.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any

from .models import KLine, StockMeta


def gm_symbol(code: str) -> str:
    code = str(code).strip().split(".")[-1].zfill(6)
    if code in {"000001", "000300"}:
        return f"SHSE.{code}"
    if code in {"399001", "399006"}:
        return f"SZSE.{code}"
    exchange = "SHSE" if code.startswith(("5", "6", "9")) else "SZSE"
    return f"{exchange}.{code}"


def gm_history(
    code: str,
    end: date,
    lookback_days: int = 1000,
    adjust: str = "none",
    token: str | None = None,
) -> tuple[StockMeta, list[KLine]]:
    """Fetch one symbol from GM without storing credentials."""
    from gm.api import history, set_token

    auth = token or os.environ.get("GM_TOKEN")
    if not auth:
        raise RuntimeError("GM_TOKEN is required for the 掘金 data adapter")
    set_token(auth)
    end_text = end.isoformat()
    start_text = (end - timedelta(days=lookback_days)).isoformat()
    adjustment = {"none": 0, "qfq": 1, "hfq": 2}.get(adjust)
    if adjustment is None:
        raise ValueError("GM adjustment must be none, qfq, or hfq")
    symbol = gm_symbol(code)
    frame = history(
        symbol,
        "1d",
        start_text,
        end_text,
        fields="symbol,eob,open,high,low,close,volume,amount",
        skip_suspended=False,
        adjust=adjustment,
        df=True,
    )
    if frame is None or len(frame) == 0:
        raise RuntimeError(f"GM returned no daily bars for {symbol} up to {end_text}")
    frame = frame.sort_values("eob")
    rows: list[KLine] = []
    previous_close: float | None = None
    for item in frame.to_dict("records"):
        day = _to_date(item["eob"])
        close = float(item["close"])
        pct = ((close / previous_close) - 1) * 100 if previous_close else 0.0
        rows.append(
            KLine(
                date=day,
                open=float(item["open"]),
                close=close,
                high=float(item["high"]),
                low=float(item["low"]),
                volume=float(item.get("volume", 0) or 0),
                amount=float(item.get("amount", 0) or 0),
                amplitude=((float(item["high"]) - float(item["low"])) / float(item["open"]) * 100)
                if float(item["open"])
                else 0.0,
                pct_chg=pct,
                change=close - previous_close if previous_close else 0.0,
                turnover=0.0,
            )
        )
        previous_close = close
    name = symbol
    return StockMeta(code=symbol.split(".")[-1], name=name, market=1 if symbol.startswith("SHSE") else 0), rows


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
