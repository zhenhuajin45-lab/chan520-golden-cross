"""Run the local portfolio engine on 掘金量化 historical data.

Credentials are read only from ``GM_TOKEN`` and are never written to files.
The default universe is the point-in-time HS300 membership at ``--start``.
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

from chan520_skill.backtest import BacktestConfig, portfolio_backtest_symbols
from chan520_skill.gm_data import gm_history, gm_symbol
from chan520_skill.models import KLine, StockMeta


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Chan520 backtest with GM historical data")
    parser.add_argument("--start", default="2026-01-05")
    parser.add_argument("--end", default="2026-07-09")
    parser.add_argument("--strategy", choices=["strategy_v1_baseline", "strategy_v2_modular"], default="strategy_v2_modular")
    parser.add_argument("--universe", choices=["hs300", "all"], default="hs300")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--output-dir", default="reports/backtest/gm/2026")
    parser.add_argument("--from-cache", help="run the local engine from a prepared GM cache")
    args = parser.parse_args()
    if args.from_cache:
        payload = pickle.loads(Path(args.from_cache).read_bytes())
        return run_engine(payload, args.strategy, Path(args.output_dir))

    token = os.environ.get("GM_TOKEN")
    if not token:
        print("ERROR: GM_TOKEN is required")
        return 2
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    symbols, names, sector_map = discover_universe(args.universe, start, token)
    if not symbols:
        print("ERROR: GM universe is empty")
        return 1
    print(f"GM universe={args.universe} eligible={len(symbols)}", flush=True)
    output_dir = Path(args.output_dir) / args.universe
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_snapshot(output_dir / f"universe_{args.universe}_{start.isoformat()}.csv", start, symbols, names, sector_map)

    lookback = max((end - start).days + 620, 1000)
    rows_by_code = fetch_bars(symbols, start, end, lookback, token, args.batch_size)
    usable = [code for code in symbols if len(rows_by_code.get(code, [])) >= 61]
    print(f"GM bars usable={len(usable)}/{len(symbols)}", flush=True)
    if not usable:
        print("ERROR: no usable GM histories")
        return 1
    _index_meta, index_rows = gm_history("000300", end, lookback_days=lookback, token=token)
    cache_path = output_dir / f"gm_cache_{args.universe}_{start.isoformat()}_{end.isoformat()}.pkl"
    cache_path.write_bytes(
        pickle.dumps(
            {
                "symbols": usable,
                "names": names,
                "sector_map": sector_map,
                "rows_by_code": rows_by_code,
                "index_rows": index_rows,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "universe": args.universe,
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    )
    print(f"GM cache ready: {cache_path.resolve()}", flush=True)
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--from-cache", str(cache_path), "--strategy", args.strategy, "--output-dir", args.output_dir],
        check=False,
    ).returncode


def run_engine(payload: dict, strategy: str, output_root: Path) -> int:
    symbols = payload["symbols"]
    names = payload["names"]
    sector_map = payload["sector_map"]
    rows_by_code = payload["rows_by_code"]
    index_rows = payload["index_rows"]
    start = date.fromisoformat(payload["start"])
    end = date.fromisoformat(payload["end"])
    output_dir = output_root / payload.get("universe", "hs300")

    def loader(code: str, _end: date, lookback_days: int = 1000, adjust: str = "none"):
        if adjust != "none":
            raise ValueError("GM runner only supports unadjusted execution and signal bars")
        market = 1 if code.startswith(("5", "6", "9")) else 0
        return StockMeta(code, names.get(code, code), market), rows_by_code[code]

    config = BacktestConfig(strategy_mode=strategy, split_date=date(2026, 4, 1), regime_index="000300")
    trades_path, metrics_path, metrics = portfolio_backtest_symbols(
        symbols,
        start,
        end,
        output_dir,
        config=config,
        sector_map=sector_map,
        index_rows=index_rows,
        history_loader=loader,
    )
    print(f"GM backtest complete trades={int(metrics['trade_count'])} expectancy={metrics['expectancy']:.2f}")
    print(f"Trades: {trades_path.resolve()}")
    print(f"Metrics: {metrics_path.resolve()}")
    return 0


def discover_universe(kind: str, as_of: date, token: str) -> tuple[list[str], dict[str, str], dict[str, str]]:
    from gm.api import get_history_instruments, get_instruments, set_token, stk_get_index_constituents, stk_get_symbol_industry

    set_token(token)
    if kind == "hs300":
        constituents = stk_get_index_constituents("SHSE.000300", trade_date=as_of.isoformat())
        symbols = [str(value) for value in constituents["symbol"].tolist()]
    else:
        instruments = get_instruments(
            exchanges=["SHSE", "SZSE"], sec_types=1, skip_suspended=False, skip_st=False, df=True
        )
        symbols = [str(value) for value in instruments["symbol"].tolist()]
    symbols = sorted(set(symbols))
    historical = get_history_instruments(symbols, start_date=as_of.isoformat(), end_date=as_of.isoformat(), df=True)
    names: dict[str, str] = {}
    eligible: list[str] = []
    for item in historical.to_dict("records"):
        symbol = str(item["symbol"])
        name = str(item.get("sec_name") or symbol)
        listed = _to_date(item.get("listed_date"))
        delisted = _to_date(item.get("delisted_date"))
        if listed and listed > as_of:
            continue
        if delisted and delisted < as_of:
            continue
        if int(item.get("is_suspended", 0) or 0) != 0:
            continue
        if any(marker in name.upper() for marker in ("ST", "退")):
            continue
        code = symbol.split(".")[-1]
        names[code] = name
        eligible.append(code)
    industry = stk_get_symbol_industry(
        [gm_symbol(code) for code in eligible], source="zjh2012", level=1, date=as_of.isoformat()
    )
    sector_map = {
        str(item["symbol"]).split(".")[-1]: str(item.get("industry_name") or item.get("industry_code") or "")
        for item in industry.to_dict("records")
        if str(item.get("industry_name") or item.get("industry_code") or "").strip()
    }
    eligible = [code for code in eligible if code in sector_map]
    return eligible, names, sector_map


def fetch_bars(
    codes: list[str], start: date, end: date, lookback_days: int, token: str, batch_size: int
) -> dict[str, list[KLine]]:
    from gm.api import history, set_token

    set_token(token)
    start_text = (end - timedelta(days=lookback_days)).isoformat()
    result: dict[str, list[KLine]] = {}
    symbols = [gm_symbol(code) for code in codes]
    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset : offset + batch_size]
        frame = history(
            batch,
            "1d",
            start_text,
            end.isoformat(),
            fields="symbol,eob,open,high,low,close,volume,amount",
            skip_suspended=False,
            adjust=0,
            df=True,
        )
        if frame is not None and len(frame):
            for symbol, group in frame.groupby("symbol"):
                code = str(symbol).split(".")[-1]
                result[code] = _rows_from_group(group)
        print(f"GM bars {min(offset + len(batch), len(symbols))}/{len(symbols)}", flush=True)
    return result


def _rows_from_group(group) -> list[KLine]:
    rows: list[KLine] = []
    previous_close: float | None = None
    for item in group.sort_values("eob").to_dict("records"):
        open_price = float(item["open"])
        high = float(item["high"])
        low = float(item["low"])
        close = float(item["close"])
        day = _to_date(item["eob"])
        rows.append(
            KLine(
                day,
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


def _to_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.date() if isinstance(value, datetime) else value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def _write_snapshot(path: Path, as_of: date, codes: list[str], names: dict[str, str], sectors: dict[str, str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["as_of", "code", "name", "industry", "eligible"])
        writer.writeheader()
        for code in codes:
            writer.writerow({"as_of": as_of.isoformat(), "code": code, "name": names.get(code, code), "industry": sectors[code], "eligible": "true"})


if __name__ == "__main__":
    raise SystemExit(main())
