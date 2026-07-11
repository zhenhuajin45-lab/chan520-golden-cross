"""Build and run a durable local GM alpha research store.

The store is SQLite on purpose: it avoids Python-object pickle coupling and is
portable enough for repeated local research runs without extra dependencies.
"""

from __future__ import annotations

import argparse
import csv
import pickle
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from chan520_skill.models import KLine, StockMeta
from chan520_skill.portfolio_engine import PortfolioEngineConfig, run_alpha_portfolio


def main() -> int:
    parser = argparse.ArgumentParser(description="Build/run local SQLite store for chan520 v5 alpha")
    sub = parser.add_subparsers(dest="cmd", required=True)

    build = sub.add_parser("build")
    build.add_argument("--cache-dir", default="reports/backtest/v6/all")
    build.add_argument("--store", default="data/gm_alpha/chan520_alpha.sqlite")
    build.add_argument("--start", default="2016-01-01")
    build.add_argument("--end", default="2026-07-09")
    build.add_argument("--universe", default="all")
    build.add_argument("--sector-source", default="industry")
    build.add_argument("--rebuild", action="store_true")

    run = sub.add_parser("run")
    run.add_argument("--store", default="data/gm_alpha/chan520_alpha.sqlite")
    run.add_argument("--start", default="2016-01-01")
    run.add_argument("--end", default="2026-07-09")
    run.add_argument("--output-dir", default="reports/backtest/v6/store_all")
    run.add_argument("--max-symbols", type=int, default=0)
    run.add_argument("--lookback-days", type=int, default=900)

    args = parser.parse_args()
    if args.cmd == "build":
        build_store(args)
        return 0
    run_store(args)
    return 0


def build_store(args) -> None:
    cache_dir = Path(args.cache_dir)
    store = Path(args.store)
    if args.rebuild and store.exists():
        store.unlink()
    store.parent.mkdir(parents=True, exist_ok=True)
    start = args.start
    end = args.end
    universe = args.universe
    sector_source = args.sector_source

    bars = load_pickle(cache_dir / f"gm_alpha_bars_{universe}_{start}_{end}.pkl")
    statuses = load_pickle(cache_dir / f"gm_alpha_status_{universe}_{start}_{end}.pkl")
    sectors = load_pickle(cache_dir / f"gm_alpha_sector_{sector_source}_{universe}_{start}_{end}.pkl")
    dynamic = load_pickle(cache_dir / f"gm_alpha_universe_{universe}_{start}_{end}.pkl")
    index_rows = load_pickle(cache_dir / f"gm_alpha_index_000300_{start}_{end}.pkl")

    conn = sqlite3.connect(store)
    try:
        configure_sqlite(conn)
        create_schema(conn)
        with conn:
            conn.execute("delete from daily_bars")
            conn.execute("delete from instrument_status")
            conn.execute("delete from sector_map")
            conn.execute("delete from dynamic_universe")
            conn.execute("delete from index_bars")
            conn.execute("delete from stock_meta")
            conn.execute("delete from metadata")
        insert_bars(conn, bars)
        insert_statuses(conn, statuses)
        insert_sectors(conn, sectors)
        insert_universe(conn, dynamic)
        insert_index(conn, index_rows)
        insert_meta(conn, bars, statuses)
        with conn:
            conn.executemany(
                "insert or replace into metadata(key, value) values (?, ?)",
                [
                    ("start", start),
                    ("end", end),
                    ("universe", universe),
                    ("sector_source", sector_source),
                    ("bar_symbols", str(len(bars))),
                    ("universe_dates", str(len(dynamic))),
                ],
            )
        print(f"SQLite store ready: {store.resolve()}", flush=True)
    finally:
        conn.close()


def run_store(args) -> None:
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    output_dir = Path(args.output_dir)
    conn = sqlite3.connect(args.store)
    try:
        names = load_names(conn)
        sector_map = load_sector_map(conn)
        rows_by_code = load_daily_bars(conn, start, end, args.lookback_days)
        rows_by_code = {code: rows for code, rows in rows_by_code.items() if len(rows) >= 260}
        if args.max_symbols:
            keep = sorted(rows_by_code)[: args.max_symbols]
            rows_by_code = {code: rows_by_code[code] for code in keep}
        symbols = sorted(rows_by_code)
        eligible_by_date = load_dynamic_universe(conn, start, end, set(symbols))
        index_rows = load_index_bars(conn, end)
    finally:
        conn.close()

    def loader(code: str, _end: date, lookback_days: int = 1000, adjust: str = "none"):
        if adjust != "none":
            raise ValueError("SQLite store only contains unadjusted bars")
        market = 1 if code.startswith(("5", "6", "9")) else 0
        return StockMeta(code, names.get(code, code), market), rows_by_code[code]

    trades_path, metrics_path, metrics = run_alpha_portfolio(
        symbols,
        start,
        end,
        output_dir,
        loader,
        sector_map,
        index_rows,
        eligible_by_date,
        PortfolioEngineConfig(max_positions=5),
    )
    write_research_report(output_dir / "research_report_v5_alpha.md", output_dir, metrics, start, end, len(symbols))
    print(f"SQLite alpha complete trades={int(metrics['trade_count'])} cagr={metrics['cagr']:.4f} max_dd={metrics['max_drawdown']:.4f}")
    print(f"Trades: {trades_path.resolve()}")
    print(f"Metrics: {metrics_path.resolve()}")


def configure_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma synchronous=normal")
    conn.execute("pragma temp_store=memory")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists daily_bars(
            code text not null,
            trade_date text not null,
            open real not null,
            close real not null,
            high real not null,
            low real not null,
            volume real not null,
            amount real not null,
            amplitude real not null,
            pct_chg real not null,
            change real not null,
            turnover real not null,
            primary key(code, trade_date)
        );
        create table if not exists instrument_status(
            trade_date text not null,
            code text not null,
            name text not null,
            listed_date text,
            delisted_date text,
            is_suspended integer not null,
            primary key(trade_date, code)
        );
        create table if not exists sector_map(
            code text primary key,
            sector text not null
        );
        create table if not exists dynamic_universe(
            trade_date text not null,
            code text not null,
            primary key(trade_date, code)
        );
        create table if not exists index_bars(
            code text not null,
            trade_date text not null,
            open real not null,
            close real not null,
            high real not null,
            low real not null,
            volume real not null,
            amount real not null,
            amplitude real not null,
            pct_chg real not null,
            change real not null,
            turnover real not null,
            primary key(code, trade_date)
        );
        create table if not exists stock_meta(
            code text primary key,
            name text not null
        );
        create table if not exists metadata(
            key text primary key,
            value text not null
        );
        create index if not exists idx_daily_bars_date on daily_bars(trade_date);
        create index if not exists idx_dynamic_universe_code on dynamic_universe(code);
        """
    )


def insert_bars(conn: sqlite3.Connection, bars: dict[str, list[KLine]]) -> None:
    rows = []
    total = 0
    for idx, (code, klines) in enumerate(bars.items(), 1):
        rows.extend(bar_tuple(code, row) for row in klines)
        if len(rows) >= 100_000:
            with conn:
                conn.executemany("insert or replace into daily_bars values (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            total += len(rows)
            rows.clear()
            print(f"store bars rows={total} symbols={idx}/{len(bars)}", flush=True)
    if rows:
        with conn:
            conn.executemany("insert or replace into daily_bars values (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        total += len(rows)
    print(f"store bars complete rows={total}", flush=True)


def insert_statuses(conn: sqlite3.Connection, statuses: dict[date, dict[str, object]]) -> None:
    rows = []
    total = 0
    for day_idx, (day, by_code) in enumerate(sorted(statuses.items()), 1):
        day_text = day.isoformat()
        for code, status in by_code.items():
            rows.append(
                (
                    day_text,
                    code,
                    getattr(status, "name", code),
                    date_text(getattr(status, "listed_date", None)),
                    date_text(getattr(status, "delisted_date", None)),
                    1 if getattr(status, "is_suspended", False) else 0,
                )
            )
        if len(rows) >= 100_000:
            with conn:
                conn.executemany("insert or replace into instrument_status values (?,?,?,?,?,?)", rows)
            total += len(rows)
            rows.clear()
            print(f"store statuses rows={total} dates={day_idx}/{len(statuses)}", flush=True)
    if rows:
        with conn:
            conn.executemany("insert or replace into instrument_status values (?,?,?,?,?,?)", rows)
        total += len(rows)
    print(f"store statuses complete rows={total}", flush=True)


def insert_sectors(conn: sqlite3.Connection, sectors: dict[str, str]) -> None:
    with conn:
        conn.executemany("insert or replace into sector_map values (?, ?)", sorted(sectors.items()))
    print(f"store sectors complete rows={len(sectors)}", flush=True)


def insert_universe(conn: sqlite3.Connection, dynamic: dict[date, set[str]]) -> None:
    rows = []
    total = 0
    for day_idx, (day, codes) in enumerate(sorted(dynamic.items()), 1):
        day_text = day.isoformat()
        rows.extend((day_text, code) for code in sorted(codes))
        if len(rows) >= 100_000:
            with conn:
                conn.executemany("insert or replace into dynamic_universe values (?, ?)", rows)
            total += len(rows)
            rows.clear()
            print(f"store universe rows={total} dates={day_idx}/{len(dynamic)}", flush=True)
    if rows:
        with conn:
            conn.executemany("insert or replace into dynamic_universe values (?, ?)", rows)
        total += len(rows)
    print(f"store universe complete rows={total}", flush=True)


def insert_index(conn: sqlite3.Connection, index_rows: list[KLine]) -> None:
    with conn:
        conn.executemany("insert or replace into index_bars values (?,?,?,?,?,?,?,?,?,?,?,?)", [bar_tuple("000300", row) for row in index_rows])
    print(f"store index complete rows={len(index_rows)}", flush=True)


def insert_meta(conn: sqlite3.Connection, bars: dict[str, list[KLine]], statuses: dict[date, dict[str, object]]) -> None:
    names = {code: code for code in bars}
    for by_code in statuses.values():
        for code, status in by_code.items():
            name = getattr(status, "name", "") or code
            if code in names and name:
                names[code] = name
    with conn:
        conn.executemany("insert or replace into stock_meta values (?, ?)", sorted(names.items()))
    print(f"store meta complete rows={len(names)}", flush=True)


def load_names(conn: sqlite3.Connection) -> dict[str, str]:
    return {code: name for code, name in conn.execute("select code, name from stock_meta")}


def load_sector_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {code: sector for code, sector in conn.execute("select code, sector from sector_map")}


def load_daily_bars(conn: sqlite3.Connection, start: date, end: date, lookback_days: int) -> dict[str, list[KLine]]:
    out: dict[str, list[KLine]] = defaultdict(list)
    from_date = start - timedelta(days=max(0, lookback_days))
    query = """
        select code, trade_date, open, close, high, low, volume, amount, amplitude, pct_chg, change, turnover
        from daily_bars
        where trade_date between ? and ?
        order by code, trade_date
    """
    count = 0
    for row in conn.execute(query, (from_date.isoformat(), end.isoformat())):
        code = row[0]
        out[code].append(kline_from_row(row[1:]))
        count += 1
        if count % 500_000 == 0:
            print(f"load bars rows={count}", flush=True)
    print(f"load bars complete symbols={len(out)} rows={count}", flush=True)
    return dict(out)


def load_dynamic_universe(conn: sqlite3.Connection, start: date, end: date, symbols: set[str]) -> dict[date, set[str]]:
    out: dict[date, set[str]] = defaultdict(set)
    query = """
        select trade_date, code from dynamic_universe
        where trade_date between ? and ?
        order by trade_date, code
    """
    count = 0
    for day_text, code in conn.execute(query, (start.isoformat(), end.isoformat())):
        if code in symbols:
            out[date.fromisoformat(day_text)].add(code)
            count += 1
    print(f"load universe complete dates={len(out)} rows={count}", flush=True)
    return dict(out)


def load_index_bars(conn: sqlite3.Connection, end: date) -> list[KLine]:
    query = """
        select trade_date, open, close, high, low, volume, amount, amplitude, pct_chg, change, turnover
        from index_bars
        where code = '000300' and trade_date <= ?
        order by trade_date
    """
    rows = [kline_from_row(row) for row in conn.execute(query, (end.isoformat(),))]
    print(f"load index complete rows={len(rows)}", flush=True)
    return rows


def bar_tuple(code: str, row: KLine) -> tuple:
    return (
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


def kline_from_row(row) -> KLine:
    return KLine(
        date.fromisoformat(row[0]),
        float(row[1]),
        float(row[2]),
        float(row[3]),
        float(row[4]),
        float(row[5]),
        float(row[6]),
        float(row[7]),
        float(row[8]),
        float(row[9]),
        float(row[10]),
    )


def date_text(value) -> str | None:
    return value.isoformat() if value else None


def load_pickle(path: Path):
    with path.open("rb") as fh:
        return pickle.load(fh)


def write_research_report(path: Path, output_dir: Path, metrics: dict[str, float], start: date, end: date, symbols: int) -> None:
    yearly = output_dir / f"yearly_report_basket_{start}_{end}.csv"
    lines = [
        "# chan520 v5 Alpha Research Report",
        "",
        f"- Period: `{start}` to `{end}`",
        f"- Symbols loaded from SQLite store: `{symbols}`",
        "- Strategy: `strategy_v5_alpha`",
        "- Execution: close signal, next-session open fill, unadjusted GM prices.",
        "- Data source: durable local SQLite store generated from GM historical caches.",
        "",
        "## Summary Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key in ("trade_count", "total_return", "cagr", "max_drawdown", "sharpe", "calmar", "win_rate", "payoff_ratio", "profit_factor"):
        lines.append(f"| {key} | {metrics.get(key, 0):.6f} |")
    lines.extend(
        [
            "",
            "## Alpha Source",
            "",
            "- Golden cross is trend confirmation, not the direct entry optimizer.",
            "- Entry requires trend pullback behavior plus renewed upside confirmation.",
            "- Alpha score combines trend structure, relative strength, volume quality, volatility risk, and capped sector heat prior.",
            "",
            "## Evidence Files",
            "",
            f"- `{(output_dir / f'equity_curve_basket_{start}_{end}.csv').name}`",
            f"- `{(output_dir / f'trade_records_basket_{start}_{end}.csv').name}`",
            f"- `{(output_dir / f'drawdown_report_basket_{start}_{end}.csv').name}`",
            f"- `{(output_dir / f'sector_heat_basket_{start}_{end}.csv').name}`",
            f"- `{yearly.name}`",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
