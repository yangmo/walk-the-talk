"""配置：IngestSettings + 简单 .env 读取。

设计：
- 只引入「过程级」配置（CLI 参数 + 环境变量），暂不引入 YAML
  （YAML 留到 Phase 2/3 配置项膨胀后再加）
- LLM 的 API key 等敏感信息走 .env：DEEPSEEK_API_KEY / OPENAI_API_KEY 等
  Phase 1 (ingest) 不需要 LLM key，但 dotenv.load_dotenv() 提前调用方便后续 Phase
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*args, **kwargs) -> bool:  # type: ignore[no-redef]
        return False


# ============== 常量 ==============

WORK_DIR_NAME = "_walk_the_talk"
PROGRESS_FILE = "_progress.json"
CHROMA_DIR = "chroma"
BM25_FILE = "bm25.pkl"
FINANCIALS_DB = "financials.db"
LLM_CACHE_DB = "llm_cache.db"
CLAIMS_FILE = "claims.json"
VERDICTS_FILE = "verdicts.json"
VERIFY_LOG_FILE = "verify_log.jsonl"
REPORT_FILE = "report.md"


# ============== Settings ==============


@dataclass
class IngestSettings:
    """单次 ingest 跑批的配置。

    work_dir 默认为 `<data_dir>/_walk_the_talk`，所有产物落在这里。
    """

    data_dir: Path
    ticker: str
    company: str
    embedder_name: str = "bge"          # bge | hash
    chunk_target_size: int = 800
    chunk_max_size: int = 1500
    chunk_min_size: int = 200
    resume: bool = True                  # False 等价于 v1 的 --no-resume
    work_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()
        self.work_dir = self.data_dir / WORK_DIR_NAME

    # 派生路径
    @property
    def progress_path(self) -> Path:
        return self.work_dir / PROGRESS_FILE

    @property
    def chroma_dir(self) -> Path:
        return self.work_dir / CHROMA_DIR

    @property
    def financials_db_path(self) -> Path:
        return self.work_dir / FINANCIALS_DB


@dataclass
class ExtractSettings:
    """单次 extract 跑批的配置。"""

    data_dir: Path
    ticker: str
    company: str
    chat_model: str = "deepseek-chat"
    reasoner_model: str = "deepseek-reasoner"
    max_workers: int = 5            # ThreadPoolExecutor 并发
    years: list[int] | None = None  # None = 全跑；否则只跑指定年
    section_canonicals: list[str] = field(
        default_factory=lambda: [
            "mgmt_letter", "mda", "outlook", "risk", "guidance", "board_report"
        ]
    )
    resume: bool = True
    work_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()
        self.work_dir = self.data_dir / WORK_DIR_NAME

    @property
    def progress_path(self) -> Path:
        return self.work_dir / PROGRESS_FILE

    @property
    def claims_path(self) -> Path:
        return self.work_dir / CLAIMS_FILE

    @property
    def llm_cache_path(self) -> Path:
        return self.work_dir / LLM_CACHE_DB


@dataclass
class VerifySettings:
    """单次 verify 跑批的配置。"""

    data_dir: Path
    ticker: str
    company: str
    chat_model: str = "deepseek-chat"
    reasoner_model: str = "deepseek-reasoner"
    max_iters: int = 4                            # agent 单 claim 工具调用上限（含 P4 rescue 多出来的 1 轮预算）
    claim_ids: list[str] | None = None            # 只验证指定 ID（None=全部）
    years: list[int] | None = None                # 只验证某些发出年的 claim
    current_fiscal_year: int | None = None        # None=自动检测 financials.db 最新 FY
    embedder: str | None = None                   # query_chunks 用的 embedder 名；
                                                  # None = 从 chroma collection metadata 自动检测，
                                                  # 检测失败回落 'bge'。可选 'bge' | 'hash'。
    resume: bool = True
    work_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()
        self.work_dir = self.data_dir / WORK_DIR_NAME

    @property
    def claims_path(self) -> Path:
        return self.work_dir / CLAIMS_FILE

    @property
    def verdicts_path(self) -> Path:
        return self.work_dir / VERDICTS_FILE

    @property
    def verify_log_path(self) -> Path:
        return self.work_dir / VERIFY_LOG_FILE

    @property
    def llm_cache_path(self) -> Path:
        return self.work_dir / LLM_CACHE_DB

    @property
    def financials_db_path(self) -> Path:
        return self.work_dir / FINANCIALS_DB

    @property
    def chroma_dir(self) -> Path:
        return self.work_dir / CHROMA_DIR


@dataclass
class ReportSettings:
    """单次 report 跑批的配置。

    从 verdicts.json + claims.json 生成 markdown 报告；
    不读 LLM、不读 chroma，纯本地数据合成。
    """

    data_dir: Path
    ticker: str
    company: str
    output_filename: str = REPORT_FILE
    current_fy: int | None = None       # None = 自动从 verdicts 里推算（max fiscal_year）
    include_highlights: bool = True     # --no-highlights 关闭"突出事件"区
    include_method_note: bool = True    # 末尾"验证方法说明"区
    work_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()
        self.work_dir = self.data_dir / WORK_DIR_NAME

    @property
    def claims_path(self) -> Path:
        return self.work_dir / CLAIMS_FILE

    @property
    def verdicts_path(self) -> Path:
        return self.work_dir / VERDICTS_FILE

    @property
    def report_path(self) -> Path:
        return self.work_dir / self.output_filename

    @property
    def financials_db_path(self) -> Path:
        return self.work_dir / FINANCIALS_DB


# ============== .env 加载 ==============


def load_env(dotenv_path: str | Path | None = None) -> None:
    """加载 .env（优先 dotenv_path，否则当前目录 .env）。

    Phase 1 实际不读任何 LLM key，但提前加载方便后续 Phase。
    """
    if dotenv_path:
        load_dotenv(str(dotenv_path), override=False)
    else:
        load_dotenv(override=False)


def get_env(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default)
