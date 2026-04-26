"""Ingest phase 编排：把一个公司目录下的 <year>.html 串起来跑通。

线性流程（不上 LangGraph，因为没有真状态机分支）：

    discover  →  for year in years:
                     parse(html)         # 内存中 ParsedReport
                     ├─ index   → ReportsStore.add_chunks
                     └─ extract → FinancialsStore.upsert_lines
                     mark progress
                 done → 写元数据

进度落 `<data_dir>/_walk_the_talk/_progress.json`，per-year × per-phase 粒度。
持久化的两个 phase：
    - index   : chroma + bm25
    - extract : sqlite
parse 不算 phase（无落盘），但任何一个 phase pending 都会触发 reparse。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import IngestSettings
from .chunker import chunk_report
from .embedding import Embedder, make_embedder
from .financials_store import FinancialsStore
from .html_loader import load_html
from .reports_store import ReportsStore
from .table_extractor import extract_from_report

log = logging.getLogger(__name__)

# 持久化 phase（顺序敏感）
PERSISTED_PHASES: tuple[str, ...] = ("index", "extract")


# ============== 进度跟踪 ==============


@dataclass
class _ProgressData:
    ticker: str
    company: str
    years: dict[str, dict[str, str]] = field(default_factory=dict)  # "2025" → {"index": "done", ...}
    updated_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company": self.company,
            "years": self.years,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "_ProgressData":
        return cls(
            ticker=d.get("ticker", ""),
            company=d.get("company", ""),
            years=d.get("years", {}),
            updated_at=d.get("updated_at", ""),
        )


class ProgressTracker:
    """读写 `_progress.json` 的小工具。"""

    def __init__(self, path: Path, ticker: str, company: str):
        self.path = path
        if path.exists():
            try:
                self.data = _ProgressData.from_json(json.loads(path.read_text("utf-8")))
            except Exception:
                log.warning("progress file 损坏，重建：%s", path)
                self.data = _ProgressData(ticker=ticker, company=company)
        else:
            self.data = _ProgressData(ticker=ticker, company=company)
        # ticker / company 漂移则覆盖（首次跑 / 不同公司复用同目录）
        self.data.ticker = ticker
        self.data.company = company

    def is_done(self, year: int, phase: str) -> bool:
        return self.data.years.get(str(year), {}).get(phase) == "done"

    def all_done(self, year: int) -> bool:
        return all(self.is_done(year, p) for p in PERSISTED_PHASES)

    def mark_done(self, year: int, phase: str) -> None:
        y = self.data.years.setdefault(str(year), {})
        y[phase] = "done"
        self.save()

    def reset(self) -> None:
        self.data.years = {}
        self.save()

    def save(self) -> None:
        self.data.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data.to_json(), ensure_ascii=False, indent=2),
            "utf-8",
        )


# ============== Discovery ==============


_YEAR_FILE_RE = re.compile(r"^(20\d{2})\.html$", re.IGNORECASE)


def discover_years(data_dir: Path) -> list[tuple[int, Path]]:
    """找 `<data_dir>/<year>.html`，按年份升序。返回 [(year, path), ...]。"""
    out: list[tuple[int, Path]] = []
    for p in sorted(data_dir.iterdir()):
        if not p.is_file():
            continue
        m = _YEAR_FILE_RE.match(p.name)
        if not m:
            continue
        out.append((int(m.group(1)), p))
    return sorted(out, key=lambda x: x[0])


# ============== Pipeline ==============


@dataclass
class IngestResult:
    years_processed: list[int]
    years_skipped: list[int]
    chunks_total: int
    financial_lines_total: int


def run_pipeline(
    settings: IngestSettings,
    *,
    embedder: Embedder | None = None,
    on_log: Any = None,  # callable(str) -> None；None 时落到 logging
) -> IngestResult:
    """ingest 主入口。线程不安全（FinancialsStore 单连接）。"""

    settings.work_dir.mkdir(parents=True, exist_ok=True)

    def _emit(msg: str) -> None:
        if on_log:
            on_log(msg)
        else:
            log.info(msg)

    # 进度
    progress = ProgressTracker(settings.progress_path, settings.ticker, settings.company)
    if not settings.resume:
        _emit("--no-resume：清空进度，全量重跑。")
        progress.reset()

    # 发现 HTML 文件
    files = discover_years(settings.data_dir)
    if not files:
        raise FileNotFoundError(f"{settings.data_dir} 下没有 <year>.html 文件")
    years = [y for y, _ in files]
    _emit(f"发现 {len(files)} 份年报：{years}")

    # Embedder（懒构造；BGE 首次用会下载模型）
    embedder = embedder or make_embedder(settings.embedder_name)

    # Stores
    reports_store = ReportsStore(
        persist_dir=settings.work_dir,
        ticker=settings.ticker,
        embedder=embedder,
    )
    fin_store = FinancialsStore(settings.financials_db_path)

    processed: list[int] = []
    skipped: list[int] = []
    chunks_total = 0
    lines_total = 0

    try:
        for year, html_path in files:
            if progress.all_done(year):
                _emit(f"  [skip] {year}（所有 phase 已完成）")
                skipped.append(year)
                continue

            _emit(f"  [parse] {year}: {html_path.name}")
            report = load_html(html_path)

            # ticker 校对（年报里写的与 CLI 给的不一致时给 warning，不阻断）
            if report.ticker != "UNKNOWN" and report.ticker != settings.ticker:
                _emit(f"    ⚠ HTML 内 ticker={report.ticker}，CLI ticker={settings.ticker}（按 CLI 走）")

            if not progress.is_done(year, "index"):
                chunks = chunk_report(
                    report,
                    target_size=settings.chunk_target_size,
                    max_size=settings.chunk_max_size,
                    min_size=settings.chunk_min_size,
                )
                # 用 CLI 提供的 ticker 覆盖 chunk.ticker（保证 store 内一致）
                for c in chunks:
                    c.ticker = settings.ticker
                reports_store.add_chunks(chunks)
                progress.mark_done(year, "index")
                chunks_total += len(chunks)
                _emit(f"  [index] {year}: +{len(chunks)} chunks")

            if not progress.is_done(year, "extract"):
                lines = extract_from_report(report)
                for ln in lines:
                    ln.ticker = settings.ticker
                fin_store.upsert_lines(lines)
                progress.mark_done(year, "extract")
                lines_total += len(lines)
                _emit(f"  [extract] {year}: +{len(lines)} financial lines")

            processed.append(year)

    finally:
        fin_store.close()

    return IngestResult(
        years_processed=processed,
        years_skipped=skipped,
        chunks_total=chunks_total,
        financial_lines_total=lines_total,
    )
