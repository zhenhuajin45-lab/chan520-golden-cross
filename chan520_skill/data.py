from __future__ import annotations

import csv
import json
import re
import time
from http.client import RemoteDisconnected
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import KLine, StockMeta


class DataError(RuntimeError):
    pass


def market_id(code: str) -> int:
    code = normalize_code(code)
    return 1 if code.startswith(("5", "6", "9")) else 0


def normalize_code(code: str) -> str:
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) != 6:
        raise ValueError(f"invalid A-share code: {code!r}")
    return digits


def eastmoney_history(
    code: str,
    end: date,
    lookback_days: int = 560,
    adjust: int = 1,
    timeout: int = 20,
) -> tuple[StockMeta, list[KLine]]:
    code = normalize_code(code)
    market = market_id(code)
    begin = end - timedelta(days=lookback_days)
    params = {
        "secid": f"{market}.{code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": str(adjust),
        "beg": begin.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
    }
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urlencode(params)
    payload = _read_json_with_retry(url, timeout=timeout)
    data = payload.get("data")
    if not data or not data.get("klines"):
        raise DataError(f"Eastmoney returned no kline data for {code} up to {end}")
    meta = StockMeta(code=code, name=data.get("name") or code, market=int(data.get("market", market)))
    return meta, [_parse_eastmoney_kline(row) for row in data["klines"]]


def tencent_history(
    code: str,
    end: date,
    lookback_days: int = 560,
    adjust: str = "qfq",
    timeout: int = 20,
) -> tuple[StockMeta, list[KLine]]:
    code = normalize_code(code)
    prefix = "sh" if market_id(code) == 1 else "sz"
    begin = end - timedelta(days=lookback_days)
    symbol = f"{prefix}{code}"
    params = f"{symbol},day,{begin.isoformat()},{end.isoformat()},640,{adjust}"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urlencode({"param": params})
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    with urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data", {}).get(symbol, {})
    key = f"{adjust}day" if adjust in {"qfq", "hfq"} else "day"
    klines = data.get(key) or data.get("qfqday") or data.get("day")
    if not klines:
        raise DataError(f"Tencent returned no kline data for {code} up to {end}")
    name = tencent_quote(code).get("name", code)
    rows = [_parse_tencent_kline(row, previous=None if i == 0 else klines[i - 1]) for i, row in enumerate(klines)]
    return StockMeta(code=code, name=name, market=market_id(code)), rows


def auto_history(code: str, end: date, adjust: int = 1) -> tuple[StockMeta, list[KLine]]:
    try:
        return eastmoney_history(code, end, adjust=adjust)
    except DataError:
        # Eastmoney is richer, but Tencent is often more stable for historical K lines.
        return tencent_history(code, end, adjust="qfq" if adjust == 1 else "hfq" if adjust == 2 else "none")


def tencent_quote(code: str, timeout: int = 10) -> dict[str, str]:
    code = normalize_code(code)
    prefix = "sh" if market_id(code) == 1 else "sz"
    url = f"https://qt.gtimg.cn/q={prefix}{code}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("gbk", errors="replace")
    match = re.search(r'="(.*)";', text)
    if not match:
        raise DataError(f"Tencent returned unexpected quote payload for {code}")
    fields = match.group(1).split("~")
    return {
        "name": fields[1],
        "code": fields[2],
        "price": fields[3],
        "prev_close": fields[4],
        "open": fields[5],
        "datetime": fields[30],
        "pct_chg": fields[32],
    }


def _read_json_with_retry(url: str, timeout: int, attempts: int = 3) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://quote.eastmoney.com/",
            },
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (RemoteDisconnected, TimeoutError, URLError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.6 * attempt)
    raise DataError(f"failed to read Eastmoney payload after {attempts} attempts: {last_error}")


def load_csv(path: str | Path) -> tuple[StockMeta, list[KLine]]:
    path = Path(path)
    rows: list[KLine] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(
                KLine(
                    date=date.fromisoformat(row["date"]),
                    open=float(row["open"]),
                    close=float(row["close"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    volume=float(row.get("volume", 0) or 0),
                    amount=float(row.get("amount", 0) or 0),
                    amplitude=float(row.get("amplitude", 0) or 0),
                    pct_chg=float(row.get("pct_chg", 0) or 0),
                    change=float(row.get("change", 0) or 0),
                    turnover=float(row.get("turnover", 0) or 0),
                )
            )
    if not rows:
        raise DataError(f"CSV has no rows: {path}")
    code = normalize_code(path.stem[:6]) if path.stem[:6].isdigit() else "000000"
    return StockMeta(code=code, name=path.stem, market=market_id(code) if code != "000000" else 0), rows


def trim_to_date(rows: Iterable[KLine], target: date) -> list[KLine]:
    trimmed = [row for row in rows if row.date <= target]
    if not trimmed or trimmed[-1].date != target:
        last = trimmed[-1].date.isoformat() if trimmed else "none"
        raise DataError(f"target date {target} not found in kline data; last available date is {last}")
    return trimmed


def _parse_eastmoney_kline(row: str) -> KLine:
    parts = row.split(",")
    return KLine(
        date=date.fromisoformat(parts[0]),
        open=float(parts[1]),
        close=float(parts[2]),
        high=float(parts[3]),
        low=float(parts[4]),
        volume=float(parts[5]),
        amount=float(parts[6]),
        amplitude=float(parts[7]),
        pct_chg=float(parts[8]),
        change=float(parts[9]),
        turnover=float(parts[10]),
    )


def _parse_tencent_kline(row: list[str], previous: list[str] | None) -> KLine:
    current_date = date.fromisoformat(row[0])
    open_price = float(row[1])
    close = float(row[2])
    high = float(row[3])
    low = float(row[4])
    volume = float(row[5])
    prev_close = float(previous[2]) if previous else open_price
    change = close - prev_close
    pct = change / prev_close * 100 if prev_close else 0.0
    amplitude = (high - low) / prev_close * 100 if prev_close else 0.0
    return KLine(
        date=current_date,
        open=open_price,
        close=close,
        high=high,
        low=low,
        volume=volume,
        amount=0.0,
        amplitude=amplitude,
        pct_chg=pct,
        change=change,
        turnover=0.0,
    )
