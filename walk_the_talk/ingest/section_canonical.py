"""把原章节名映射到 SectionCanonical。

规则：按优先级遍历关键字表，首个命中的 canonical 胜出。优先级越靠前越具体。
不命中则返回 OTHER。
"""

from __future__ import annotations

from ..core.enums import SectionCanonical

# 优先级顺序很重要：先匹配更具体的关键词。
# 例：「公司治理、环境和社会」必须先命中 GOVERNANCE，否则会被 ESG 抢走。
CANONICAL_RULES: list[tuple[SectionCanonical, tuple[str, ...]]] = [
    (SectionCanonical.MDA, ("管理层讨论与分析", "经营情况讨论与分析")),
    (SectionCanonical.MGMT_LETTER, ("致股东", "董事长致辞", "总经理致辞", "首席执行官致辞")),
    (SectionCanonical.BOARD_REPORT, ("董事会报告",)),
    (SectionCanonical.SHARES, ("股份变动", "股东情况", "股本变动")),
    (SectionCanonical.NOTES, ("财务报告", "财务报表", "审计报告")),
    (SectionCanonical.GOVERNANCE, ("公司治理",)),
    (SectionCanonical.ESG, ("环境和社会", "可持续发展", "ESG", "社会责任")),
    (SectionCanonical.RISK, ("风险因素",)),
    (SectionCanonical.OUTLOOK, ("未来发展展望", "未来展望")),
    (SectionCanonical.GUIDANCE, ("业绩指引",)),
    (SectionCanonical.LEGAL_TEMPLATE, ("释义", "重要事项", "备查文件")),
]


def classify_section(title: str) -> SectionCanonical:
    """按 CANONICAL_RULES 顺序匹配，首个命中胜出。"""
    if not title:
        return SectionCanonical.OTHER
    for canonical, keywords in CANONICAL_RULES:
        for kw in keywords:
            if kw in title:
                return canonical
    return SectionCanonical.OTHER
