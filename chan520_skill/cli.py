from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from .data import DataError, auto_history, eastmoney_history, load_csv, normalize_code, tencent_history, trim_to_date
from .report import render_markdown
from .scanner import scan_market
from .strategy import analyze


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

    scan_parser = sub.add_parser("scan-market", help="scan HS non-ST A-share universe")
    scan_parser.add_argument("--date", required=True, help="target trade date, YYYY-MM-DD")
    scan_parser.add_argument("--output-dir", default="reports", help="directory for CSV and Markdown outputs")
    scan_parser.add_argument("--workers", type=int, default=16, help="parallel fetch workers")

    args = parser.parse_args(argv)
    if args.cmd == "analyze":
        return run_analyze(args)
    if args.cmd == "scan-market":
        return run_scan_market(args)
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
        report = analyze(meta, rows, target_date)
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
    return 0
