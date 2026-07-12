from __future__ import annotations

import sqlite3

from chan520_skill.evidence_manifest import build_evidence_manifest, write_evidence_manifest
from chan520_skill.portfolio_engine import PortfolioEngineConfig
from chan520_skill.research_config import ResearchRunConfig


def test_evidence_manifest_schema_and_hashes(tmp_path) -> None:
    store = tmp_path / "sample.sqlite"
    conn = sqlite3.connect(store)
    try:
        conn.executescript(
            """
            create table daily_bars(code text, trade_date text);
            create table instrument_status(trade_date text, code text);
            create table sector_map(code text, sector text);
            create table dynamic_universe(trade_date text, code text);
            create table index_bars(code text, trade_date text, open real, close real, high real, low real, volume real, amount real);
            create table stock_meta(code text, name text);
            create table metadata(key text, value text);
            insert into daily_bars values ('600001', '2026-01-05');
            insert into dynamic_universe values ('2026-01-05', '600001');
            insert into sector_map values ('600001', 'tech');
            insert into index_bars values ('000300', '2026-01-05', 1, 1, 1, 1, 1, 1);
            """
        )
        conn.commit()
    finally:
        conn.close()

    manifest = build_evidence_manifest(
        store_path=store,
        symbols=["600001"],
        ordered_symbols=["600001"],
        config=PortfolioEngineConfig(strategy_mode="strategy_v5_alpha_ranked"),
        full_config=ResearchRunConfig(),
        cwd=tmp_path,
    )
    required = {
        "git_commit",
        "run_code_commit",
        "artifact_commit",
        "git_dirty",
        "source_tree_hash",
        "strategy_config_hash",
        "full_config_hash",
        "backtest_config_hash",
        "risk_config_hash",
        "entry_config_hash",
        "alpha_config_hash",
        "sector_config_hash",
        "statistics_config_hash",
        "sqlite_sha256",
        "schema_hash",
        "daily_bars_logical_hash",
        "instrument_status_logical_hash",
        "tables",
        "symbol_universe_hash",
        "ordered_symbol_list_hash",
        "dynamic_universe_hash",
        "sector_map_hash",
        "index_data_hash",
    }
    assert required <= set(manifest)
    assert manifest["tables"]["daily_bars"]["row_count"] == 1
    assert manifest["tables"]["daily_bars"]["min_date"] == "2026-01-05"

    out = tmp_path / "evidence_manifest.json"
    write_evidence_manifest(out, manifest)
    assert out.read_text(encoding="utf-8").startswith("{")
