from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .data import DataError, trim_to_date
from .indicators import build_indicators, pct_change
from .models import KLine, RegimeState
from .quality import ensure_data_quality


INDEX_NAMES = {
    "000001": "上证指数",
    "399006": "创业板指",
    "000300": "沪深300",
}

INDEX_SECIDS = {
    "000001": "1.000001",
    "399006": "0.399006",
    "000300": "1.000300",
}


def evaluate_regime(symbol: str, rows: list[KLine], target: date) -> RegimeState:
    trimmed = trim_to_date(rows, target)
    ensure_data_quality(trimmed, min_bars=61)
    points = build_indicators(trimmed)
    point = points[-1]
    close = trimmed[-1].close
    rise60 = pct_change(trimmed[-61].close, close)
    ma20_slope = point.slope5_deg or 0.0
    above20 = point.ma20 is not None and close > point.ma20
    above60 = point.ma60 is not None and close > point.ma60
    if above20 and above60 and rise60 >= 3 and ma20_slope >= 0:
        regime = "trend_up"
        ok = True
    elif not above60 and rise60 <= -5:
        regime = "down"
        ok = False
    else:
        regime = "range"
        ok = False
    ma20_text = f"{point.ma20:.2f}" if point.ma20 is not None else "N/A"
    ma60_text = f"{point.ma60:.2f}" if point.ma60 is not None else "N/A"
    detail = (
        f"{INDEX_NAMES.get(symbol, symbol)} {target.isoformat()} regime={regime}; "
        f"close={close:.2f}, ma20={ma20_text}, ma60={ma60_text}, rise60={rise60:.2f}%"
    )
    return RegimeState(symbol=symbol, name=INDEX_NAMES.get(symbol, symbol), date=target, regime=regime, regime_ok=ok, detail=detail)


def fetch_regime(symbol: str, target: date, adjust: int = 0) -> RegimeState:
    rows = index_history(symbol, target, adjust=adjust)
    return evaluate_regime(symbol, rows, target)


def index_history(symbol: str, end: date, adjust: int = 0, lookback_days: int = 560, timeout: int = 20) -> list[KLine]:
    secid = INDEX_SECIDS.get(symbol)
    if not secid:
        raise DataError(f"unsupported index symbol for regime: {symbol}")
    begin = end.replace(year=end.year - 2) if end.year > 2 else end
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": str(adjust),
        "beg": begin.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
    }
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
    with urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data")
    if not data or not data.get("klines"):
        raise DataError(f"Eastmoney returned no index kline data for {symbol}")
    rows: list[KLine] = []
    for item in data["klines"]:
        parts = item.split(",")
        rows.append(
            KLine(
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
        )
    return rows


def degrade_verdict(verdict: str) -> str:
    if verdict == "入选":
        return "观察"
    if verdict.startswith("观察"):
        return "回避/减仓观察"
    return verdict
