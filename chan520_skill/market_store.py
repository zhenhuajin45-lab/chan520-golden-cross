from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .models import KLine, StockMeta


DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "market.db"
TZ = ZoneInfo("Asia/Shanghai")


def initialize(path: str | Path = DEFAULT_PATH) -> Path:
    store = Path(path)
    store.parent.mkdir(parents=True, exist_ok=True)
    with connect(store) as conn:
        conn.executescript(
            """
            create table if not exists daily_bars (
                code text not null,
                trade_date text not null,
                open real not null, close real not null, high real not null, low real not null,
                volume real not null default 0, amount real not null default 0,
                amplitude real not null default 0, pct_chg real not null default 0,
                change real not null default 0, turnover real not null default 0,
                source text not null, updated_at text not null,
                primary key (code, trade_date)
            );
            create table if not exists index_bars (
                code text not null,
                trade_date text not null,
                open real not null, close real not null, high real not null, low real not null,
                volume real not null default 0, amount real not null default 0,
                amplitude real not null default 0, pct_chg real not null default 0,
                change real not null default 0, turnover real not null default 0,
                source text not null, updated_at text not null,
                primary key (code, trade_date)
            );
            create table if not exists stock_meta (
                code text primary key, name text not null, market integer not null default 0,
                source text not null, updated_at text not null
            );
            create table if not exists sector_map (
                code text primary key, sector text not null, source text not null, updated_at text not null
            );
            create table if not exists scan_snapshots (
                trade_date text not null, code text not null, payload_json text not null,
                source text not null, updated_at text not null,
                primary key (trade_date, code)
            );
            create table if not exists scan_quality (
                trade_date text primary key, payload_json text not null,
                source text not null, updated_at text not null
            );
            create table if not exists minute_days (
                symbol text not null, trade_date text not null, is_index integer not null,
                name text not null, prev_close real not null, open real not null,
                minutes_json text not null, source text not null, updated_at text not null,
                primary key (symbol, trade_date, is_index)
            );
            create index if not exists idx_daily_bars_date on daily_bars(trade_date);
            create index if not exists idx_index_bars_date on index_bars(trade_date);
            create index if not exists idx_scan_snapshots_date on scan_snapshots(trade_date);
            """
        )
    return store


def connect(path: str | Path = DEFAULT_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=WAL")
    conn.execute("pragma synchronous=NORMAL")
    conn.execute("pragma busy_timeout=30000")
    return conn


def upsert_history(
    code: str,
    name: str,
    market: int,
    rows: Iterable[KLine],
    *,
    source: str,
    is_index: bool = False,
    path: str | Path = DEFAULT_PATH,
) -> None:
    initialize(path)
    table = "index_bars" if is_index else "daily_bars"
    stamp = now()
    payload = [
        (
            code, row.date.isoformat(), row.open, row.close, row.high, row.low,
            row.volume, row.amount, row.amplitude, row.pct_chg, row.change, row.turnover,
            source, stamp,
        )
        for row in rows
    ]
    with connect(path) as conn:
        conn.executemany(
            f"insert or replace into {table} values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        if not is_index:
            conn.execute(
                "insert or replace into stock_meta values (?, ?, ?, ?, ?)",
                (code, name or code, market, source, stamp),
            )


def load_history(
    code: str,
    target: date,
    *,
    is_index: bool = False,
    minimum_bars: int = 61,
    path: str | Path = DEFAULT_PATH,
) -> tuple[StockMeta, list[KLine], str] | None:
    if not Path(path).exists():
        return None
    table = "index_bars" if is_index else "daily_bars"
    with connect(path) as conn:
        records = conn.execute(
            f"select * from {table} where code = ? and trade_date <= ? order by trade_date",
            (code, target.isoformat()),
        ).fetchall()
        if len(records) < minimum_bars or records[-1]["trade_date"] != target.isoformat():
            return None
        meta_row = None if is_index else conn.execute("select * from stock_meta where code = ?", (code,)).fetchone()
    rows = [_record_to_kline(row) for row in records]
    name = code if meta_row is None else str(meta_row["name"])
    market = 1 if code.startswith(("5", "6", "9")) else 0
    source = str(records[-1]["source"])
    return StockMeta(code=code, name=name, market=market), rows, f"local_sqlite:{source}"


def latest_bar_date_before(
    code: str, before: date, *, is_index: bool = False, path: str | Path = DEFAULT_PATH
) -> date | None:
    if not Path(path).exists():
        return None
    table = "index_bars" if is_index else "daily_bars"
    with connect(path) as conn:
        value = conn.execute(
            f"select max(trade_date) from {table} where code = ? and trade_date < ?",
            (code, before.isoformat()),
        ).fetchone()[0]
    return date.fromisoformat(str(value)) if value else None


def upsert_scan(
    target: date,
    rows: Iterable[dict[str, Any]],
    quality: dict[str, Any],
    *,
    source: str,
    path: str | Path = DEFAULT_PATH,
) -> None:
    initialize(path)
    stamp = now()
    records = [
        (target.isoformat(), str(row.get("code") or ""), json.dumps(row, ensure_ascii=False), source, stamp)
        for row in rows
        if str(row.get("code") or "")
    ]
    with connect(path) as conn:
        conn.executemany("insert or replace into scan_snapshots values (?, ?, ?, ?, ?)", records)
        conn.execute(
            "insert or replace into scan_quality values (?, ?, ?, ?)",
            (target.isoformat(), json.dumps(quality, ensure_ascii=False), source, stamp),
        )


def load_scan(target: date, *, path: str | Path = DEFAULT_PATH) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    if not Path(path).exists():
        return None
    with connect(path) as conn:
        rows = conn.execute(
            "select payload_json from scan_snapshots where trade_date = ? order by code",
            (target.isoformat(),),
        ).fetchall()
        quality = conn.execute("select payload_json from scan_quality where trade_date = ?", (target.isoformat(),)).fetchone()
    if not rows or quality is None:
        return None
    return [json.loads(row[0]) for row in rows], json.loads(quality[0])


def upsert_minute_day(
    symbol: str,
    target: date,
    payload: dict[str, Any],
    *,
    is_index: bool,
    source: str,
    path: str | Path = DEFAULT_PATH,
) -> None:
    initialize(path)
    with connect(path) as conn:
        conn.execute(
            "insert or replace into minute_days values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                symbol, target.isoformat(), int(is_index), str(payload.get("name") or symbol),
                float(payload["prev_close"]), float(payload["open"]),
                json.dumps(payload["minutes"], ensure_ascii=False, sort_keys=True), source, now(),
            ),
        )


def load_minute_day(
    symbol: str,
    target: date,
    *,
    is_index: bool,
    path: str | Path = DEFAULT_PATH,
) -> dict[str, Any] | None:
    if not Path(path).exists():
        return None
    with connect(path) as conn:
        row = conn.execute(
            "select * from minute_days where symbol = ? and trade_date = ? and is_index = ?",
            (symbol, target.isoformat(), int(is_index)),
        ).fetchone()
    if row is None:
        return None
    return {
        "symbol": symbol,
        "name": str(row["name"]),
        "prev_close": float(row["prev_close"]),
        "open": float(row["open"]),
        "minutes": {str(key): float(value) for key, value in json.loads(row["minutes_json"]).items()},
        "source": f"local_sqlite:{row['source']}",
        "historical_price_fields_source": "same_date_cached_day_payload",
    }


def upsert_sectors(
    mapping: dict[str, str], *, source: str, path: str | Path = DEFAULT_PATH
) -> None:
    initialize(path)
    stamp = now()
    with connect(path) as conn:
        conn.executemany(
            "insert or replace into sector_map values (?, ?, ?, ?)",
            [(code, sector or "UNMAPPED", source, stamp) for code, sector in mapping.items()],
        )


def load_sectors(*, path: str | Path = DEFAULT_PATH) -> dict[str, str]:
    if not Path(path).exists():
        return {}
    with connect(path) as conn:
        return {str(row["code"]): str(row["sector"]) for row in conn.execute("select code, sector from sector_map")}


def stats(*, path: str | Path = DEFAULT_PATH) -> dict[str, Any]:
    initialize(path)
    with connect(path) as conn:
        tables = ("daily_bars", "index_bars", "scan_snapshots", "sector_map", "minute_days")
        counts = {table: int(conn.execute(f"select count(*) from {table}").fetchone()[0]) for table in tables}
        latest = conn.execute("select max(trade_date) from scan_snapshots").fetchone()[0]
    return {"path": str(Path(path)), "counts": counts, "latest_scan_date": latest}


def _record_to_kline(row: sqlite3.Row) -> KLine:
    return KLine(
        date=date.fromisoformat(str(row["trade_date"])), open=float(row["open"]), close=float(row["close"]),
        high=float(row["high"]), low=float(row["low"]), volume=float(row["volume"]), amount=float(row["amount"]),
        amplitude=float(row["amplitude"]), pct_chg=float(row["pct_chg"]), change=float(row["change"]),
        turnover=float(row["turnover"]),
    )


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")
