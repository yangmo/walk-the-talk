"""Phase 1.6 pipeline + CLI tests。

主要覆盖：
- discover_years：扫年报文件，按年升序
- ProgressTracker：读写、phase 标记、all_done / reset
- run_pipeline 端到端：SMIC 2025 单年 + HashEmbedder（CI 友好）
- resume 行为：二次跑全 skip
- CLI：typer CliRunner 跑 --help / --version / ingest 端到端
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from walk_the_talk.cli import app
from walk_the_talk.config import IngestSettings
from walk_the_talk.ingest import HashEmbedder, FinancialsStore, ReportsStore
from walk_the_talk.ingest.pipeline import (
    PERSISTED_PHASES,
    ProgressTracker,
    discover_years,
    run_pipeline,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "中芯国际"
FIXTURE_HTML = FIXTURE_DIR / "2025.html"


# ============== discover_years ==============


def test_discover_years_sorted(tmp_path: Path):
    # 故意乱序写入
    (tmp_path / "2024.html").write_text("x")
    (tmp_path / "2022.html").write_text("x")
    (tmp_path / "2023.html").write_text("x")
    # 不该被识别的
    (tmp_path / "1999.html").write_text("x")          # 1990< 但小于 2000，正则不匹配
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "draft_2025.html").write_text("x")    # 前缀不匹配
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "2099.html").write_text("x")

    years = discover_years(tmp_path)
    assert [y for y, _ in years] == [2022, 2023, 2024]
    assert all(p.suffix == ".html" for _, p in years)


def test_discover_years_empty(tmp_path: Path):
    assert discover_years(tmp_path) == []


# ============== ProgressTracker ==============


def test_progress_tracker_roundtrip(tmp_path: Path):
    p = tmp_path / "_progress.json"
    pt = ProgressTracker(p, ticker="688981", company="中芯国际")
    assert not pt.is_done(2025, "index")
    assert not pt.all_done(2025)

    pt.mark_done(2025, "index")
    pt.mark_done(2025, "extract")
    assert pt.is_done(2025, "index")
    assert pt.all_done(2025)

    # 文件已落盘
    assert p.exists()
    data = json.loads(p.read_text("utf-8"))
    assert data["ticker"] == "688981"
    assert data["company"] == "中芯国际"
    assert data["years"]["2025"] == {"index": "done", "extract": "done"}
    assert data["updated_at"]

    # 重新加载，状态应保留
    pt2 = ProgressTracker(p, ticker="688981", company="中芯国际")
    assert pt2.all_done(2025)


def test_progress_tracker_reset(tmp_path: Path):
    p = tmp_path / "_progress.json"
    pt = ProgressTracker(p, ticker="X", company="X-Co")
    pt.mark_done(2024, "index")
    pt.mark_done(2024, "extract")
    assert pt.all_done(2024)

    pt.reset()
    assert not pt.is_done(2024, "index")
    assert not pt.all_done(2024)


def test_progress_tracker_corrupted_file_rebuilds(tmp_path: Path):
    p = tmp_path / "_progress.json"
    p.write_text("not json {{{", encoding="utf-8")
    pt = ProgressTracker(p, ticker="T", company="C")
    # 不抛错，且空
    assert not pt.is_done(2025, "index")


def test_progress_tracker_company_drift_overwrites(tmp_path: Path):
    """同目录复用：不同 ticker/company 时 metadata 被覆盖。"""
    p = tmp_path / "_progress.json"
    ProgressTracker(p, ticker="A", company="Co-A").save()
    pt = ProgressTracker(p, ticker="B", company="Co-B")
    assert pt.data.ticker == "B"
    assert pt.data.company == "Co-B"


def test_persisted_phases_order():
    # index 必须在 extract 之前，下游 chunker 依赖前置抽取
    assert PERSISTED_PHASES == ("index", "extract")


# ============== run_pipeline 端到端（SMIC 2025） ==============


@pytest.fixture
def smic_data_dir(tmp_path: Path) -> Path:
    """复制 SMIC 2025 fixture 到 tmp，避免污染原 fixture 目录。"""
    if not FIXTURE_HTML.exists():
        pytest.skip("SMIC fixture missing")
    dst = tmp_path / "smic"
    dst.mkdir()
    shutil.copy(FIXTURE_HTML, dst / "2025.html")
    return dst


def _make_settings(data_dir: Path) -> IngestSettings:
    return IngestSettings(
        data_dir=data_dir,
        ticker="688981",
        company="中芯国际",
        embedder_name="hash",   # 端到端用 hash，免下载模型
    )


def test_run_pipeline_smic_2025_e2e(smic_data_dir: Path):
    settings = _make_settings(smic_data_dir)
    embedder = HashEmbedder(dim=128)
    logs: list[str] = []

    result = run_pipeline(settings, embedder=embedder, on_log=logs.append)

    # 跑通基本面
    assert result.years_processed == [2025]
    assert result.years_skipped == []
    assert result.chunks_total > 50, f"chunks 太少：{result.chunks_total}"
    assert result.financial_lines_total > 30, f"financial lines 太少：{result.financial_lines_total}"

    # 工作目录与产物
    work = settings.work_dir
    assert work.exists() and work.is_dir()
    assert settings.progress_path.exists()
    assert settings.chroma_dir.exists()
    assert settings.financials_db_path.exists()
    # bm25 pickle 由 ReportsStore 自动落（命名见 store）
    assert any(work.iterdir())

    # progress 文件结构
    pdata = json.loads(settings.progress_path.read_text("utf-8"))
    assert pdata["ticker"] == "688981"
    assert pdata["company"] == "中芯国际"
    assert pdata["years"]["2025"]["index"] == "done"
    assert pdata["years"]["2025"]["extract"] == "done"

    # ReportsStore 能打开并查到 chunks
    store = ReportsStore(work, ticker="688981", embedder=embedder)
    assert store.count() == result.chunks_total

    # FinancialsStore 能打开并查到行
    # 注：result.financial_lines_total 是 extract_from_report 返回的原始行数；
    # DB count 是 PRIMARY KEY upsert 后的去重数（相同 statement+canonical+is_consolidated 会合并），
    # 所以 DB 数 ≤ 原始数，但应当差距不大。
    with FinancialsStore(settings.financials_db_path) as fin:
        db_count = fin.count(ticker="688981")
        assert db_count > 30, f"DB 行太少：{db_count}"
        assert db_count <= result.financial_lines_total
        periods = fin.list_periods("688981")
        assert "FY2025" in periods


def test_run_pipeline_resume_skips(smic_data_dir: Path):
    """二次 run_pipeline 应当全部 skip。"""
    settings = _make_settings(smic_data_dir)
    embedder = HashEmbedder(dim=128)

    r1 = run_pipeline(settings, embedder=embedder)
    assert r1.years_processed == [2025]

    r2 = run_pipeline(settings, embedder=embedder)
    assert r2.years_processed == []
    assert r2.years_skipped == [2025]
    # 二次没有新增 chunks / lines
    assert r2.chunks_total == 0
    assert r2.financial_lines_total == 0


def test_run_pipeline_no_resume_reruns(smic_data_dir: Path):
    settings = _make_settings(smic_data_dir)
    embedder = HashEmbedder(dim=128)

    run_pipeline(settings, embedder=embedder)

    # 关闭 resume → 清空进度，全量重跑（chunks 会再 add 一份，count 翻倍）
    settings_no_resume = IngestSettings(
        data_dir=smic_data_dir,
        ticker="688981",
        company="中芯国际",
        embedder_name="hash",
        resume=False,
    )
    r2 = run_pipeline(settings_no_resume, embedder=embedder)
    assert r2.years_processed == [2025]
    assert r2.chunks_total > 0


def test_run_pipeline_no_html_raises(tmp_path: Path):
    settings = IngestSettings(
        data_dir=tmp_path,
        ticker="X",
        company="X",
        embedder_name="hash",
    )
    with pytest.raises(FileNotFoundError):
        run_pipeline(settings, embedder=HashEmbedder(dim=32))


# ============== CLI ==============


runner = CliRunner()


def test_cli_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "walk-the-talk" in result.stdout


def test_cli_ingest_help():
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    assert "ticker" in result.stdout.lower()
    assert "company" in result.stdout.lower()
    assert "embedder" in result.stdout.lower()


def test_cli_ingest_missing_required(tmp_path: Path):
    # 缺 --ticker / --company
    result = runner.invoke(app, ["ingest", str(tmp_path)])
    assert result.exit_code != 0


def test_cli_ingest_missing_dir():
    result = runner.invoke(
        app, ["ingest", "/nonexistent/path/zzz", "-t", "X", "-c", "X"]
    )
    assert result.exit_code != 0


def test_cli_ingest_e2e_hash(smic_data_dir: Path):
    """CLI 全流程：用 hash embedder 跑 SMIC 2025。"""
    result = runner.invoke(
        app,
        [
            "ingest",
            str(smic_data_dir),
            "--ticker", "688981",
            "--company", "中芯国际",
            "--embedder", "hash",
        ],
    )
    assert result.exit_code == 0, f"stdout:\n{result.stdout}\nexc:\n{result.exception}"
    assert "Done" in result.stdout or "已处理" in result.stdout

    work = smic_data_dir / "_walk_the_talk"
    assert work.exists()
    assert (work / "_progress.json").exists()
    assert (work / "financials.db").exists()
    assert (work / "chroma").exists()
