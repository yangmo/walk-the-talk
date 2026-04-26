"""walk-the-talk CLI (Typer)。

子命令：
    ingest   <data_dir>  --ticker T --company C [--embedder bge|hash] [--no-resume]
    extract  <data_dir>  --ticker T --company C [--years 2024,2025] [--debug] [--no-resume]
    inspect  <data_dir>  --ticker T --company C [--years 2024,2025]
                            （只读地按年×section 列 chunk 数，零 LLM）
    verify   <data_dir>  --ticker T --company C [--claim-ids ...] [--years ...]
                            [--current-fy 2025] [--max-iters 3] [--no-resume] [--debug]

后续 Phase 加：
    report   <data_dir>  ...

入口在 pyproject.toml 注册为 `walk-the-talk = "walk_the_talk.cli:app"`。
"""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import ExtractSettings, IngestSettings, ReportSettings, VerifySettings, load_env
from .core.enums import Verdict
from .extract.pipeline import inspect_chunks, run_extract
from .ingest.pipeline import run_pipeline
from .report.builder import run_report
from .verify.pipeline import run_verify

app = typer.Typer(
    name="walk-the-talk",
    help="回溯上市公司年报中管理层的可验证断言，用后续年份的事实回头打分。",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()


# ============== 公共 ==============


class EmbedderChoice(str, Enum):
    bge = "bge"
    hash = "hash"


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"walk-the-talk {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="打印版本号并退出。",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """walk-the-talk CLI 根入口。"""
    return None


# ============== ingest ==============


@app.command("ingest")
def ingest_cmd(
    data_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="包含 <year>.html 的目录。",
    ),
    ticker: str = typer.Option(
        ...,
        "--ticker",
        "-t",
        help="股票代码（6 位数字，如 688981）。",
    ),
    company: str = typer.Option(
        ...,
        "--company",
        "-c",
        help="公司中文名，用于报告输出（如 中芯国际）。",
    ),
    embedder: EmbedderChoice = typer.Option(
        EmbedderChoice.bge,
        "--embedder",
        help="向量模型：bge=BAAI/bge-small-zh-v1.5，hash=依赖最小的 fallback。",
    ),
    no_resume: bool = typer.Option(
        False,
        "--no-resume",
        help="清空 _progress.json 全量重跑。默认开启 resume。",
    ),
    chunk_target: int = typer.Option(
        800,
        "--chunk-target",
        help="目标 chunk 字符长度。",
    ),
    chunk_max: int = typer.Option(
        1500,
        "--chunk-max",
        help="chunk 字符长度上限。",
    ),
    chunk_min: int = typer.Option(
        200,
        "--chunk-min",
        help="chunk 字符长度下限。",
    ),
    env_file: Path | None = typer.Option(
        None,
        "--env-file",
        help="自定义 .env 路径（默认当前目录 .env）。",
    ),
) -> None:
    """从 <data_dir>/<year>.html 抽取 chunks + financials，落到 <data_dir>/_walk_the_talk/。"""

    # 加载 .env（Phase 1 暂不读 LLM key，提前加载方便后续 Phase）
    load_env(env_file)

    settings = IngestSettings(
        data_dir=data_dir,
        ticker=ticker,
        company=company,
        embedder_name=embedder.value,
        chunk_target_size=chunk_target,
        chunk_max_size=chunk_max,
        chunk_min_size=chunk_min,
        resume=not no_resume,
    )

    console.rule(f"[bold cyan]walk-the-talk ingest[/]  {company} ({ticker})")
    console.print(f"[dim]data_dir[/] {settings.data_dir}")
    console.print(f"[dim]work_dir[/] {settings.work_dir}")
    console.print(f"[dim]embedder[/] {embedder.value}    [dim]resume[/] {settings.resume}")

    try:
        result = run_pipeline(settings, on_log=console.print)
    except FileNotFoundError as e:
        console.print(f"[red]✗ {e}[/]")
        raise typer.Exit(code=2) from e
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]✗ ingest 失败：{type(e).__name__}: {e}[/]")
        raise typer.Exit(code=1) from e

    console.rule("[bold green]Done")
    console.print(
        f"已处理 [green]{len(result.years_processed)}[/] 年 "
        f"(skipped {len(result.years_skipped)})；"
        f"chunks +{result.chunks_total}；financial lines +{result.financial_lines_total}"
    )
    if result.years_processed:
        console.print(f"[dim]years_processed[/] {result.years_processed}")
    if result.years_skipped:
        console.print(f"[dim]years_skipped[/]  {result.years_skipped}")


# ============== extract ==============


@app.command("extract")
def extract_cmd(
    data_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="包含 <year>.html 与 _walk_the_talk/ 的目录（必须先跑过 ingest）。",
    ),
    ticker: str = typer.Option(..., "--ticker", "-t", help="股票代码。"),
    company: str = typer.Option(..., "--company", "-c", help="公司中文名。"),
    years: str = typer.Option(
        "",
        "--years",
        help="逗号分隔年份（如 2024,2025）。空 = 自动跑所有已 ingest 的年份。",
    ),
    chat_model: str = typer.Option(
        "deepseek-chat", "--chat-model", help="主力 LLM 模型名。"
    ),
    reasoner_model: str = typer.Option(
        "deepseek-reasoner", "--reasoner-model", help="schema 失败时的降级模型。"
    ),
    max_workers: int = typer.Option(
        5, "--max-workers", "-j", help="ThreadPoolExecutor 并发度。"
    ),
    no_resume: bool = typer.Option(
        False, "--no-resume", help="忽略已完成标记，全量重跑指定年份。"
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="额外落 claims.raw.json（postprocess 前）+ extract_log.jsonl（per-chunk）方便诊断。",
    ),
    env_file: Path | None = typer.Option(None, "--env-file", help="自定义 .env 路径。"),
) -> None:
    """从已 ingest 的 chunks 抽前瞻 claim，落到 <data_dir>/_walk_the_talk/claims.json。"""
    load_env(env_file)

    year_list: list[int] | None = None
    if years.strip():
        try:
            year_list = sorted({int(y) for y in years.split(",") if y.strip()})
        except ValueError as e:
            console.print(f"[red]✗ --years 解析失败：{e}[/]")
            raise typer.Exit(code=2) from e

    settings = ExtractSettings(
        data_dir=data_dir,
        ticker=ticker,
        company=company,
        chat_model=chat_model,
        reasoner_model=reasoner_model,
        max_workers=max_workers,
        years=year_list,
        resume=not no_resume,
    )

    console.rule(f"[bold cyan]walk-the-talk extract[/]  {company} ({ticker})")
    console.print(f"[dim]data_dir[/] {settings.data_dir}")
    console.print(f"[dim]work_dir[/] {settings.work_dir}")
    console.print(
        f"[dim]chat_model[/] {chat_model}  [dim]reasoner_model[/] {reasoner_model}  "
        f"[dim]workers[/] {max_workers}  [dim]resume[/] {settings.resume}  "
        f"[dim]debug[/] {debug}"
    )

    try:
        result = run_extract(settings, on_log=console.print, debug=debug)
    except RuntimeError as e:
        console.print(f"[red]✗ {e}[/]")
        raise typer.Exit(code=2) from e
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]✗ extract 失败：{type(e).__name__}: {e}[/]")
        raise typer.Exit(code=1) from e

    console.rule("[bold green]Extract Done")
    # 头条：总览
    console.print(
        f"已处理 [bold green]{len(result.years_processed)}[/] 年 "
        f"(skipped {len(result.years_skipped)})；"
        f"chunks [cyan]{result.chunks_total}[/] "
        f"(pre-LLM trivial -[red]{result.chunks_skipped_trivial}[/]) → "
        f"raw claims [yellow]{result.raw_claims_total}[/] → "
        f"final claims [bold green]{result.final_claims_total}[/]"
    )

    # Postprocess 漏斗
    console.print(
        f"[dim]postprocess[/]  blacklist=-{result.pp_dropped_blacklist}  "
        f"expired=-{result.pp_dropped_expired}  trivial=-{result.pp_dropped_trivial}  "
        f"dedup_within=-{result.pp_dedup_within_year}  "
        f"dedup_cross=-{result.pp_dedup_cross_year}"
    )

    # LLM 用量
    console.print(
        f"[dim]llm[/]  tokens in={result.prompt_tokens} out={result.completion_tokens} "
        f"total={result.total_tokens}  cache_hits={result.cache_hits}  "
        f"fallback={result.fallback_used}  failed_chunks={result.chunks_failed}"
    )

    # Per-section 表（chunks / raw / final）
    sections = sorted(
        set(result.chunks_by_section)
        | set(result.raw_claims_by_section)
        | set(result.final_claims_by_section)
    )
    if sections:
        table = Table(title="按 section_canonical 分布", show_lines=False)
        table.add_column("section", style="cyan", no_wrap=True)
        table.add_column("chunks", justify="right")
        table.add_column("raw claims", justify="right")
        table.add_column("final claims", justify="right", style="green")
        table.add_column("yield (final/chunk)", justify="right", style="dim")
        for s in sections:
            ch = result.chunks_by_section.get(s, 0)
            rc = result.raw_claims_by_section.get(s, 0)
            fc = result.final_claims_by_section.get(s, 0)
            yield_str = f"{fc / ch:.2f}" if ch else "-"
            table.add_row(s, str(ch), str(rc), str(fc), yield_str)
        console.print(table)

    if result.years_processed:
        console.print(f"[dim]years_processed[/] {result.years_processed}")
    if result.years_skipped:
        console.print(f"[dim]years_skipped[/]  {result.years_skipped}")
    console.print(f"[dim]claims.json[/] {settings.claims_path}")
    if debug:
        console.print(
            f"[dim]debug 落盘：{settings.work_dir / 'claims.raw.json'} + "
            f"{settings.work_dir / 'extract_log.jsonl'}[/]"
        )


# ============== inspect ==============


@app.command("inspect")
def inspect_cmd(
    data_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="包含 _walk_the_talk/ 的目录（必须先跑过 ingest）。",
    ),
    ticker: str = typer.Option(..., "--ticker", "-t", help="股票代码。"),
    company: str = typer.Option(..., "--company", "-c", help="公司中文名。"),
    years: str = typer.Option(
        "",
        "--years",
        help="逗号分隔年份（如 2023,2024）。空 = 自动列所有已 ingest 的年份。",
    ),
) -> None:
    """只读地按年×section_canonical 列出 chunk 数；零 LLM 成本，用于诊断 ingest 覆盖面。"""

    year_list: list[int] | None = None
    if years.strip():
        try:
            year_list = sorted({int(y) for y in years.split(",") if y.strip()})
        except ValueError as e:
            console.print(f"[red]✗ --years 解析失败：{e}[/]")
            raise typer.Exit(code=2) from e

    settings = ExtractSettings(
        data_dir=data_dir,
        ticker=ticker,
        company=company,
        years=year_list,
    )

    console.rule(f"[bold cyan]walk-the-talk inspect[/]  {company} ({ticker})")
    console.print(f"[dim]data_dir[/] {settings.data_dir}")
    console.print(f"[dim]work_dir[/] {settings.work_dir}")

    try:
        result = inspect_chunks(settings, on_log=console.print)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]✗ inspect 失败：{type(e).__name__}: {e}[/]")
        raise typer.Exit(code=1) from e

    if not result.years:
        console.print("[yellow]没有可统计的年份；先跑 ingest。[/]")
        return

    # 收集所有出现过的 section
    sections: list[str] = sorted(
        {s for sec_map in result.chunks_by_year_section.values() for s in sec_map}
    )
    table = Table(
        title=f"chunks by year × section ({result.total_chunks} total)",
        show_lines=False,
    )
    table.add_column("section", style="cyan", no_wrap=True)
    for y in result.years:
        table.add_column(str(y), justify="right")
    table.add_column("Σ", justify="right", style="bold green")

    # 标记候选 section（与 ExtractSettings 默认 section_canonicals 一致）
    candidate = set(settings.section_canonicals)

    # row 列表
    for s in sections:
        per_year = [result.chunks_by_year_section.get(y, {}).get(s, 0) for y in result.years]
        row_total = sum(per_year)
        marker = "★ " if s in candidate else "  "
        table.add_row(
            f"{marker}{s}",
            *[str(n) if n else "[dim]-[/]" for n in per_year],
            str(row_total),
        )
    # 列汇总
    col_totals = [
        sum(result.chunks_by_year_section.get(y, {}).values()) for y in result.years
    ]
    table.add_row(
        "[bold]TOTAL[/]",
        *[f"[bold]{n}[/]" for n in col_totals],
        f"[bold]{result.total_chunks}[/]",
    )
    console.print(table)
    console.print(
        "[dim]★ 标记的 section 是 extract 的默认候选；其余 section 不进 LLM。[/]"
    )


# ============== verify ==============


@app.command("verify")
def verify_cmd(
    data_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="包含 _walk_the_talk/claims.json 的目录（必须先跑过 extract）。",
    ),
    ticker: str = typer.Option(..., "--ticker", "-t", help="股票代码。"),
    company: str = typer.Option(..., "--company", "-c", help="公司中文名。"),
    claim_ids: str = typer.Option(
        "",
        "--claim-ids",
        help="逗号分隔的 claim_id（如 688981-FY2022-002,688981-FY2023-001）；空 = 全部。",
    ),
    years: str = typer.Option(
        "",
        "--years",
        help="逗号分隔的发出年（claim.from_fiscal_year）；空 = 全部。",
    ),
    chat_model: str = typer.Option(
        "deepseek-chat", "--chat-model", help="主力 LLM 模型。"
    ),
    reasoner_model: str = typer.Option(
        "deepseek-reasoner", "--reasoner-model", help="降级 LLM 模型。"
    ),
    max_iters: int = typer.Option(
        4, "--max-iters", help="单 claim agent 工具调用上限（默认 4，含 rescue 重试预算）。"
    ),
    current_fy: int | None = typer.Option(
        None,
        "--current-fy",
        help="覆盖自动检测的 current_fiscal_year（默认 = financials.db 最大 FY）。",
    ),
    embedder: str = typer.Option(
        "",
        "--embedder",
        help="query_chunks 用的 embedder 名（bge / hash）；空 = 从 chroma metadata 自动检测。",
    ),
    no_resume: bool = typer.Option(
        False, "--no-resume", help="忽略 verdicts.json 缓存，全量重跑指定 claims。"
    ),
    debug: bool = typer.Option(
        False, "--debug", help="详细日志（per-claim 状态机轨迹）。"
    ),
    env_file: Path | None = typer.Option(None, "--env-file", help="自定义 .env 路径。"),
) -> None:
    """对 claims.json 中的前瞻断言执行 verify，落到 verdicts.json。"""

    load_env(env_file)

    claim_id_list: list[str] | None = None
    if claim_ids.strip():
        claim_id_list = [x.strip() for x in claim_ids.split(",") if x.strip()] or None

    year_list: list[int] | None = None
    if years.strip():
        try:
            year_list = sorted({int(y) for y in years.split(",") if y.strip()})
        except ValueError as e:
            console.print(f"[red]✗ --years 解析失败：{e}[/]")
            raise typer.Exit(code=2) from e

    settings = VerifySettings(
        data_dir=data_dir,
        ticker=ticker,
        company=company,
        chat_model=chat_model,
        reasoner_model=reasoner_model,
        max_iters=max_iters,
        claim_ids=claim_id_list,
        years=year_list,
        current_fiscal_year=current_fy,
        embedder=embedder.strip() or None,
        resume=not no_resume,
    )

    console.rule(f"[bold cyan]walk-the-talk verify[/]  {company} ({ticker})")
    console.print(f"[dim]data_dir[/] {settings.data_dir}")
    console.print(f"[dim]work_dir[/] {settings.work_dir}")
    console.print(
        f"[dim]chat_model[/] {chat_model}  [dim]max_iters[/] {max_iters}  "
        f"[dim]resume[/] {settings.resume}  [dim]debug[/] {debug}"
    )

    try:
        result = run_verify(settings, on_log=console.print)
    except RuntimeError as e:
        console.print(f"[red]✗ {e}[/]")
        raise typer.Exit(code=2) from e
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]✗ verify 失败：{type(e).__name__}: {e}[/]")
        raise typer.Exit(code=1) from e

    console.rule("[bold green]Verify Done")
    console.print(
        f"claims [cyan]{result.claims_total}[/] → "
        f"processed [bold green]{result.claims_processed}[/] "
        f"(skipped {result.claims_skipped}, "
        f"failed {len(result.claims_failed)})；"
        f"current_fy [yellow]FY{result.current_fiscal_year}[/]"
    )

    if result.verdicts_by_type:
        v_table = Table(title="verdict 分布", show_lines=False)
        v_table.add_column("verdict", style="cyan", no_wrap=True)
        v_table.add_column("count", justify="right", style="bold green")
        order = [
            Verdict.VERIFIED,
            Verdict.PARTIALLY_VERIFIED,
            Verdict.FAILED,
            Verdict.NOT_VERIFIABLE,
            Verdict.PREMATURE,
            Verdict.EXPIRED,
        ]
        for v in order:
            n = result.verdicts_by_type.get(v.value, 0)
            if n:
                v_table.add_row(v.value, str(n))
        console.print(v_table)

    if result.verdicts_by_year:
        y_table = Table(title="按 from_fiscal_year 分布", show_lines=False)
        y_table.add_column("from_fy", style="cyan", justify="right")
        y_table.add_column("count", justify="right", style="bold green")
        for y in sorted(result.verdicts_by_year):
            y_table.add_row(f"FY{y}", str(result.verdicts_by_year[y]))
        console.print(y_table)

    console.print(
        f"[dim]llm[/]  tokens in={result.prompt_tokens} out={result.completion_tokens} "
        f"total={result.total_tokens}  cache_hits={result.cache_hits}  "
        f"tool_calls={result.tool_calls_total}"
    )
    console.print(
        f"[dim]elapsed[/] {result.elapsed_seconds:.1f}s  "
        f"[dim]verdicts.json[/] {settings.verdicts_path}"
    )

    if result.claims_failed:
        console.print(f"[red]failed claims[/] {result.claims_failed}")


# ============== report ==============


@app.command("report")
def report_cmd(
    data_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="包含 _walk_the_talk/verdicts.json + claims.json 的目录（必须先跑过 verify）。",
    ),
    ticker: str = typer.Option(..., "--ticker", "-t", help="股票代码。"),
    company: str = typer.Option(..., "--company", "-c", help="公司中文名。"),
    out: str = typer.Option(
        "report.md",
        "--out",
        help="输出文件名（落到 _walk_the_talk/ 目录下）。",
    ),
    current_fy: int | None = typer.Option(
        None,
        "--current-fy",
        help="覆盖自动检测的当前财年基准（默认 = verdicts 里 max(fiscal_year)）。",
    ),
    no_highlights: bool = typer.Option(
        False, "--no-highlights", help="关闭'突出事件'区（用于纯诊断场景）。"
    ),
    no_method_note: bool = typer.Option(
        False, "--no-method-note", help="关闭末尾'验证方法说明'区。"
    ),
) -> None:
    """从 verdicts.json + claims.json 合成 markdown 可信度报告。"""

    settings = ReportSettings(
        data_dir=data_dir,
        ticker=ticker,
        company=company,
        output_filename=out,
        current_fy=current_fy,
        include_highlights=not no_highlights,
        include_method_note=not no_method_note,
    )

    console.rule(f"[bold cyan]walk-the-talk report[/]  {company} ({ticker})")
    console.print(f"[dim]data_dir[/] {settings.data_dir}")
    console.print(f"[dim]work_dir[/] {settings.work_dir}")

    try:
        result = run_report(settings, on_log=console.print)
    except FileNotFoundError as e:
        console.print(f"[red]✗ {e}[/]")
        raise typer.Exit(code=2) from e
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]✗ report 失败：{type(e).__name__}: {e}[/]")
        raise typer.Exit(code=1) from e

    console.rule("[bold green]Report Done")
    console.print(
        f"claims [cyan]{result['n_claims']}[/]  "
        f"verdict 分布 V/P/F/NV/PR/EXP "
        f"[bold green]{result['n_verified']}[/]/"
        f"[yellow]{result['n_partial']}[/]/"
        f"[bold red]{result['n_failed']}[/]/"
        f"[dim]{result['n_not_verifiable']}[/]/"
        f"[dim]{result['n_premature']}[/]/"
        f"[dim]{result['n_expired']}[/]"
    )
    if result.get("overall_credibility") is not None:
        console.print(
            f"整体可信度 [bold green]{result['overall_credibility']}[/]/100  "
            f"current_fy [yellow]FY{result['current_fy']}[/]"
        )
    else:
        console.print(
            "[yellow]整体可信度: 无可对照 claim（全为 PREMATURE/NOT_VERIFIABLE）[/]"
        )
    console.print(f"[dim]report.md[/] {settings.report_path}")


# ============== entrypoint ==============


def main() -> None:
    """方便 `python -m walk_the_talk.cli` 直接调用（pyproject 已注册 console_script）。"""
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
