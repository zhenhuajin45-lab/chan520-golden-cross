from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

from chan520_skill import backtest as bt
from chan520_skill.backtest import BacktestConfig, CandidateSignal, portfolio_backtest_symbols
from chan520_skill.evidence_codes import ReasonCode, SectorDataStatus
from chan520_skill.models import KLine, RegimeState, StockMeta
from chan520_skill.risk import RiskConfig


SYMBOLS = ["600001", "600002", "600003", "600004", "600005", "600006"]
START = date(2026, 1, 1)
END = date(2026, 4, 30)


def make_rows(code: str) -> list[KLine]:
    offset = int(code[-1]) * 0.03
    rows: list[KLine] = []
    prev = 10.0 + offset
    for idx in range(180):
        day = date(2025, 10, 1) + timedelta(days=idx)
        close = 10.0 + offset + idx * 0.04
        open_price = close * 0.995
        rows.append(
            KLine(
                date=day,
                open=open_price,
                close=close,
                high=close * 1.01,
                low=close * 0.99,
                volume=1_000_000,
                amount=500_000_000,
                amplitude=2.0,
                pct_chg=(close / prev - 1) * 100 if idx else 0.0,
                change=close - prev if idx else 0.0,
                turnover=2.0,
            )
        )
        prev = close
    return rows


def make_index_rows() -> list[KLine]:
    rows: list[KLine] = []
    prev = 100.0
    for idx in range(220):
        day = date(2025, 8, 1) + timedelta(days=idx)
        close = 100.0 + idx * 0.2
        rows.append(KLine(day, close, close, close + 1, close - 1, 1_000_000, 1_000_000_000, 1, (close / prev - 1) * 100 if idx else 0, close - prev if idx else 0, 1))
        prev = close
    return rows


def fake_candidate_signals(meta, _rows, all_dates, _regime_by_date, *_args, **_kwargs):
    out = {}
    score = 90.0 - int(meta.code[-1])
    for day in all_dates:
        if day.day % 7 != 0:
            continue
        out[day] = CandidateSignal(
            candidate_id=f"cand_{day.isoformat()}_{meta.code}",
            code=meta.code,
            name=meta.name,
            date=day,
            industry="tech",
            market_regime="NORMAL",
            eligible=True,
            market_ok=True,
            alpha_pass=True,
            entry_pass=True,
            regime_pass=True,
            sector_data_status=SectorDataStatus.MAPPED.value,
            four_no_pass=True,
            stop_valid=True,
            rr_pass=True,
            sizing_feasible=True,
            alpha_total=score,
            trend_score=30.0,
            relative_strength_score=score / 10,
            volume_quality_score=20.0,
            risk_score=20.0,
            sector_bonus=0.0,
            sector_heat_score=0.0,
            entry_score=70.0,
            ranking_score=score,
            signal_close=10.0,
            planned_stop=9.0,
            planned_target=13.0,
            ex_ante_rr=3.0,
            initial_risk_cash=100.0,
            position_neutral_shares=100,
            hard_pass=True,
            reason_codes=(ReasonCode.OK.value,),
            reasons=("unit",),
        )
    return out


def run_case(monkeypatch, tmp_path: Path, symbols: list[str]) -> dict[str, str]:
    histories = {code: make_rows(code) for code in SYMBOLS}

    def loader(code: str, _end: date, lookback_days: int = 1000, adjust: str = "none"):
        return StockMeta(code, code, 1), list(histories[code])

    monkeypatch.setattr(bt, "_analyze_v5_candidate_signals", fake_candidate_signals)
    monkeypatch.setattr(bt, "_initial_stop", lambda row, _point, _config: row.close - 1.0)
    monkeypatch.setattr(bt, "_target_price", lambda _rows, _day, close, _config, point=None: close + 3.0)
    out = tmp_path / "_".join(symbols)
    portfolio_backtest_symbols(
        symbols,
        START,
        END,
        out,
        config=BacktestConfig(initial_cash=100000, strategy_mode="strategy_v5_alpha_ranked", require_industry=False),
        risk_config=RiskConfig(max_sector_pct=1.0, cash_reserve_pct=0.0),
        sector_map={code: "tech" for code in SYMBOLS},
        index_rows=make_index_rows(),
        history_loader=loader,
        eligible_by_date={START + timedelta(days=idx): set(SYMBOLS) for idx in range((END - START).days + 1)},
        max_positions=3,
    )
    names = [
        "candidate_selection_audit.csv",
        "pending_orders.csv",
        "candidate_funnel_daily.csv",
        f"fills_basket_{START}_{END}.csv",
        f"trades_basket_{START}_{END}.csv",
        f"equity_curve_basket_{START}_{END}.csv",
        f"metrics_basket_{START}_{END}.md",
    ]
    return {name: (out / name).read_text(encoding="utf-8-sig") for name in names}


def test_full_ranked_portfolio_order_invariance(monkeypatch, tmp_path) -> None:
    baseline = run_case(monkeypatch, tmp_path / "asc", sorted(SYMBOLS))
    cases = [list(reversed(sorted(SYMBOLS)))]
    for seed in (520, 2026, 42):
        shuffled = sorted(SYMBOLS)
        random.Random(seed).shuffle(shuffled)
        cases.append(shuffled)

    for idx, symbols in enumerate(cases):
        assert run_case(monkeypatch, tmp_path / f"case_{idx}", symbols) == baseline
