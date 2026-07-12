from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


DATE_COLUMNS = {
    "daily_bars": "trade_date",
    "instrument_status": "trade_date",
    "sector_map": None,
    "dynamic_universe": "trade_date",
    "index_bars": "trade_date",
    "stock_meta": None,
    "metadata": None,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_hash_lines(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def config_hash(config: Any) -> str:
    value = asdict(config) if is_dataclass(config) else dict(config)
    return stable_hash_json(value)


def git_commit(cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )
    except Exception:
        return "unknown"
    return result.stdout.strip()


def git_dirty(cwd: Path | None = None) -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )
    except Exception:
        return True
    return bool(result.stdout.strip())


def source_tree_hash(cwd: Path | None = None, roots: tuple[str, ...] = ("chan520_skill", "scripts", "tests", ".github")) -> str:
    base = cwd or Path.cwd()
    digest = hashlib.sha256()
    for root in roots:
        path = base / root
        if not path.exists():
            continue
        for item in sorted(path.rglob("*")):
            if not item.is_file() or "__pycache__" in item.parts or item.suffix == ".pyc":
                continue
            rel = item.relative_to(base).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(sha256_file(item).encode("ascii"))
            digest.update(b"\n")
    return digest.hexdigest()


def sqlite_table_stats(conn: sqlite3.Connection) -> dict[str, dict[str, int | str | None]]:
    out: dict[str, dict[str, int | str | None]] = {}
    for table, date_col in DATE_COLUMNS.items():
        exists = conn.execute(
            "select 1 from sqlite_master where type='table' and name=?",
            (table,),
        ).fetchone()
        if not exists:
            continue
        row_count = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
        min_date = max_date = None
        if date_col:
            min_date, max_date = conn.execute(f"select min({date_col}), max({date_col}) from {table}").fetchone()
        out[table] = {"row_count": row_count, "min_date": min_date, "max_date": max_date}
    return out


def query_hash(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> str:
    digest = hashlib.sha256()
    for row in conn.execute(query, params):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def safe_query_hash(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> str:
    try:
        return query_hash(conn, query, params)
    except sqlite3.Error:
        return "unavailable"


def schema_hash(conn: sqlite3.Connection) -> str:
    return query_hash(
        conn,
        "select type, name, coalesce(sql, '') from sqlite_master where type in ('table','index') order by type, name",
    )


def build_evidence_manifest(
    *,
    store_path: Path,
    symbols: list[str],
    ordered_symbols: list[str],
    config: Any,
    full_config: Any | None = None,
    cwd: Path | None = None,
    artifact_commit: str | None = None,
) -> dict[str, Any]:
    conn = sqlite3.connect(store_path)
    try:
        commit = git_commit(cwd)
        full_config_value = full_config.to_dict() if hasattr(full_config, "to_dict") else full_config
        config_value = full_config_value if full_config_value is not None else config
        dirty = git_dirty(cwd)
        return {
            "git_commit": commit,
            "run_code_commit": commit,
            "artifact_commit": artifact_commit or commit,
            "git_dirty": dirty,
            "acceptance_status": "UNREPRODUCIBLE_DIRTY_TREE" if dirty else "REPRODUCIBLE_CLEAN_TREE",
            "source_tree_hash": source_tree_hash(cwd),
            "strategy_config_hash": config_hash(config),
            "full_config_hash": stable_hash_json(full_config_value if full_config_value is not None else config_hash(config)),
            "backtest_config_hash": stable_hash_json(_section(config_value, "backtest")),
            "risk_config_hash": stable_hash_json(_section(config_value, "risk")),
            "entry_config_hash": stable_hash_json(_section(config_value, "entry_filter")),
            "alpha_config_hash": stable_hash_json(_section(config_value, "alpha")),
            "sector_config_hash": stable_hash_json(_section(config_value, "sector")),
            "statistics_config_hash": stable_hash_json(_section(config_value, "statistics")),
            "sqlite_sha256": sha256_file(store_path),
            "schema_hash": schema_hash(conn),
            "tables": sqlite_table_stats(conn),
            "symbol_universe_hash": stable_hash_lines(sorted(symbols)),
            "ordered_symbol_list_hash": stable_hash_lines(list(ordered_symbols)),
            "daily_bars_logical_hash": safe_query_hash(
                conn,
                "select code, trade_date, open, close, high, low, volume, amount from daily_bars order by code, trade_date",
            ),
            "instrument_status_logical_hash": safe_query_hash(
                conn,
                "select trade_date, code, name, listed_date, delisted_date, is_suspended from instrument_status order by trade_date, code",
            ),
            "dynamic_universe_hash": safe_query_hash(
                conn,
                "select trade_date, code from dynamic_universe order by trade_date, code",
            ),
            "sector_map_hash": safe_query_hash(conn, "select code, sector from sector_map order by code"),
            "index_data_hash": safe_query_hash(
                conn,
                "select code, trade_date, open, close, high, low, volume, amount from index_bars order by code, trade_date",
            ),
        }
    finally:
        conn.close()


def _section(config: Any, name: str) -> Any:
    if config is None:
        return {}
    if is_dataclass(config):
        config = asdict(config)
    if isinstance(config, dict):
        return config.get(name, {})
    return getattr(config, name, {})


def write_evidence_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
