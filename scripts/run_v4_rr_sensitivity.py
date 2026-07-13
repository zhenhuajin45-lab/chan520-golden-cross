from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# Make direct ``python scripts/...`` execution behave like ``python -m``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chan520_skill.backtest import BacktestConfig, portfolio_backtest_symbols
from chan520_skill.entry_filters import EntryFilterConfig
from chan520_skill.universe import industry_map, load_universe_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a reproducible V4 R:R sensitivity backtest.")
    parser.add_argument("--snapshot", required=True, help="point-in-time universe snapshot CSV")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD; must match snapshot as_of")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--split-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--min-rr", type=float, required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    members = load_universe_snapshot(args.snapshot, start)
    trades_path, metrics_path, metrics = portfolio_backtest_symbols(
        [member.code for member in members],
        start,
        date.fromisoformat(args.end),
        Path(args.output_dir),
        config=BacktestConfig(split_date=date.fromisoformat(args.split_date)),
        entry_config=EntryFilterConfig(min_rr=args.min_rr),
        sector_map=industry_map(members),
    )
    print(f"trades={int(metrics['trade_count'])} total_return={metrics['total_return']:.6f}")
    print(f"Trades: {trades_path.resolve()}")
    print(f"Metrics: {metrics_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
