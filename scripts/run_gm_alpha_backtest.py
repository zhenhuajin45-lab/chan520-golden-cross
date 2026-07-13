"""Run the v5 Alpha research framework on GM historical data.

Credentials are read from ``GM_TOKEN`` only.  Pickle caches are local rerun
artifacts and are ignored by Git.
"""

from __future__ import annotations

import argparse
import csv
import os
import pickle
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import gm.api as gm_api
from gm.api import (
    get_history_instruments,
    get_instruments,
    history,
    set_token,
    stk_get_index_constituents,
    stk_get_symbol_industry,
)

from chan520_skill.dynamic_universe import InstrumentStatus, build_dynamic_universe
from chan520_skill.gm_data import gm_history, gm_symbol
from chan520_skill.models import KLine, StockMeta
from chan520_skill.portfolio_engine import PortfolioEngineConfig, run_alpha_portfolio


def main() -> int:
    parser = argparse.ArgumentParser(description="Run GM-backed Chan520 v5 alpha research")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2026-07-09")
    parser.add_argument("--universe", choices=["hs300", "all"], default="hs300")
    parser.add_argument("--output-dir", default="reports/backtest/v6")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-symbols", type=int, default=0, help="debug cap; 0 means no cap")
    parser.add_argument("--from-cache")
    parser.add_argument(
        "--sector-source",
        choices=["hybrid", "industry"],
        default="hybrid",
        help="hybrid tries GM symbol-sector first; industry uses the stable GM industry API only",
    )
    args = parser.parse_args()
    if args.from_cache:
        payload = pickle.loads(Path(args.from_cache).read_bytes())
        return run_from_payload(payload, Path(args.output_dir))

    token = os.environ.get("GM_TOKEN")
    if not token:
        print("ERROR: GM_TOKEN is required")
        return 2
    set_token(token)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    output_dir = Path(args.output_dir) / args.universe
    output_dir.mkdir(parents=True, exist_ok=True)

    symbols, names = discover_symbols(args.universe, start, end)
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]
    print(f"GM alpha universe={args.universe} candidates={len(symbols)}", flush=True)
    if not symbols:
        return 1

    lookback = max((end - start).days + 620, 1000)
    bars_cache = output_dir / f"gm_alpha_bars_{args.universe}_{start}_{end}.pkl"
    status_cache = output_dir / f"gm_alpha_status_{args.universe}_{start}_{end}.pkl"
    sector_cache = output_dir / f"gm_alpha_sector_{args.sector_source}_{args.universe}_{start}_{end}.pkl"
    universe_cache = output_dir / f"gm_alpha_universe_{args.universe}_{start}_{end}.pkl"
    index_cache = output_dir / f"gm_alpha_index_000300_{start}_{end}.pkl"
    rows_by_code = fetch_bars(symbols, end, lookback, args.batch_size, bars_cache)
    rows_by_code = {code: rows for code, rows in rows_by_code.items() if len(rows) >= 260}
    symbols = [code for code in symbols if code in rows_by_code]
    print(f"GM alpha bars usable={len(symbols)}", flush=True)
    if not symbols:
        return 1

    statuses_by_date = fetch_statuses(symbols, start, end, args.batch_size, status_cache)
    sector_map = fetch_sector_map(symbols, end, sector_cache, args.batch_size, source=args.sector_source)
    for code in symbols:
        sector_map.setdefault(code, f"UNMAPPED_{code[:3]}")
    print(f"GM alpha sector mapped={sum(1 for code in symbols if not sector_map[code].startswith('UNMAPPED_'))}/{len(symbols)}", flush=True)
    rows_by_code = {code: rows_by_code[code] for code in symbols}

    all_dates = sorted({row.date for rows in rows_by_code.values() for row in rows if start <= row.date <= end})
    eligible_by_date = load_pickle(universe_cache, None)
    if eligible_by_date is None:
        eligible_by_date = build_dynamic_universe(rows_by_code, statuses_by_date, all_dates)
        dump_pickle(universe_cache, eligible_by_date)
        print(f"GM alpha dynamic universe ready dates={len(eligible_by_date)}", flush=True)
    else:
        print(f"GM alpha dynamic universe cache dates={len(eligible_by_date)}", flush=True)
    write_universe_counts(output_dir / f"dynamic_universe_counts_{start}_{end}.csv", eligible_by_date)
    write_static_snapshot(output_dir / f"candidate_symbols_{args.universe}_{start}_{end}.csv", symbols, names, sector_map)

    index_rows = load_pickle(index_cache, None)
    if index_rows is None:
        print("GM alpha index fetch start 000300", flush=True)
        _index_meta, index_rows = gm_history("000300", end, lookback_days=lookback)
        dump_pickle(index_cache, index_rows)
        print(f"GM alpha index ready rows={len(index_rows)}", flush=True)
    else:
        print(f"GM alpha index cache rows={len(index_rows)}", flush=True)
    payload = {
        "symbols": symbols,
        "names": names,
        "sector_map": sector_map,
        "rows_by_code": rows_by_code,
        "statuses_by_date": statuses_by_date,
        "eligible_by_date": eligible_by_date,
        "index_rows": index_rows,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "universe": args.universe,
    }
    if os.environ.get("GM_ALPHA_SAVE_PAYLOAD") == "1":
        cache_path = output_dir / f"gm_alpha_cache_{args.universe}_{start}_{end}.pkl"
        print("GM alpha payload dump start", flush=True)
        dump_pickle(cache_path, payload)
        print(f"GM alpha cache ready: {cache_path.resolve()}", flush=True)
    else:
        print("GM alpha payload dump skipped; running portfolio in-process", flush=True)
    return run_from_payload(payload, Path(args.output_dir))


def run_from_payload(payload: dict, output_root: Path) -> int:
    symbols = payload["symbols"]
    names = payload["names"]
    rows_by_code = payload["rows_by_code"]
    sector_map = payload["sector_map"]
    eligible_by_date = payload["eligible_by_date"]
    index_rows = payload["index_rows"]
    start = date.fromisoformat(payload["start"])
    end = date.fromisoformat(payload["end"])
    output_dir = output_root / payload.get("universe", "hs300")

    def loader(code: str, _end: date, lookback_days: int = 1000, adjust: str = "none"):
        if adjust != "none":
            raise ValueError("GM alpha runner only supports unadjusted bars")
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
    write_research_report(output_dir / "research_report_v5_1.md", output_dir, metrics, start, end, len(symbols))
    print(f"GM alpha complete trades={int(metrics['trade_count'])} cagr={metrics['cagr']:.4f} max_dd={metrics['max_drawdown']:.4f}")
    print(f"Trades: {trades_path.resolve()}")
    print(f"Metrics: {metrics_path.resolve()}")
    return 0


def discover_symbols(kind: str, start: date, end: date) -> tuple[list[str], dict[str, str]]:
    if kind == "hs300":
        frame = stk_get_index_constituents("SHSE.000300", trade_date=start.isoformat())
        symbols = sorted({str(value) for value in frame["symbol"].tolist()})
    else:
        frame = get_instruments(exchanges=["SHSE", "SZSE"], sec_types=1, skip_suspended=False, skip_st=False, df=True)
        symbols = []
        for item in frame.to_dict("records"):
            listed = to_date(item.get("listed_date"))
            delisted = to_date(item.get("delisted_date"))
            if listed and listed > end:
                continue
            if delisted and delisted < start:
                continue
            symbols.append(str(item["symbol"]))
    codes = [symbol.split(".")[-1] for symbol in symbols]
    names = {}
    for item in get_instruments(exchanges=["SHSE", "SZSE"], sec_types=1, skip_suspended=False, skip_st=False, df=True).to_dict("records"):
        code = str(item["symbol"]).split(".")[-1]
        if code in codes:
            names[code] = str(item.get("sec_name") or code)
    return sorted(set(codes)), names


def fetch_bars(
    codes: list[str], end: date, lookback_days: int, batch_size: int, cache_path: Path | None = None
) -> dict[str, list[KLine]]:
    start_text = (end - timedelta(days=lookback_days)).isoformat()
    result: dict[str, list[KLine]] = load_pickle(cache_path, {})
    symbols = [gm_symbol(code) for code in codes]
    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset : offset + batch_size]
        missing = [symbol for symbol in batch if symbol.split(".")[-1] not in result]
        if missing:
            for symbol, rows in safe_history(missing, start_text, end.isoformat()).items():
                result[symbol.split(".")[-1]] = rows
            dump_pickle(cache_path, result)
        print(f"GM alpha bars {min(offset + len(batch), len(symbols))}/{len(symbols)}", flush=True)
    return result


def safe_history(symbols: list[str], start_text: str, end_text: str) -> dict[str, list[KLine]]:
    try:
        frame = history(
            symbols,
            "1d",
            start_text,
            end_text,
            fields="symbol,eob,open,high,low,close,volume,amount",
            skip_suspended=False,
            adjust=0,
            df=True,
        )
    except Exception:
        if len(symbols) > 1:
            mid = len(symbols) // 2
            left = safe_history(symbols[:mid], start_text, end_text)
            right = safe_history(symbols[mid:], start_text, end_text)
            left.update(right)
            return left
        start_day = date.fromisoformat(start_text)
        end_day = date.fromisoformat(end_text)
        if (end_day - start_day).days <= 900:
            raise
        chunks: list[tuple[date, date]] = []
        cursor = start_day
        while cursor <= end_day:
            chunk_end = min(cursor + timedelta(days=900), end_day)
            chunks.append((cursor, chunk_end))
            cursor = chunk_end + timedelta(days=1)
        merged: dict[str, list[KLine]] = {}
        for chunk_start, chunk_end in chunks:
            part = safe_history(symbols, chunk_start.isoformat(), chunk_end.isoformat())
            for symbol, rows in part.items():
                merged.setdefault(symbol, []).extend(rows)
        return {symbol: _dedupe_rows(rows) for symbol, rows in merged.items()}
    out: dict[str, list[KLine]] = {}
    if frame is not None and len(frame):
        for symbol, group in frame.groupby("symbol"):
            out[str(symbol)] = rows_from_group(group)
    return out


def _dedupe_rows(rows: list[KLine]) -> list[KLine]:
    by_date = {row.date: row for row in rows}
    return [by_date[day] for day in sorted(by_date)]


def fetch_statuses(
    codes: list[str], start: date, end: date, batch_size: int, cache_path: Path | None = None
) -> dict[date, dict[str, InstrumentStatus]]:
    output: dict[date, dict[str, InstrumentStatus]] = load_pickle(cache_path, {})
    cached_codes = {code for daily in output.values() for code in daily}
    symbols = [gm_symbol(code) for code in codes]
    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset : offset + batch_size]
        missing = [symbol for symbol in batch if symbol.split(".")[-1] not in cached_codes]
        if missing:
            for day, statuses in safe_history_instruments(missing, start, end).items():
                output.setdefault(day, {}).update(statuses)
            cached_codes.update(symbol.split(".")[-1] for symbol in missing)
            dump_pickle(cache_path, output)
        print(f"GM alpha status {min(offset + len(batch), len(symbols))}/{len(symbols)}", flush=True)
    return output


def safe_history_instruments(
    symbols: list[str], start: date, end: date
) -> dict[date, dict[str, InstrumentStatus]]:
    try:
        frame = get_history_instruments(symbols, start_date=start.isoformat(), end_date=end.isoformat(), df=True)
    except Exception:
        if len(symbols) <= 1:
            raise
        mid = len(symbols) // 2
        left = safe_history_instruments(symbols[:mid], start, end)
        right = safe_history_instruments(symbols[mid:], start, end)
        for day, statuses in right.items():
            left.setdefault(day, {}).update(statuses)
        return left
    output: dict[date, dict[str, InstrumentStatus]] = {}
    if frame is not None and len(frame):
        for item in frame.to_dict("records"):
            day = to_date(item.get("trade_date"))
            if day is None:
                continue
            symbol = str(item["symbol"])
            code = symbol.split(".")[-1]
            output.setdefault(day, {})[code] = InstrumentStatus(
                code=code,
                name=str(item.get("sec_name") or code),
                listed_date=to_date(item.get("listed_date")),
                delisted_date=to_date(item.get("delisted_date")),
                is_suspended=bool(int(item.get("is_suspended", 0) or 0)),
            )
    return output


def fetch_sector_map(
    codes: list[str],
    as_of: date,
    cache_path: Path | None,
    batch_size: int,
    *,
    source: str = "hybrid",
) -> dict[str, str]:
    out: dict[str, str] = load_pickle(cache_path, {})
    symbols = [gm_symbol(code) for code in codes if code not in out]
    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset : offset + batch_size]
        mapped = {} if source == "industry" else safe_symbol_sectors(batch, as_of)
        missing = [symbol for symbol in batch if symbol.split(".")[-1] not in mapped]
        mapped.update(safe_industries(missing, as_of))
        for code, sector in mapped.items():
            out[code] = sector
        dump_pickle(cache_path, out)
        print(f"GM alpha sector {min(offset + len(batch), len(symbols))}/{len(symbols)}", flush=True)
    return out


def safe_symbol_sectors(symbols: list[str], as_of: date, sector_type: str = "1003") -> dict[str, str]:
    fn = getattr(gm_api, "stk_get_symbol_sector", None)
    if fn is None or not symbols:
        return {}
    try:
        try:
            frame = fn(symbols, sector_type, date=as_of.isoformat())
        except TypeError:
            try:
                frame = fn(symbols, sector_type=sector_type, date=as_of.isoformat())
            except TypeError:
                frame = fn(symbols, sector_type)
    except Exception:
        if len(symbols) <= 1:
            return {}
        mid = len(symbols) // 2
        out = safe_symbol_sectors(symbols[:mid], as_of, sector_type)
        out.update(safe_symbol_sectors(symbols[mid:], as_of, sector_type))
        return out
    out: dict[str, str] = {}
    if frame is not None and len(frame):
        grouped: dict[str, list[str]] = {}
        for item in frame.to_dict("records"):
            code = str(item.get("symbol") or "").split(".")[-1]
            sector = _sector_name_from_record(item)
            if code and sector:
                grouped.setdefault(code, []).append(sector)
        for code, sectors in grouped.items():
            out[code] = _choose_sector_label(sectors)
    return out


def safe_industries(symbols: list[str], as_of: date) -> dict[str, str]:
    try:
        frame = stk_get_symbol_industry(symbols, source="zjh2012", level=1, date=as_of.isoformat())
    except Exception:
        if len(symbols) <= 1:
            return {}
        mid = len(symbols) // 2
        out = safe_industries(symbols[:mid], as_of)
        out.update(safe_industries(symbols[mid:], as_of))
        return out
    out: dict[str, str] = {}
    if frame is not None and len(frame):
        for item in frame.to_dict("records"):
            industry = str(item.get("industry_name") or item.get("industry_code") or "").strip()
            if industry:
                out[str(item["symbol"]).split(".")[-1]] = industry
    return out


def _sector_name_from_record(item: dict) -> str:
    for key in ("sector_name", "name", "industry_name", "category_name", "sector_code", "industry_code"):
        value = str(item.get(key) or "").strip()
        if value and not _generic_sector_label(value):
            return value
    return ""


def _choose_sector_label(sectors: list[str]) -> str:
    filtered = [sector for sector in sectors if not _generic_sector_label(sector)]
    if not filtered:
        return ""
    return sorted(set(filtered), key=lambda value: (len(value), value), reverse=True)[0]


def _generic_sector_label(label: str) -> bool:
    bad_words = ("全部", "A股", "沪深", "其他", "未知", "综合")
    return any(word in label for word in bad_words)


def rows_from_group(group) -> list[KLine]:
    rows: list[KLine] = []
    previous_close: float | None = None
    for item in group.sort_values("eob").to_dict("records"):
        open_price = float(item["open"])
        high = float(item["high"])
        low = float(item["low"])
        close = float(item["close"])
        rows.append(
            KLine(
                to_date(item["eob"]),
                open_price,
                close,
                high,
                low,
                float(item.get("volume", 0) or 0),
                float(item.get("amount", 0) or 0),
                (high - low) / open_price * 100 if open_price else 0.0,
                (close / previous_close - 1) * 100 if previous_close else 0.0,
                close - previous_close if previous_close else 0.0,
                0.0,
            )
        )
        previous_close = close
    return rows


def write_universe_counts(path: Path, eligible_by_date: dict[date, set[str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "eligible_count"])
        writer.writeheader()
        for day in sorted(eligible_by_date):
            writer.writerow({"date": day.isoformat(), "eligible_count": len(eligible_by_date[day])})


def write_static_snapshot(path: Path, codes: list[str], names: dict[str, str], sectors: dict[str, str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["code", "name", "industry"])
        writer.writeheader()
        for code in codes:
            writer.writerow({"code": code, "name": names.get(code, code), "industry": sectors.get(code, "")})


def write_research_report(path: Path, output_dir: Path, metrics: dict[str, float], start: date, end: date, symbols: int) -> None:
    yearly = output_dir / f"yearly_report_basket_{start}_{end}.csv"
    lines = [
        "# chan520 v5.1 Alpha Research Report",
        "",
        f"- Period: `{start}` to `{end}`",
        f"- Symbols loaded: `{symbols}`",
        "- Strategy: `strategy_v5_alpha_ranked`",
        "- Execution: close signal, next-session open fill, unadjusted GM prices.",
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
            "- Golden cross is not used as an entry optimizer in v5 alpha.",
            "- Entry requires trend pullback behavior: MA20 > MA60, price near MA20, reclaimed MA20, and renewed upside confirmation.",
            "- Alpha score combines trend structure, relative strength versus HS300, volume quality, and volatility risk.",
            "- Sector heat is a small prior, following the production sector logic pattern: participation, relative strength, breadth, amount and momentum form a 0-100 heat score; hot sectors can add at most 6 points and never replace the entry trigger.",
            "",
            "## Validation Label",
            "",
            "This report is `retrospective_research_validation` unless a separate frozen training window and untouched future window are supplied.",
            f"Yearly report: `{yearly.name}`",
            "",
            "## Evidence Files",
            "",
            f"- `{(output_dir / f'equity_curve_basket_{start}_{end}.csv').name}`",
            f"- `{(output_dir / f'trade_records_basket_{start}_{end}.csv').name}`",
            f"- `{(output_dir / f'drawdown_report_basket_{start}_{end}.csv').name}`",
            f"- `{(output_dir / f'sector_heat_basket_{start}_{end}.csv').name}`",
            f"- `{yearly.name}`",
            "- `candidate_funnel_daily.csv`",
            "- `candidate_selection_audit.csv`",
            "- `signal_snapshots.csv`",
            "- `trade_attribution.csv`",
            "",
            "## Acceptance",
            "",
            f"- Trade count >300: {'PASS' if metrics.get('trade_count', 0) > 300 else 'FAIL'}",
            f"- Max drawdown <25%: {'PASS' if abs(metrics.get('max_drawdown', 0)) < 0.25 else 'FAIL'}",
            f"- CAGR >10%: {'PASS' if metrics.get('cagr', 0) > 0.10 else 'FAIL'}",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def load_pickle(path: Path | None, default):
    if path is None or not path.exists():
        return default
    with path.open("rb") as fh:
        return pickle.load(fh)


def dump_pickle(path: Path | None, value) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        pickle.dump(value, fh, protocol=pickle.HIGHEST_PROTOCOL)


def to_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


if __name__ == "__main__":
    raise SystemExit(main())
