"""核心数据模型：ParsedReport / Section / Table / Chunk / FinancialLine / Claim / Verdict。

设计原则：所有跨 phase 流转的数据都用 Pydantic 模型，便于 JSON 序列化与校验。
LangGraph 内部 state 也用 TypedDict（在各 graph.py 里定义），不强求 Pydantic。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .enums import ClaimStatus, ClaimType, ReportType, SectionCanonical, StatementType, Verdict

# ============== Phase 1 输出 ==============


class Table(BaseModel):
    """从 HTML <table> 抽出的表格，保留 markdown 形态和原始 2D 数组。

    raw_2d 是 list[list[str]]：第 0 行通常是表头（也可能不是，table_extractor 自己判断）。
    """

    index: int                          # 在该年报内的表序号（0-based）
    markdown: str                       # `| col1 | col2 |\n|---|---|\n| a | b |` 形式
    raw_2d: list[list[str]]             # 原始二维数组
    bbox_anchor: str | None = None      # 占位符，如 "TABLE_PLACEHOLDER_3"
    caption: str = ""                   # 表格之前的紧邻文本（含 "单位：千元" / 表名等）


class Section(BaseModel):
    """文本流中的一节。"""

    seq: int                            # 该年报内章节序号（0-based）
    title: str                          # 原章节名，如 "第二节致股东的信"
    canonical: SectionCanonical = SectionCanonical.OTHER
    text: str                           # 节内全文（含表格占位符）
    table_refs: list[str] = Field(default_factory=list)  # 该节内引用的 TABLE_PLACEHOLDER_N


class ParsedReport(BaseModel):
    """html_loader 的统一输出。"""

    ticker: str
    fiscal_year: int
    report_type: ReportType = ReportType.ANNUAL
    source_path: str                    # 原 HTML 文件绝对路径
    encoding: str                       # 检测到的编码（通常 GBK）
    sections: list[Section]
    tables: list[Table]


class Chunk(BaseModel):
    """写入 Chroma 的最小单位。"""

    chunk_id: str
    ticker: str
    fiscal_period: str                  # FY2024
    report_type: ReportType = ReportType.ANNUAL
    section: str                        # 原章节名
    section_canonical: SectionCanonical
    source_path: str
    locator: str                        # "第二节致股东的信#3"
    text: str
    contains_table_refs: list[str] = Field(default_factory=list)
    # embedding 不入 Pydantic 模型，由 reports_store 直接写 Chroma
    is_forward_looking: bool | None = None  # Phase 2 classifier 填


class FinancialLine(BaseModel):
    """SQLite financial_lines 表的一行。"""

    ticker: str
    fiscal_period: str                  # FY2024
    statement_type: StatementType
    line_item: str                      # 原文，如 "营业收入"
    line_item_canonical: str            # 归一化，如 "revenue"
    value: float                        # 已归一为元
    unit: str = "元"
    is_consolidated: bool = True
    source_path: str = ""
    source_locator: str = ""            # 如 "table_12#row_5"


# ============== Phase 2 输出 ==============


class Subject(BaseModel):
    scope: str = "整体"                  # 整体 / 业务板块 / 子公司 / ...
    name: str = ""


class Predicate(BaseModel):
    operator: str                        # >= <= = ≈ 趋势 完成 启动 暂缓
    value: Any = None
    unit: str | None = None


class Horizon(BaseModel):
    type: str                            # 明确日期 / 财年 / 滚动期 / 长期
    start: str                           # FY2024 等
    end: str


class VerificationPlan(BaseModel):
    """Phase 2 抽 claim 时给的粗 plan，Phase 3 verifier 直接照着执行。"""

    required_line_items: list[str] = Field(default_factory=list)   # canonical 名字
    computation: str | None = None                                  # 表达式描述
    comparison: str | None = None                                   # 与目标值比较的方式


class Claim(BaseModel):
    claim_id: str
    claim_type: ClaimType
    section: str
    section_canonical: SectionCanonical
    speaker: str = "管理层"
    original_text: str
    locator: str
    subject: Subject = Field(default_factory=Subject)
    metric: str = ""
    metric_canonical: str = ""
    predicate: Predicate
    horizon: Horizon
    conditions: str = ""
    hedging_words: list[str] = Field(default_factory=list)
    specificity_score: int = 1          # 1-5
    verifiability_score: int = 1
    materiality_score: int = 1
    extraction_confidence: float = 0.0
    from_fiscal_year: int
    canonical_key: str
    verification_plan: VerificationPlan = Field(default_factory=VerificationPlan)
    status: ClaimStatus = ClaimStatus.OPEN
    verifications: list["VerificationRecord"] = Field(default_factory=list)


# ============== Phase 3 输出 ==============


class Evidence(BaseModel):
    quote: str
    locator: str = ""
    source_path: str = ""


class ToolCall(BaseModel):
    """Verifier agent 的一次工具调用，computation_trace 里串成列表。"""

    tool_name: str
    args: dict[str, Any]
    result: Any = None
    error: str | None = None


class VerificationRecord(BaseModel):
    fiscal_year: int                     # 在哪一年的报告里被验证
    verdict: Verdict
    target_value: Any = None
    actual_value: Any = None
    evidence: list[Evidence] = Field(default_factory=list)
    computation_trace: list[ToolCall] = Field(default_factory=list)
    confidence: float = 0.0
    comment: str = ""
    cost: dict[str, Any] = Field(default_factory=dict)


# ============== 顶层容器 ==============


class ClaimStore(BaseModel):
    """落盘到 claims.json 的顶层结构。"""

    company_name: str
    ticker: str
    years_processed: list[int]
    claims: dict[str, Claim] = Field(default_factory=dict)


class VerdictStore(BaseModel):
    """落盘到 verdicts.json 的顶层结构。

    一个 claim 可能被多次验证（FY2022 的预测可在 2023/2024/2025 年报里反复审视），
    因此 verifications 是 list 而非单个 record。MVP 阶段每个 claim 只有一条 record，
    但 schema 留扩展空间，后续 incremental verify 直接 append。
    """

    company_name: str
    ticker: str
    claims_processed: list[str] = Field(default_factory=list)         # 已验证过的 claim_id（去重）
    verifications: dict[str, list[VerificationRecord]] = Field(default_factory=dict)


# Forward refs
Claim.model_rebuild()
