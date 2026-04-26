"""枚举定义：claim_type / verdict / section_canonical / statement_type 等。

注：`class Foo(str, Enum)` 是 Python 3.10 兼容写法（StrEnum 是 3.11+ 才有），
行为上与 StrEnum 一致：值是 str，可直接序列化、可与 str 比较。
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """3.10 兼容的 StrEnum 替身。"""

    def __str__(self) -> str:  # 与原生 StrEnum 行为一致
        return self.value


class ClaimType(StrEnum):
    """前瞻断言的五类，无 historical_disclosure 旁路。"""

    QUANTITATIVE_FORECAST = "quantitative_forecast"
    STRATEGIC_COMMITMENT = "strategic_commitment"
    CAPITAL_ALLOCATION = "capital_allocation"
    RISK_ASSESSMENT = "risk_assessment"
    QUALITATIVE_JUDGMENT = "qualitative_judgment"


class Verdict(StrEnum):
    """Phase 3 的判定结果。

    PREMATURE vs EXPIRED 是两个不同边界状态：
      PREMATURE: claim.horizon.end > current_fiscal_year，预测窗口尚未到达。
      EXPIRED:   claim.horizon.end ≤ current_fiscal_year，但所需数据缺失（中间年没 ingest 等）。
    """

    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    FAILED = "failed"
    NOT_VERIFIABLE = "not_verifiable"
    PREMATURE = "premature"
    EXPIRED = "expired"


class ClaimStatus(StrEnum):
    OPEN = "open"
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    FAILED = "failed"
    NOT_VERIFIABLE = "not_verifiable"
    PREMATURE = "premature"
    EXPIRED = "expired"


class SectionCanonical(StrEnum):
    """章节归一化分类。手维护映射表把原文章节名映射到这里。"""

    MGMT_LETTER = "mgmt_letter"           # 致股东的信 / 董事长致辞
    MDA = "mda"                            # 管理层讨论与分析
    OUTLOOK = "outlook"                    # 公司未来发展展望（mda 子项可独立）
    RISK = "risk"                          # 风险因素
    GUIDANCE = "guidance"                  # 业绩指引（少见单列）
    BOARD_REPORT = "board_report"          # 董事会报告
    GOVERNANCE = "governance"              # 公司治理
    ESG = "esg"                            # 环境与社会
    NOTES = "notes"                        # 财务报告附注
    SHARES = "shares"                      # 股份变动
    LEGAL_TEMPLATE = "legal_template"      # 重要事项 / 释义 / 备查文件等模板章节
    OTHER = "other"


class StatementType(StrEnum):
    """财务表类型。"""

    INCOME = "income"        # 利润表
    BALANCE = "balance"      # 资产负债表
    CASHFLOW = "cashflow"    # 现金流量表
    SEGMENT = "segment"      # 分部信息
    RD = "rd"                # 研发投入分项
    CAPEX = "capex"          # 资本开支
    OTHER = "other"


class ReportType(StrEnum):
    ANNUAL = "annual"
    SEMI = "semi"      # v2
    Q1 = "q1"
    Q2 = "q2"
    Q3 = "q3"
