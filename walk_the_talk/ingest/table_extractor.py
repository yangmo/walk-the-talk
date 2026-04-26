"""三大表识别与抽取：HTML <table> → list[FinancialLine]。

策略：
- 用第一列前若干行的关键词命中数判定 statement_type（BALANCE / INCOME / CASHFLOW）。
- 单位（元/千元/万元/百万元/亿元）：先看 caption，再扫描表内前几行。
- 行抽取：lookup_canonical 命中才写入；parse_numeric × 单位倍率得到「元」。
- 多片段同种表（HTML 里 BALANCE 有时被拆成 2-3 张子表）：按片段独立处理，
  下游 financials_store 用 (ticker, fy, statement_type, canonical, is_consolidated)
  upsert 自然去重。

不做：
- colspan / rowspan 复杂解构（年报里 99% 是规则表）。
- 母公司 vs 合并精确切换：默认 is_consolidated=True；表内/caption 出现
  「母公司」且不出现「合并」时才设 False。SMIC 2025 实际只有合并报表。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.enums import StatementType
from ..core.models import FinancialLine, ParsedReport, Table
from ._taxonomy import (
    UNIT_MULTIPLIER,
    lookup_canonical,
    parse_numeric,
    parse_unit_from_caption,
)

# ============== Statement-type 关键词 ==============

# 第一列文本里命中的关键词数 — 取最高者作为 statement_type。
# 阈值 >=2 才视为可信，否则 OTHER。
_BALANCE_KEYWORDS = {
    "流动资产", "货币资金", "应收账款", "存货", "资产总计",
    "负债合计", "所有者权益", "股东权益", "实收资本", "股本",
    "归属于母公司所有者权益", "未分配利润", "盈余公积", "短期借款",
    "应付账款", "合同负债", "固定资产", "在建工程",
}
_INCOME_KEYWORDS = {
    "营业总收入", "营业收入", "营业总成本", "营业成本",
    "净利润", "利润总额", "营业利润", "研发费用", "管理费用",
    "销售费用", "财务费用", "归属于母公司股东的净利润",
    "基本每股收益", "综合收益总额", "所得税费用",
}
_CASHFLOW_KEYWORDS = {
    # 主表关键词
    "经营活动产生的现金流量", "投资活动产生的现金流量",
    "筹资活动产生的现金流量", "销售商品、提供劳务收到的现金",
    "购建固定资产", "经营活动现金流入小计", "经营活动现金流出小计",
    "支付给职工", "现金及现金等价物", "汇率变动",
    # === 补充资料表（将净利润调节为经营活动现金流量）专属关键词 ===
    # 这些短语**仅在 cashflow 补充资料**里作为单独的行项目出现，
    # 既不出现于资产负债表也不出现于利润表。加进来让补充资料表自身
    # 拿到 c_score>=7 直接过 _is_strong_main，避免 ties 落到 BALANCE。
    # 不影响主三大表识别（主表已有 7+ 命中，supp 关键词不会减分）。
    "经营性应收项目",       # "经营性应收项目的减少"
    "经营性应付项目",       # "经营性应付项目的增加"
    "递延所得税资产减少",   # 补充资料行（BALANCE 只是"递延所得税资产"）
    "无形资产摊销",         # 补充资料行（BALANCE 只是"无形资产"）
    "长期待摊费用摊销",     # 补充资料行
    "投资损失",             # 补充资料行（INCOME 只有"投资收益"）
    "固定资产折旧",         # 补充资料行（BALANCE 只是"固定资产"）
    "资产减值准备",         # 补充资料行（INCOME 只有"资产减值损失"）
}

# 表头日期列模式（资产负债表 vs 利润 / 现金流）
_DATE_COL_RE_TPL = r"{fy}\s*年\s*12\s*月\s*31\s*日"           # 资产负债表
_PERIOD_COL_RE_TPL = r"{fy}\s*年(?:\s*度|\s*1\s*-\s*12\s*月|\s*全年|$|\s)"  # 利润/现金流

# "单位：千元" / "单位:百万元" 这种行内单位声明
_UNIT_INLINE_RE = re.compile(r"单位\s*[:：]\s*(元|千元|万元|百万元|亿元)")

# 退一步用的当期列匹配
_FALLBACK_PERIOD_RE = re.compile(r"本期|本年|期末|本年累计|本期金额|期末余额")

# 子项前缀 — 抽取时不直接用，但保留方便日后扩展
_PARENT_MARKER = "母公司"
_CONSOL_MARKER = "合并"


# ============== 输出诊断对象 ==============


@dataclass
class TableClassification:
    """classify_table 的结构化返回。下游测试 / 调试用得上。"""

    statement_type: StatementType
    unit_label: str
    unit_multiplier: float
    is_consolidated: bool
    score: int  # 关键词命中数；OTHER 时为命中最高表的分数


# ============== 主 API ==============


def classify_table(table: Table, caption: str | None = None) -> TableClassification:
    """根据 raw_2d 内容判定 statement_type + 单位 + 母公司/合并。

    caption=None 时使用 table.caption（html_loader 在 ingest 时捕获的紧邻文本）。
    OTHER 表示无法判定，下游应跳过（不报错）。
    """
    rows = table.raw_2d
    eff_caption = caption if caption is not None else (table.caption or "")
    if not rows:
        return TableClassification(StatementType.OTHER, "元", 1.0, True, 0)

    # 第一列前 40 行拼起来用于关键词匹配（财务表很少超过 40 行）
    first_col_text = "\n".join((r[0] if r else "") for r in rows[:40])

    b_score = sum(1 for kw in _BALANCE_KEYWORDS if kw in first_col_text)
    i_score = sum(1 for kw in _INCOME_KEYWORDS if kw in first_col_text)
    c_score = sum(1 for kw in _CASHFLOW_KEYWORDS if kw in first_col_text)
    best = max(b_score, i_score, c_score)

    if best < 2:
        return TableClassification(StatementType.OTHER, "元", 1.0, True, best)

    if best == b_score:
        st = StatementType.BALANCE
    elif best == c_score:
        # 当 INCOME / CASHFLOW 同分时，cashflow 关键词更具体；按 cashflow 优先
        st = StatementType.CASHFLOW
    else:
        st = StatementType.INCOME

    # 单位检测：caption 优先，再扫表内前 4 行
    unit_label, unit_mult = parse_unit_from_caption(eff_caption)
    if unit_mult == 1.0:
        # parse_unit_from_caption 只匹配 "单位：X" 形式，但 caption 也可能写成
        # "合并资产负债表 单位:千元 币种：人民币"，这种 _UNIT_INLINE_RE 也能抓到
        m = _UNIT_INLINE_RE.search(eff_caption)
        if m:
            unit_label = m.group(1)
            unit_mult = UNIT_MULTIPLIER[unit_label]
    if unit_mult == 1.0:
        for r in rows[:4]:
            for cell in r:
                m = _UNIT_INLINE_RE.search(cell or "")
                if m:
                    unit_label = m.group(1)
                    unit_mult = UNIT_MULTIPLIER[unit_label]
                    break
            if unit_mult != 1.0:
                break

    # 母公司 vs 合并：只看 caption（数据行里"母公司"出现频繁，会误伤）。
    # 数据表内即便有"归属母公司..."这类行，也不影响"合并报表"的本质。
    is_consol = True
    if eff_caption:
        if _PARENT_MARKER in eff_caption and _CONSOL_MARKER not in eff_caption:
            is_consol = False

    return TableClassification(
        statement_type=st,
        unit_label=unit_label,
        unit_multiplier=unit_mult,
        is_consolidated=is_consol,
        score=best,
    )


def _pick_value_column(
    rows: list[list[str]],
    fiscal_year: int,
    statement_type: StatementType,
) -> int | None:
    """在表头里找当期数值列的索引。

    优先级：
    1) 包含 "<fy>年12月31日"（BALANCE）或 "<fy>年度"（INCOME/CASHFLOW）
    2) 包含 本期 / 本年 / 期末 / 本年累计 / 本期金额 / 期末余额
    3) fallback 到第二列（line item 之后第一个数据列）
    """
    if not rows:
        return None
    fy_str = str(fiscal_year)
    if statement_type == StatementType.BALANCE:
        period_re = re.compile(_DATE_COL_RE_TPL.format(fy=fy_str))
    else:
        period_re = re.compile(_PERIOD_COL_RE_TPL.format(fy=fy_str))

    for header_row in rows[:4]:
        for j, cell in enumerate(header_row):
            if period_re.search(cell or ""):
                return j

    for header_row in rows[:4]:
        for j, cell in enumerate(header_row):
            if _FALLBACK_PERIOD_RE.search(cell or ""):
                return j

    n_cols = max((len(r) for r in rows), default=0)
    return 1 if n_cols >= 2 else None


def _extract_with(
    table: Table,
    cls: TableClassification,
    fiscal_year: int,
    ticker: str,
    source_path: str,
) -> list[FinancialLine]:
    """用既定 classification 抽 FinancialLine。"""
    if cls.statement_type == StatementType.OTHER:
        return []
    rows = table.raw_2d
    value_col = _pick_value_column(rows, fiscal_year, cls.statement_type)
    if value_col is None:
        return []

    out: list[FinancialLine] = []
    for row_idx, row in enumerate(rows):
        if not row:
            continue
        line_text = row[0]
        canonical = lookup_canonical(line_text, cls.statement_type)
        if not canonical:
            continue
        if value_col >= len(row):
            continue
        raw_val = row[value_col]
        val = parse_numeric(raw_val)
        if val is None:
            continue
        anchor = table.bbox_anchor or f"TABLE_{table.index}"
        out.append(
            FinancialLine(
                ticker=ticker,
                fiscal_period=f"FY{fiscal_year}",
                statement_type=cls.statement_type,
                line_item=line_text.strip(),
                line_item_canonical=canonical,
                value=val * cls.unit_multiplier,
                unit="元",
                is_consolidated=cls.is_consolidated,
                source_path=source_path,
                source_locator=f"{anchor}#row_{row_idx}",
            )
        )
    return out


def extract_lines_from_table(
    table: Table,
    fiscal_year: int,
    ticker: str,
    source_path: str,
    caption: str | None = None,
) -> list[FinancialLine]:
    """从单张表抽 FinancialLine。OTHER 或找不到当期列时返回 []。

    caption=None 时使用 table.caption。单表使用，不会做单位继承（只能看自己）。
    """
    cls = classify_table(table, caption=caption)
    return _extract_with(table, cls, fiscal_year, ticker, source_path)


# 主表判定门槛 — 两条任意命中即视作"独立主表"：
#   - score >= 7：很多 income / cashflow 关键词同时出现，强信号
#   - score >= 5 且 rows >= 20：典型主三大表的最少长度
# 紧邻型延续片段：score >= 2 且与上一个 main 表同类型且中间无 OTHER 表
_MAIN_STRONG_SCORE = 7
_MAIN_LARGE_SCORE = 5
_MAIN_LARGE_ROWS = 20
_CONTINUATION_MIN_SCORE = 2


def _is_strong_main(cls: TableClassification, n_rows: int) -> bool:
    if cls.score >= _MAIN_STRONG_SCORE:
        return True
    if cls.score >= _MAIN_LARGE_SCORE and n_rows >= _MAIN_LARGE_ROWS:
        return True
    return False


def extract_from_report(report: ParsedReport) -> list[FinancialLine]:
    """从一份 ParsedReport 的所有 <table> 中抽 FinancialLine（合并三大表）。

    四个核心防御：

    1) **单位继承**：HTML 把一份合并资产负债表拆成多张 <table> 时，只有第一张带
       "单位：千元" caption；后续片段 caption 为空 → unit=元（错误）。
       策略：遇到同类型相邻片段且当前 unit 默认时，沿用上一张的单位。

    2) **噪声表过滤**：试运行销售 / 单一子公司 / 部分季度数据 等小表也会包含
       "营业收入" 这种 canonical 关键词，会被 last-wins upsert 顶掉主表数据。
       策略：分两类来通过——
         - 强主表：score>=7 或 (score>=5 AND rows>=20)
         - 紧邻延续：上一张被采用的主表是同类型，且中间没插过其他表
       两类都不命中的小表全部跳过，包括 score=5 但 rows<20 的伪主表。

    3) **延续上下文重置**：碰到 OTHER 类型的表（人员构成、股东、会计政策等），
       立即清掉 prev_main_st，避免远距离误继承单位 / 误判延续。

    4) **first-win 去重**：年报里"主三大表"通常先出现，后面零散出现的子表
       （分部收入分解、各产品线收入、按地区收入…）也可能命中相同 canonical，
       例如 SMIC 2024 报告里有一张 segment table 行内含"营业收入"，会被分类为
       INCOME 主表延续；其值是分部聚合后的某一行（96 亿），下游 financials_store
       默认 INSERT OR REPLACE 后就把真正的合并营业收入（673 亿）顶掉。
       策略：在本函数内按 (statement_type, line_item_canonical, is_consolidated)
       做 first-win 去重——主表先写入的值，后续表里再出现的同 key 行一律丢弃。
       注意 key 包含 is_consolidated，因此合并报表与母公司报表同 canonical 仍可
       并存（financials_store 主键也包含 is_consolidated）。
    """
    out: list[FinancialLine] = []
    seen: set[tuple[StatementType, str, bool]] = set()  # first-win dedup
    prev_main_st: StatementType | None = None
    prev_unit_label: str = "元"
    prev_unit_mult: float = 1.0

    for tbl in report.tables:
        cls = classify_table(tbl)
        n_rows = len(tbl.raw_2d)

        if cls.statement_type == StatementType.OTHER:
            # OTHER 表打断主表上下文（防止远距离误延续）
            prev_main_st = None
            prev_unit_mult = 1.0
            continue

        is_strong = _is_strong_main(cls, n_rows)
        is_continuation = (
            not is_strong
            and cls.statement_type == prev_main_st
            and cls.score >= _CONTINUATION_MIN_SCORE
        )

        if not (is_strong or is_continuation):
            # 噪声表跳过；保持 prev_main_st 不变（同样不算「打断上下文」，
            # 因为它通常是 INCOME 类的小表夹在 INCOME 主表延续之间，没必要切断）
            continue

        # 单位继承：只要和上一张主表/延续紧邻、同类型、且当前未识别出单位，就沿用。
        # 注意：HTML 经常把一张资产负债表拆成 3 个 <table>，每张行数 >40，
        # 都会被 _is_strong_main 命中；但只有第一张带 caption。所以不能仅在
        # is_continuation 时继承。
        if (
            cls.unit_multiplier == 1.0
            and cls.statement_type == prev_main_st
            and prev_unit_mult != 1.0
        ):
            cls = TableClassification(
                statement_type=cls.statement_type,
                unit_label=prev_unit_label,
                unit_multiplier=prev_unit_mult,
                is_consolidated=cls.is_consolidated,
                score=cls.score,
            )

        new_lines = _extract_with(
            tbl, cls, report.fiscal_year, report.ticker, report.source_path
        )
        for ln in new_lines:
            key = (ln.statement_type, ln.line_item_canonical, ln.is_consolidated)
            if key in seen:
                # first-win：先出现的主表值优先，后续同 key 一律丢弃
                continue
            seen.add(key)
            out.append(ln)

        prev_main_st = cls.statement_type
        prev_unit_label = cls.unit_label
        prev_unit_mult = cls.unit_multiplier
    return out
