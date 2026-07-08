from __future__ import annotations

import csv
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import requests

from .data import DataError, normalize_code, trim_to_date
from .models import KLine, StockMeta
from .strategy import analyze
from .indicators import fmt


@dataclass(frozen=True)
class UniverseStock:
    code: str
    name: str
    market: int
    close: float | None = None
    pct_chg: float | None = None
    turnover: float | None = None


@dataclass(frozen=True)
class ScanRow:
    code: str
    name: str
    close: float
    pct_chg: float
    verdict: str
    level: str
    score: int
    main_signal: str
    satisfied_count: int
    defect_count: int
    satisfied: str
    defects: str
    ma5: str
    ma10: str
    ma20: str
    ma60: str
    dif: str
    dea: str
    macd: str
    rsi14: str
    volume_ratio: str
    ma5_slope: str


def fetch_hs_universe(timeout: int = 20) -> list[UniverseStock]:
    try:
        stocks = _fetch_akshare_universe()
        if stocks:
            return stocks
    except Exception:
        pass

    stocks: list[UniverseStock] = []
    page = 1
    page_size = 10000
    total = None
    while total is None or len(stocks) < total:
        params = {
            "pn": page,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f2,f3,f8",
        }
        url = "https://push2.eastmoney.com/api/qt/clist/get?" + urlencode(params)
        payload = _read_json(url, timeout=timeout, referer="https://quote.eastmoney.com/", attempts=6)
        data = payload.get("data") or {}
        total = int(data.get("total") or 0)
        diff = data.get("diff") or []
        if not diff:
            break
        for item in diff:
            code = normalize_code(str(item.get("f12", "")))
            name = str(item.get("f14") or "")
            if _is_excluded_name(name):
                continue
            stocks.append(
                UniverseStock(
                    code=code,
                    name=name,
                    market=1 if code.startswith(("5", "6", "9")) else 0,
                    close=_float_or_none(item.get("f2")),
                    pct_chg=_float_or_none(item.get("f3")),
                    turnover=_float_or_none(item.get("f8")),
                )
            )
        page += 1
    return stocks


def _fetch_akshare_universe() -> list[UniverseStock]:
    import akshare as ak

    df = ak.stock_info_a_code_name()
    stocks: list[UniverseStock] = []
    for item in df.to_dict("records"):
        code = normalize_code(str(item.get("code", "")))
        name = str(item.get("name") or "")
        if not code.startswith(("0", "3", "6")):
            continue
        if _is_excluded_name(name):
            continue
        stocks.append(UniverseStock(code=code, name=name, market=1 if code.startswith("6") else 0))
    return stocks


def scan_market(
    target: date,
    output_dir: Path,
    max_workers: int = 16,
    include_observe: bool = True,
) -> tuple[Path, Path, dict[str, int]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    universe = fetch_hs_universe()
    rows: list[ScanRow] = []
    failures = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(_scan_one, stock, target): stock for stock in universe}
        completed = 0
        for future in as_completed(future_map):
            completed += 1
            result = future.result()
            if result is None:
                failures += 1
            elif include_observe or result.verdict != "不入选":
                rows.append(result)
            if completed % 50 == 0:
                print(f"scan progress: {completed}/{len(universe)}, rows={len(rows)}, failures={failures}", flush=True)

    rows.sort(key=lambda row: (row.verdict != "入选", -row.score, -row.satisfied_count, row.code))
    csv_path = output_dir / f"market_scan_{target.isoformat()}.csv"
    md_path = output_dir / f"market_scan_{target.isoformat()}.md"
    _write_csv(csv_path, rows)
    _write_markdown(md_path, rows, len(universe), failures, target)
    stats = {
        "universe": len(universe),
        "rows": len(rows),
        "failures": failures,
        "selected": sum(1 for row in rows if row.verdict == "入选"),
        "watch": sum(1 for row in rows if row.verdict.startswith("观察")),
        "not_selected": sum(1 for row in rows if row.verdict == "不入选"),
    }
    return csv_path, md_path, stats


def _scan_one(stock: UniverseStock, target: date) -> ScanRow | None:
    try:
        klines = _tencent_kline(stock, target)
        rows = trim_to_date(klines, target)
        report = analyze(StockMeta(code=stock.code, name=stock.name, market=stock.market), rows, target)
    except Exception:
        return None
    score = sum(
        item.score
        for item in report.large_cycle + report.buy_points + report.trend_rules + report.position_rules + report.exit_rules
    )
    main_signal = _main_signal(report)
    return ScanRow(
        code=stock.code,
        name=stock.name,
        close=report.target.close,
        pct_chg=report.target.pct_chg,
        verdict=report.verdict,
        level=report.level,
        score=score,
        main_signal=main_signal,
        satisfied_count=len(report.satisfied),
        defect_count=len(report.defects),
        satisfied="；".join(report.satisfied),
        defects="；".join(report.defects),
        ma5=fmt(report.indicator.ma5),
        ma10=fmt(report.indicator.ma10),
        ma20=fmt(report.indicator.ma20),
        ma60=fmt(report.indicator.ma60),
        dif=fmt(report.indicator.macd_dif, 3),
        dea=fmt(report.indicator.macd_dea, 3),
        macd=fmt(report.indicator.macd_hist, 3),
        rsi14=fmt(report.indicator.rsi14),
        volume_ratio=fmt(report.indicator.volume_ratio),
        ma5_slope=fmt(report.indicator.slope5_deg),
    )


def _tencent_kline(stock: UniverseStock, end: date, timeout: int = 5) -> list[KLine]:
    prefix = "sh" if stock.market == 1 else "sz"
    begin = end - timedelta(days=560)
    symbol = f"{prefix}{stock.code}"
    params = f"{symbol},day,{begin.isoformat()},{end.isoformat()},640,qfq"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urlencode({"param": params})
    payload = _read_json_urllib(url, timeout=timeout, referer="https://gu.qq.com/")
    data = payload.get("data", {}).get(symbol, {})
    klines = data.get("qfqday") or data.get("day")
    if not klines:
        raise DataError(f"no Tencent kline for {stock.code}")
    rows: list[KLine] = []
    for i, item in enumerate(klines):
        previous = klines[i - 1] if i > 0 else None
        rows.append(_parse_tencent_row(item, previous))
    return rows


def _parse_tencent_row(row: list[str], previous: list[str] | None) -> KLine:
    current_date = date.fromisoformat(row[0])
    open_price = float(row[1])
    close = float(row[2])
    high = float(row[3])
    low = float(row[4])
    volume = float(row[5])
    prev_close = float(previous[2]) if previous else open_price
    change = close - prev_close
    pct_chg = change / prev_close * 100 if prev_close else 0.0
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
        pct_chg=pct_chg,
        change=change,
        turnover=0.0,
    )


def _read_json(url: str, timeout: int, referer: str, attempts: int = 3) -> dict:
    last_error: Exception | None = None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": referer,
        "Connection": "keep-alive",
    }
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.4 * attempt)
    try:
        return _read_json_with_powershell(url, timeout)
    except Exception as exc:
        last_error = exc
    raise DataError(f"read json failed after {attempts} attempts: {last_error}")


def _read_json_with_powershell(url: str, timeout: int) -> dict:
    quoted_url = url.replace("'", "''")
    command = (
        "$ProgressPreference='SilentlyContinue'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"(Invoke-WebRequest -UseBasicParsing -Uri '{quoted_url}' -TimeoutSec {timeout}).Content"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout + 5,
    )
    return json.loads(completed.stdout)


def _read_json_urllib(url: str, timeout: int, referer: str, attempts: int = 1) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (RemoteDisconnected, TimeoutError, URLError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.2 * attempt)
    raise DataError(f"urllib read json failed after {attempts} attempts: {last_error}")


def _write_csv(path: Path, rows: Iterable[ScanRow]) -> None:
    rows = list(rows)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ScanRow.__dataclass_fields__.keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def _write_markdown(path: Path, rows: list[ScanRow], universe_count: int, failures: int, target: date) -> None:
    selected = [row for row in rows if row.verdict == "入选"]
    core = [row for row in rows if row.verdict.startswith("观察") and ("520金叉" in row.main_signal or row.score >= 8)]
    observe = [row for row in rows if row.verdict.startswith("观察") and row not in core]
    lines = [
        f"# {target.isoformat()} 沪深非ST 520策略全市场扫描",
        "",
        f"- 股票池：沪深主板、创业板、科创板，剔除 ST/*ST/退市，合计 {universe_count} 只。",
        f"- 成功输出：{len(rows)} 只；失败/停牌/数据不足：{failures} 只。",
        f"- 入选确认：{len(selected)} 只。",
        f"- 满足核心/高分观察：{len(core)} 只。",
        f"- 其他观察：{len(observe)} 只。",
        "",
    ]
    lines.extend(_section("一、入选确认", selected, limit=80))
    lines.extend(_section("二、满足核心条件/高分观察", core, limit=120))
    lines.extend(_section("三、其他观察候选 Top 80", observe, limit=80))
    lines.append("")
    lines.append("完整结果见同目录 CSV。")
    path.write_text("\n".join(lines), encoding="utf-8")


def _section(title: str, rows: list[ScanRow], limit: int) -> list[str]:
    lines = [f"## {title}", ""]
    if not rows:
        lines.append("无。")
        lines.append("")
        return lines
    lines.append("| 代码 | 名称 | 收盘 | 涨跌幅 | 结论 | 分数 | 主信号 | 满足条件 | 主要缺陷 |")
    lines.append("| --- | --- | ---: | ---: | --- | ---: | --- | --- | --- |")
    for row in rows[:limit]:
        lines.append(
            f"| {row.code} | {row.name} | {row.close:.2f} | {row.pct_chg:.2f}% | {row.verdict} | {row.score} | "
            f"{row.main_signal} | {_short(row.satisfied)} | {_short(row.defects)} |"
        )
    if len(rows) > limit:
        lines.append(f"| ... | ... | ... | ... | ... | ... | 仅展示前 {limit} 只 | 完整见 CSV | |")
    lines.append("")
    return lines


def _main_signal(report) -> str:
    active = [
        item.name
        for item in report.buy_points + report.trend_rules + report.position_rules
        if item.status in {"PASS", "WARN"} and item.score > 0
    ]
    return " + ".join(active[:4]) if active else "无"


def _is_excluded_name(name: str) -> bool:
    upper = name.upper()
    return "ST" in upper or "退" in name


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _short(text: str, limit: int = 80) -> str:
    text = text.replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"
