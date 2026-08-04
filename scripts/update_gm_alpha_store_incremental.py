from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from gm.api import set_token

from chan520_skill.dynamic_universe import build_dynamic_universe
from chan520_skill.gm_data import gm_history, gm_symbol
from chan520_skill.models import KLine
from run_gm_alpha_backtest import safe_history, safe_history_instruments


def main() -> int:
    parser = argparse.ArgumentParser(description="Incrementally update local GM alpha SQLite store")
    parser.add_argument("--store", default=str(ROOT / "data" / "gm_alpha" / "chan520_alpha.sqlite"))
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    import os

    token = os.environ.get("GM_TOKEN", "").strip()
    if not token:
        print("ERROR: GM_TOKEN is required", flush=True)
        return 2
    set_token(token)
    store = Path(args.store)
    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)
    if start > end:
        raise SystemExit("--from-date cannot be after --to-date")
    if not args.no_backup:
        backup = store.with_suffix(store.suffix + f".bak_{date.today().isoformat().replace('-', '')}")
        if not backup.exists():
            shutil.copy2(store, backup)
            print(f"backup={backup}", flush=True)

    conn = sqlite3.connect(store)
    try:
        codes = [row[0] for row in conn.execute("select code from stock_meta order by code")]
        names = {row[0]: row[1] for row in conn.execute("select code, name from stock_meta")}
        print(f"incremental symbols={len(codes)} start={start} end={end}", flush=True)
        bars = fetch_recent_bars(codes, start, end, args.batch_size)
        statuses = safe_history_instruments([gm_symbol(code) for code in codes], start, end)
        _meta, index_rows = gm_history("000300", end, lookback_days=max((end - start).days + 10, 30))
        index_recent = [row for row in index_rows if start <= row.date <= end]
        with conn:
            insert_bars(conn, bars)
            insert_statuses(conn, statuses, names)
            insert_index(conn, index_recent)
            update_dynamic_universe(conn, codes, statuses, start, end)
            conn.execute("insert or replace into metadata(key, value) values (?, ?)", ("incremental_update_end", end.isoformat()))
        max_dates = max_store_dates(conn)
        report = {
            "status": "OK",
            "symbols": len(codes),
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "bars_symbols": len(bars),
            "status_dates": len(statuses),
            "index_rows": len(index_recent),
            "max_dates": max_dates,
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    finally:
        conn.close()


def fetch_recent_bars(codes: list[str], start: date, end: date, batch_size: int) -> dict[str, list[KLine]]:
    out: dict[str, list[KLine]] = {}
    symbols = [gm_symbol(code) for code in codes]
    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset : offset + batch_size]
        chunk = safe_history(batch, start.isoformat(), end.isoformat())
        for symbol, rows in chunk.items():
            code = symbol.split(".")[-1]
            out[code] = [row for row in rows if start <= row.date <= end]
        print(f"incremental bars {min(offset + len(batch), len(symbols))}/{len(symbols)}", flush=True)
    return out


def insert_bars(conn: sqlite3.Connection, bars: dict[str, list[KLine]]) -> None:
    rows = []
    for code, items in bars.items():
        for row in items:
            rows.append(
                (
                    code,
                    row.date.isoformat(),
                    row.open,
                    row.close,
                    row.high,
                    row.low,
                    row.volume,
                    row.amount,
                    row.amplitude,
                    row.pct_chg,
                    row.change,
                    row.turnover,
                )
            )
    conn.executemany(
        """
        insert or replace into daily_bars(
            code, trade_date, open, close, high, low, volume, amount, amplitude, pct_chg, change, turnover
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def insert_statuses(conn: sqlite3.Connection, statuses, names: dict[str, str]) -> None:
    rows = []
    for day, daily in statuses.items():
        for code, status in daily.items():
            rows.append(
                (
                    day.isoformat(),
                    code,
                    status.name or names.get(code, code),
                    status.listed_date.isoformat() if status.listed_date else None,
                    status.delisted_date.isoformat() if status.delisted_date else None,
                    int(bool(status.is_suspended)),
                )
            )
    conn.executemany(
        """
        insert or replace into instrument_status(
            trade_date, code, name, listed_date, delisted_date, is_suspended
        ) values (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def insert_index(conn: sqlite3.Connection, rows: list[KLine]) -> None:
    conn.executemany(
        """
        insert or replace into index_bars(
            code, trade_date, open, close, high, low, volume, amount, amplitude, pct_chg, change, turnover
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "000300",
                row.date.isoformat(),
                row.open,
                row.close,
                row.high,
                row.low,
                row.volume,
                row.amount,
                row.amplitude,
                row.pct_chg,
                row.change,
                row.turnover,
            )
            for row in rows
        ],
    )


def update_dynamic_universe(conn: sqlite3.Connection, codes: list[str], statuses, start: date, end: date) -> None:
    lookback_start = start - timedelta(days=420)
    histories: dict[str, list[KLine]] = {}
    for code in codes:
        rows = []
        for row in conn.execute(
            """
            select trade_date, open, close, high, low, volume, amount, amplitude, pct_chg, change, turnover
            from daily_bars
            where code = ? and trade_date >= ? and trade_date <= ?
            order by trade_date
            """,
            (code, lookback_start.isoformat(), end.isoformat()),
        ):
            rows.append(
                KLine(
                    date=date.fromisoformat(row[0]),
                    open=float(row[1]),
                    close=float(row[2]),
                    high=float(row[3]),
                    low=float(row[4]),
                    volume=float(row[5]),
                    amount=float(row[6]),
                    amplitude=float(row[7]),
                    pct_chg=float(row[8]),
                    change=float(row[9]),
                    turnover=float(row[10]),
                )
            )
        if rows:
            histories[code] = rows
    all_dates = sorted({row.date for rows in histories.values() for row in rows if start <= row.date <= end})
    dynamic = build_dynamic_universe(histories, statuses, all_dates)
    for day in all_dates:
        conn.execute("delete from dynamic_universe where trade_date = ?", (day.isoformat(),))
    conn.executemany(
        "insert or replace into dynamic_universe(trade_date, code) values (?, ?)",
        [(day.isoformat(), code) for day, members in dynamic.items() for code in members],
    )


def max_store_dates(conn: sqlite3.Connection) -> dict[str, str]:
    out = {}
    for table in ("daily_bars", "dynamic_universe", "index_bars", "instrument_status"):
        row = conn.execute(f"select trade_date from {table} order by trade_date desc limit 1").fetchone()
        out[table] = row[0] if row else ""
    return out


if __name__ == "__main__":
    raise SystemExit(main())
