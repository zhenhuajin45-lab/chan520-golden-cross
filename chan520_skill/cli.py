from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from .backtest import BacktestConfig, backtest_symbols
from .data import DataError, auto_history, eastmoney_history, load_csv, normalize_code, tencent_history, trim_to_date
from .regime import fetch_regime
from .report import render_markdown
from .strategy import analyze
from .universe import industry_map, load_universe_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chan520_skill")
    sub = parser.add_subparsers(dest="cmd", required=True)
    analyze_parser = sub.add_parser("analyze", help="analyze one A-share stock by Chan/520 rules")
    analyze_parser.add_argument("code", help="A-share code, for example 600288")
    analyze_parser.add_argument("--date", required=True, help="target trade date, YYYY-MM-DD")
    analyze_parser.add_argument("--source", choices=["auto", "eastmoney", "tencent", "csv"], default="auto")
    analyze_parser.add_argument("--csv", help="CSV path when --source csv")
    analyze_parser.add_argument("--output", help="output markdown path")
    analyze_parser.add_argument("--adjust", type=int, default=1, help="Eastmoney fqt value: 0 none, 1 qfq, 2 hfq")
    analyze_parser.add_argument("--macd-fast", type=int, default=12, help="MACD fast EMA period")
    analyze_parser.add_argument("--macd-slow", type=int, default=26, help="MACD slow EMA period; Tonghuashun screenshots often use 50")
    analyze_parser.add_argument("--macd-signal", type=int, default=9, help="MACD signal EMA period")
    analyze_parser.add_argument("--no-regime", action="store_true", help="disable market regime downgrade")
    analyze_parser.add_argument("--regime-index", default="000001", help="index symbol for regime filter")
    analyze_parser.add_argument("--min-history", type=int, default=250, help="minimum bars before long-term filters are trusted")
    analyze_parser.add_argument("--stop-mode", choices=["pct", "atr"], default="pct")
    analyze_parser.add_argument("--atr-k", type=float, default=2.0)
    analyze_parser.add_argument("--vol-base", choices=["prior_only", "incl_today"], default="prior_only")

    scan_parser = sub.add_parser("scan-market", help="scan HS non-ST A-share universe")
    scan_parser.add_argument("--date", required=True, help="target trade date, YYYY-MM-DD")
    scan_parser.add_argument("--output-dir", default="reports", help="directory for CSV and Markdown outputs")
    scan_parser.add_argument("--workers", type=int, default=16, help="parallel fetch workers")
    scan_parser.add_argument("--allow-low-coverage", action="store_true", help="return zero even when coverage is below threshold")

    backtest_parser = sub.add_parser("backtest", help="event-driven D+1 backtest")
    backtest_parser.add_argument("code", nargs="?", help="single A-share code")
    backtest_parser.add_argument("--basket", help="comma-separated codes")
    backtest_parser.add_argument("--start", required=True, help="start date, YYYY-MM-DD")
    backtest_parser.add_argument("--end", required=True, help="end date, YYYY-MM-DD")
    backtest_parser.add_argument("--split-date", help="optional IS/OOS split date, YYYY-MM-DD")
    backtest_parser.add_argument("--fill", choices=["next_open", "next_vwap_proxy"], default="next_open")
    backtest_parser.add_argument("--slippage-bps", type=float, default=5.0)
    backtest_parser.add_argument("--initial-cash", type=float, default=100000.0)
    backtest_parser.add_argument("--output-dir", default="reports/backtest")
    backtest_parser.add_argument("--regime-index", default="000300", help="index symbol for portfolio regime, default HS300")
    backtest_parser.add_argument("--use-sector", action="store_true", help="enable current sector breadth proxy gate")
    backtest_parser.add_argument("--signal-adjust", choices=["none", "qfq", "hfq"], default="none", help="signal price adjustment; none is causal default")
    backtest_parser.add_argument(
        "--strategy",
        choices=["strategy_v1_baseline", "strategy_v2_modular"],
        default="strategy_v1_baseline",
        help="strategy implementation used for the research run",
    )
    backtest_parser.add_argument("--universe-snapshot", help="point-in-time eligible-universe CSV; requires as_of=start date")

    args = parser.parse_args(argv)
    if args.cmd == "analyze":
        return run_analyze(args)
    if args.cmd == "scan-market":
        return run_scan_market(args)
    if args.cmd == "backtest":
        return run_backtest(args)
    return 2


def run_analyze(args: argparse.Namespace) -> int:
    target_date = date.fromisoformat(args.date)
    code = normalize_code(args.code)
    try:
        if args.source == "csv":
            if not args.csv:
                raise DataError("--csv is required when --source csv")
            meta, rows = load_csv(args.csv)
        elif args.source == "eastmoney":
            meta, rows = eastmoney_history(code, target_date, adjust=args.adjust)
        elif args.source == "tencent":
            meta, rows = tencent_history(code, target_date, adjust="qfq" if args.adjust == 1 else "hfq" if args.adjust == 2 else "none")
        else:
            meta, rows = auto_history(code, target_date, adjust=args.adjust)
        rows = trim_to_date(rows, target_date)
        regime_state = None
        if not args.no_regime:
            try:
                regime_state = fetch_regime(args.regime_index, target_date)
            except Exception as exc:
                print(f"WARNING: regime fetch failed, continue without regime filter: {exc}")
        report = analyze(
            meta,
            rows,
            target_date,
            macd_fast=args.macd_fast,
            macd_slow=args.macd_slow,
            macd_signal=args.macd_signal,
            regime_state=regime_state,
            min_history=args.min_history,
            stop_mode=args.stop_mode,
            atr_k=args.atr_k,
            vol_base=args.vol_base,
        )
        markdown = render_markdown(report)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    output = Path(args.output) if args.output else Path("reports") / f"{code}_{target_date.isoformat()}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")

    print(report.core_summary)
    print(f"报告已生成: {output.resolve()}")
    return 0


def run_scan_market(args: argparse.Namespace) -> int:
    # Scanning has optional third-party dependencies.  Keep analysis and
    # backtesting usable in minimal local environments.
    from .scanner import scan_market

    target_date = date.fromisoformat(args.date)
    try:
        csv_path, md_path, stats = scan_market(target_date, Path(args.output_dir), max_workers=args.workers)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    print(
        f"扫描完成：股票池 {stats['universe']}，输出 {stats['rows']}，"
        f"入选 {stats['selected']}，观察 {stats['watch']}，失败 {stats['failures']}"
    )
    print(f"CSV: {csv_path.resolve()}")
    print(f"报告: {md_path.resolve()}")
    if not args.allow_low_coverage and stats.get("coverage_below_threshold"):
        return 1
    return 0


def run_backtest(args: argparse.Namespace) -> int:
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    sector_map = None
    if args.universe_snapshot:
        if args.basket or args.code:
            print("ERROR: --universe-snapshot cannot be combined with code or --basket")
            return 2
        try:
            members = load_universe_snapshot(args.universe_snapshot, start)
        except Exception as exc:
            print(f"ERROR: invalid universe snapshot: {exc}")
            return 2
        symbols = [member.code for member in members]
        sector_map = industry_map(members)
    elif args.basket:
        symbols = [normalize_code(item) for item in args.basket.split(",") if item.strip()]
    elif args.code:
        symbols = [normalize_code(args.code)]
    else:
        print("ERROR: provide code or --basket")
        return 2
    config = BacktestConfig(
        initial_cash=args.initial_cash,
        fill=args.fill,
        slippage_bps=args.slippage_bps,
        split_date=date.fromisoformat(args.split_date) if args.split_date else None,
        regime_index=args.regime_index,
        use_sector=args.use_sector,
        signal_adjust=args.signal_adjust,
        strategy_mode=args.strategy,
    )
    try:
        trades_path, metrics_path, metrics = backtest_symbols(
            symbols, start, end, Path(args.output_dir), config=config, sector_map=sector_map
        )
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"回测完成：交易 {int(metrics['trade_count'])} 笔，期望值 {metrics['expectancy']:.2f}")
    print(f"Trades: {trades_path.resolve()}")
    print(f"Metrics: {metrics_path.resolve()}")
    return 0
