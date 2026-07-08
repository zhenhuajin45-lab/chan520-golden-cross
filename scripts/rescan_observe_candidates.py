from __future__ import annotations

import csv
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chan520_skill.data import auto_history, normalize_code, trim_to_date
from chan520_skill.indicators import fmt
from chan520_skill.models import StockMeta
from chan520_skill.scanner import ScanRow, UniverseStock, _scan_one
from chan520_skill.strategy import analyze


TARGET = date(2026, 7, 7)
SRC = Path("reports/market_candidate_scan_2026-07-07.csv")
OUT_CSV = Path("reports/observe_rescan_practical_2026-07-07.csv")
OUT_MD = Path("reports/observe_rescan_practical_2026-07-07.md")


def main() -> int:
    source_rows = _load_observe_rows(SRC)
    print(f"old_observe={len(source_rows)}", flush=True)

    rescanned: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(_rescan_one, row): row for row in source_rows}
        for idx, future in enumerate(as_completed(futures), 1):
            old = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                failures.append({"code": old["code"], "name": old["name"], "error": str(exc)})
                continue
            if result is None:
                failures.append({"code": old["code"], "name": old["name"], "error": "no result"})
            else:
                rescanned.append(result)
            if idx % 50 == 0:
                print(f"progress={idx}/{len(source_rows)} rows={len(rescanned)} failures={len(failures)}", flush=True)

    rescanned.sort(key=_sort_key)
    _write_csv(OUT_CSV, rescanned)
    _write_markdown(OUT_MD, source_rows, rescanned, failures)
    print(f"CSV: {OUT_CSV.resolve()}")
    print(f"MD: {OUT_MD.resolve()}")
    return 0


def _load_observe_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [row for row in rows if row.get("verdict", "").startswith("观察")]


def _rescan_one(row: dict[str, str]) -> dict[str, str] | None:
    code = str(row["code"]).zfill(6)
    stock = UniverseStock(code=code, name=row["name"], market=1 if code.startswith("6") else 0)
    result = _scan_one(stock, TARGET)
    if result is None:
        result = _fallback_scan_one(stock)
    data = {key: str(value) for key, value in asdict(result).items()}
    data["old_verdict"] = row.get("verdict", "")
    data["old_level"] = row.get("level", "")
    data["old_score"] = row.get("score", "")
    data["old_main_signal"] = row.get("main_signal", "")
    return data


def _fallback_scan_one(stock: UniverseStock) -> ScanRow:
    meta, rows = auto_history(normalize_code(stock.code), TARGET)
    rows = trim_to_date(rows, TARGET)
    report = analyze(StockMeta(code=stock.code, name=meta.name or stock.name, market=stock.market), rows, TARGET)
    score = sum(
        item.score
        for item in report.large_cycle + report.buy_points + report.trend_rules + report.position_rules + report.exit_rules
    )
    return ScanRow(
        code=stock.code,
        name=stock.name,
        close=report.target.close,
        pct_chg=report.target.pct_chg,
        verdict=report.verdict,
        level=report.level,
        score=score,
        main_signal=_main_signal(report),
        satisfied_count=len(report.satisfied),
        defect_count=len(report.defects),
        satisfied="；".join(report.satisfied),
        defects="；".join(report.defects),
        ma5=fmt(report.indicator.ma5),
        ma10=fmt(report.indicator.ma10),
        ma20=fmt(report.indicator.ma20),
        ma60=fmt(report.indicator.ma60),
        dif=fmt(report.indicator.macd_dif, 3),
        dea=fmt(report.indicator.macd_dea, 3),
        macd=fmt(report.indicator.macd_hist, 3),
        rsi14=fmt(report.indicator.rsi14),
        volume_ratio=fmt(report.indicator.volume_ratio),
        ma5_slope=fmt(report.indicator.slope5_deg),
    )


def _main_signal(report) -> str:
    active = [
        item.name
        for item in report.buy_points + report.trend_rules + report.position_rules
        if item.status in {"PASS", "WARN"} and item.score > 0
    ]
    return " + ".join(active[:4]) if active else "无"


def _sort_key(row: dict[str, str]) -> tuple[int, int, int, float, str]:
    order = {
        "入选": 0,
        "观察（轻仓试探）": 1,
        "观察（等待回踩）": 2,
        "观察（趋势持有）": 3,
        "观察": 4,
        "回避/减仓观察": 5,
        "不入选": 6,
    }
    verdict = row.get("verdict", "")
    return (
        order.get(verdict, 9),
        -_as_int(row.get("score", "0")),
        -_as_int(row.get("satisfied_count", "0")),
        -_as_float(row.get("pct_chg", "0")),
        row.get("code", ""),
    )


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(exist_ok=True)
    base_fields = list(ScanRow.__dataclass_fields__.keys())
    fields = base_fields + ["old_verdict", "old_level", "old_score", "old_main_signal"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _write_markdown(
    path: Path,
    old_rows: list[dict[str, str]],
    rows: list[dict[str, str]],
    failures: list[dict[str, str]],
) -> None:
    counts = Counter(row["verdict"] for row in rows)
    old_counts = Counter(row["verdict"] for row in old_rows)
    lines: list[str] = [
        f"# {TARGET.isoformat()} 旧观察池实战版复扫",
        "",
        "- 来源：`reports/market_candidate_scan_2026-07-07.csv` 中旧版 `观察*` 股票。",
        f"- 旧观察池：{len(old_rows)} 只；新版成功复扫：{len(rows)} 只；失败：{len(failures)} 只。",
        f"- 旧版分布：{_counts_text(old_counts)}。",
        f"- 新版分布：{_counts_text(counts)}。",
        "",
        "## 一、结论分层",
        "",
        "| 新结论 | 数量 | 操作含义 |",
        "| --- | ---: | --- |",
    ]
    for verdict, meaning in [
        ("入选", "标准520，允许小仓试错，确认后再加仓"),
        ("观察（轻仓试探）", "弱化520，只能轻仓或继续等确认"),
        ("观察（等待回踩）", "趋势延续但追高，空仓等回踩，持仓看5日线"),
        ("观察（趋势持有）", "更偏持仓管理，不是新开仓强买点"),
        ("观察", "有局部积极信号，但共振不足"),
        ("回避/减仓观察", "退出/止盈风险优先，新仓回避，持仓降风险"),
        ("不入选", "新版实战规则过滤后不再跟踪"),
    ]:
        lines.append(f"| {verdict} | {counts.get(verdict, 0)} | {meaning} |")
    lines.append("")

    _add_section(lines, "二、标准入选", rows, lambda r: r["verdict"] == "入选", 80)
    _add_section(lines, "三、弱化520轻仓试探", rows, lambda r: r["verdict"] == "观察（轻仓试探）", 120)
    _add_section(lines, "四、等待回踩候选", rows, lambda r: r["verdict"] == "观察（等待回踩）", 120)
    _add_section(lines, "五、趋势持有候选", rows, lambda r: r["verdict"] == "观察（趋势持有）", 120)
    _add_section(lines, "六、普通观察", rows, lambda r: r["verdict"] == "观察", 120)
    _add_section(lines, "七、回避或不入选", rows, lambda r: r["verdict"] in {"回避/减仓观察", "不入选"}, 120)

    if failures:
        lines.extend(["## 八、失败", ""])
        lines.append("| 代码 | 名称 | 错误 |")
        lines.append("| --- | --- | --- |")
        for item in failures:
            lines.append(f"| {item['code']} | {item['name']} | {_short(item['error'], 100)} |")
        lines.append("")

    lines.append("完整字段见同目录 CSV。")
    path.write_text("\n".join(lines), encoding="utf-8")


def _add_section(lines: list[str], title: str, rows: list[dict[str, str]], predicate, limit: int) -> None:
    data = [row for row in rows if predicate(row)]
    lines.extend([f"## {title}", ""])
    if not data:
        lines.extend(["无。", ""])
        return
    lines.append("| 代码 | 名称 | 收盘 | 涨跌幅 | 新结论 | 分数 | 旧结论 | 主信号 | 主要缺陷 |")
    lines.append("| --- | --- | ---: | ---: | --- | ---: | --- | --- | --- |")
    for row in data[:limit]:
        lines.append(
            f"| {row['code']} | {row['name']} | {_fmt_float(row['close'])} | {_fmt_float(row['pct_chg'])}% | "
            f"{row['verdict']} | {row['score']} | {row['old_verdict']}({row['old_score']}) | "
            f"{_short(row['main_signal'], 70)} | {_short(row['defects'], 100)} |"
        )
    if len(data) > limit:
        lines.append(f"| ... | ... | ... | ... | ... | ... | ... | 仅展示前 {limit} 只 | 完整见 CSV |")
    lines.append("")


def _counts_text(counter: Counter[str]) -> str:
    if not counter:
        return "无"
    return "，".join(f"{key} {value}" for key, value in counter.most_common())


def _short(value: str, limit: int) -> str:
    value = str(value).replace("|", "/").replace("\n", " ")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _fmt_float(value: str) -> str:
    return f"{_as_float(value):.2f}"


def _as_float(value: str) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _as_int(value: str) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
