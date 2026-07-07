from __future__ import annotations

import csv
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import akshare as ak

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.scanner import UniverseStock, _scan_one


TARGET = date(2026, 7, 7)


def main() -> int:
    out_dir = Path("reports")
    out_dir.mkdir(exist_ok=True)
    universe = load_universe()
    quotes = fetch_quotes(universe)
    quote_path = out_dir / f"market_quote_prefilter_{TARGET.isoformat()}.csv"
    write_quotes(quote_path, quotes)

    candidates = [
        item
        for item in quotes
        if item["pct_chg"] >= 3.0
        or item["volume_ratio"] >= 2.5
        or item["turnover"] >= 8.0
    ]
    print(f"universe={len(universe)} quotes={len(quotes)} candidates={len(candidates)}", flush=True)

    rows = []
    failures = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {
            pool.submit(
                _scan_one,
                UniverseStock(code=item["code"], name=item["name"], market=1 if item["code"].startswith("6") else 0),
                TARGET,
            ): item
            for item in candidates
        }
        for idx, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result is None:
                failures += 1
            else:
                rows.append(result)
            if idx % 50 == 0:
                print(f"history progress: {idx}/{len(candidates)} rows={len(rows)} failures={failures}", flush=True)

    rows.sort(key=lambda row: (row.verdict != "入选", -row.score, -row.satisfied_count, -row.pct_chg, row.code))
    csv_path = out_dir / f"market_candidate_scan_{TARGET.isoformat()}.csv"
    md_path = out_dir / f"market_candidate_scan_{TARGET.isoformat()}.md"
    write_scan_csv(csv_path, rows)
    write_markdown(md_path, rows, len(universe), len(quotes), len(candidates), failures)
    print(f"CSV: {csv_path.resolve()}")
    print(f"MD: {md_path.resolve()}")
    return 0


def load_universe() -> list[UniverseStock]:
    df = ak.stock_info_a_code_name()
    out = []
    for item in df.to_dict("records"):
        code = str(item.get("code", "")).zfill(6)
        name = str(item.get("name", ""))
        if not code.startswith(("0", "3", "6")):
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        out.append(UniverseStock(code=code, name=name, market=1 if code.startswith("6") else 0))
    return out


def fetch_quotes(universe: list[UniverseStock]) -> list[dict]:
    rows = []
    symbols = [("sh" if item.market == 1 else "sz") + item.code for item in universe]
    stock_by_code = {item.code: item for item in universe}
    for start in range(0, len(symbols), 80):
        chunk = symbols[start : start + 80]
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        text = urllib.request.urlopen(url, timeout=20).read().decode("gbk", errors="replace")
        for payload in text.split(";"):
            if '="' not in payload:
                continue
            body = payload.split('="', 1)[1].strip('"')
            fields = body.split("~")
            if len(fields) < 50:
                continue
            code = fields[2]
            stock = stock_by_code.get(code)
            if not stock:
                continue
            price = as_float(fields[3])
            if price <= 0:
                continue
            rows.append(
                {
                    "code": code,
                    "name": stock.name,
                    "price": price,
                    "pct_chg": as_float(fields[32]),
                    "volume": as_float(fields[36]),
                    "turnover": as_float(fields[38]),
                    "amplitude": as_float(fields[43]),
                    "volume_ratio": as_float(fields[49]),
                }
            )
    return rows


def write_quotes(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["code", "name", "price", "pct_chg", "volume", "turnover", "amplitude", "volume_ratio"])
        writer.writeheader()
        writer.writerows(rows)


def write_scan_csv(path: Path, rows) -> None:
    fields = list(rows[0].__dataclass_fields__.keys()) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        if not fields:
            return
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def write_markdown(path: Path, rows, universe_count: int, quote_count: int, candidate_count: int, failures: int) -> None:
    selected = [row for row in rows if row.verdict == "入选"]
    core = [row for row in rows if row.verdict.startswith("观察") and ("520金叉" in row.main_signal or row.score >= 8)]
    observe = [row for row in rows if row.verdict.startswith("观察") and row not in core]
    lines = [
        f"# {TARGET.isoformat()} 沪深非ST 两阶段520候选扫描",
        "",
        f"- 全市场代码池：{universe_count}",
        f"- 成功取得腾讯收盘报价：{quote_count}",
        f"- 报价初筛候选：{candidate_count}",
        f"- 历史K线失败：{failures}",
        f"- 入选确认：{len(selected)}",
        f"- 满足核心/高分观察：{len(core)}",
        f"- 其他观察：{len(observe)}",
        "",
    ]
    lines.extend(section("一、入选确认", selected, 100))
    lines.extend(section("二、满足核心/高分观察", core, 150))
    lines.extend(section("三、其他观察 Top 100", observe, 100))
    path.write_text("\n".join(lines), encoding="utf-8")


def section(title: str, rows, limit: int) -> list[str]:
    lines = [f"## {title}", ""]
    if not rows:
        lines.extend(["无。", ""])
        return lines
    lines.extend(
        [
            "| 代码 | 名称 | 收盘 | 涨跌幅 | 结论 | 分数 | 主信号 | 满足条件 | 主要缺陷 |",
            "| --- | --- | ---: | ---: | --- | ---: | --- | --- | --- |",
        ]
    )
    for row in rows[:limit]:
        lines.append(
            f"| {row.code} | {row.name} | {row.close:.2f} | {row.pct_chg:.2f}% | {row.verdict} | {row.score} | "
            f"{short(row.main_signal)} | {short(row.satisfied)} | {short(row.defects)} |"
        )
    lines.append("")
    return lines


def short(value: str, limit: int = 90) -> str:
    value = value.replace("|", "/")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def as_float(value: str) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
