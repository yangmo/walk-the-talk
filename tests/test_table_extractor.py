"""Tests for table_extractor + financials_store + _taxonomy 数值/单位 helpers。"""

from __future__ import annotations

from pathlib import Path

import pytest

from walk_the_talk.core.enums import StatementType
from walk_the_talk.core.models import FinancialLine, ParsedReport, Table
from walk_the_talk.ingest import (
    FinancialsStore,
    classify_table,
    extract_from_report,
    extract_lines_from_table,
    load_html,
)
from walk_the_talk.ingest._taxonomy import (
    lookup_canonical,
    normalize_line_item_text,
    parse_numeric,
    parse_unit_from_caption,
)

FIXTURE = Path(__file__).parent / "fixtures" / "中芯国际" / "2025.html"


# ============== _taxonomy ==============


def test_normalize_strips_numerals():
    assert normalize_line_item_text("一、营业总收入") == ("营业总收入", False)
    assert normalize_line_item_text("（一）按经营分部")[0] == "按经营分部"
    assert normalize_line_item_text("1.持续经营净利润")[0] == "持续经营净利润"


def test_normalize_marks_sub_item():
    cleaned, is_sub = normalize_line_item_text("其中：利息费用")
    assert cleaned == "利息费用"
    assert is_sub is True


def test_normalize_strips_tail_note():
    cleaned, _ = normalize_line_item_text('净利润（亏损以"－"号填列）')
    assert cleaned == "净利润"


def test_lookup_canonical_basic():
    assert lookup_canonical("一、营业总收入", StatementType.INCOME) == "total_revenue"
    assert lookup_canonical("货币资金", StatementType.BALANCE) == "cash"
    assert lookup_canonical(
        "购建固定资产、无形资产和其他长期资产支付的现金", StatementType.CASHFLOW
    ) == "capex"
    assert lookup_canonical("不存在的科目", StatementType.INCOME) is None
    # 错位查（cashflow 关键词查 income）应当失败
    assert lookup_canonical("购建固定资产、无形资产和其他长期资产支付的现金",
                            StatementType.INCOME) is None


def test_parse_unit_from_caption():
    assert parse_unit_from_caption("单位：千元") == ("千元", 1_000.0)
    assert parse_unit_from_caption("合并资产负债表  单位:百万元") == ("百万元", 1_000_000.0)
    assert parse_unit_from_caption("") == ("元", 1.0)
    assert parse_unit_from_caption("无单位说明") == ("元", 1.0)


def test_parse_numeric_variants():
    assert parse_numeric("1,234.56") == 1234.56
    assert parse_numeric("1，234.56") == 1234.56          # 全角逗号
    assert parse_numeric("(1,234)") == -1234.0            # 会计括号
    assert parse_numeric("（1,234）") == -1234.0          # 全角括号
    assert parse_numeric("－123") == -123.0
    assert parse_numeric("−45.67") == -45.67
    assert parse_numeric("-89") == -89.0
    assert parse_numeric("0") == 0.0
    # 未披露
    assert parse_numeric("-") is None
    assert parse_numeric("—") is None
    assert parse_numeric("") is None
    assert parse_numeric("不适用") is None


# ============== classify_table ==============


def _mk_table(rows: list[list[str]], idx: int = 0, anchor: str | None = None) -> Table:
    return Table(
        index=idx,
        markdown="",  # 测试不关心
        raw_2d=rows,
        bbox_anchor=anchor or f"TABLE_PLACEHOLDER_{idx}",
    )


def test_classify_balance():
    rows = [
        ["项目", "2025年12月31日", "2024年12月31日"],
        ["流动资产：", "", ""],
        ["货币资金", "1,000", "900"],
        ["应收账款", "200", "150"],
        ["存货", "50", "40"],
        ["流动资产合计", "1,250", "1,090"],
        ["资产总计", "5,000", "4,500"],
    ]
    cls = classify_table(_mk_table(rows))
    assert cls.statement_type == StatementType.BALANCE
    assert cls.is_consolidated is True


def test_classify_income():
    rows = [
        ["项目", "2025年度", "2024年度"],
        ["一、营业总收入", "10,000", "9,000"],
        ["营业收入", "9,800", "8,800"],
        ["二、营业总成本", "8,000", "7,500"],
        ["营业成本", "6,000", "5,500"],
        ["研发费用", "800", "700"],
        ["净利润", "1,200", "1,000"],
        ["归属于母公司股东的净利润", "1,100", "950"],
    ]
    cls = classify_table(_mk_table(rows))
    assert cls.statement_type == StatementType.INCOME


def test_classify_cashflow():
    rows = [
        ["项目", "2025年度", "2024年度"],
        ["一、经营活动产生的现金流量：", "", ""],
        ["销售商品、提供劳务收到的现金", "8,000", "7,000"],
        ["经营活动现金流入小计", "8,500", "7,400"],
        ["经营活动现金流出小计", "5,000", "4,500"],
        ["经营活动产生的现金流量净额", "3,500", "2,900"],
        ["二、投资活动产生的现金流量：", "", ""],
        ["购建固定资产、无形资产和其他长期资产支付的现金", "2,000", "1,800"],
        ["投资活动产生的现金流量净额", "-1,500", "-1,200"],
        ["三、筹资活动产生的现金流量：", "", ""],
        ["筹资活动产生的现金流量净额", "500", "400"],
    ]
    cls = classify_table(_mk_table(rows))
    assert cls.statement_type == StatementType.CASHFLOW


def test_classify_other_for_non_financial_table():
    rows = [
        ["人员构成", "数量", "比例"],
        ["研发人员", "500", "20%"],
        ["生产人员", "1500", "60%"],
    ]
    cls = classify_table(_mk_table(rows))
    assert cls.statement_type == StatementType.OTHER


def test_classify_unit_inline():
    rows = [
        ["项目", "2025年度", "2024年度"],
        ["单位：千元", "", ""],
        ["营业收入", "10,000", "9,000"],
        ["营业成本", "6,000", "5,500"],
        ["净利润", "1,200", "1,000"],
        ["利润总额", "1,400", "1,150"],
    ]
    cls = classify_table(_mk_table(rows))
    assert cls.statement_type == StatementType.INCOME
    assert cls.unit_label == "千元"
    assert cls.unit_multiplier == 1_000.0


def test_classify_unit_from_caption():
    rows = [
        ["项目", "2025年度", "2024年度"],
        ["营业收入", "10", "9"],
        ["营业成本", "6", "5.5"],
        ["净利润", "1.2", "1"],
        ["利润总额", "1.4", "1.15"],
    ]
    cls = classify_table(_mk_table(rows), caption="合并利润表  单位：亿元")
    assert cls.statement_type == StatementType.INCOME
    assert cls.unit_multiplier == 100_000_000.0


def test_classify_uses_table_caption_field():
    """没有显式传 caption 时，应回落到 table.caption。"""
    rows = [
        ["项目", "2025年12月31日", "2024年12月31日"],
        ["货币资金", "1,000", "900"],
        ["应收账款", "200", "150"],
        ["存货", "50", "40"],
        ["资产总计", "5,000", "4,500"],
    ]
    tbl = Table(
        index=0, markdown="", raw_2d=rows,
        bbox_anchor="TABLE_PLACEHOLDER_0",
        caption="合并资产负债表  单位：千元 币种：人民币",
    )
    cls = classify_table(tbl)  # 不传 caption
    assert cls.statement_type == StatementType.BALANCE
    assert cls.unit_multiplier == 1_000.0
    assert cls.is_consolidated is True


def test_classify_parent_only_via_caption():
    """母公司 vs 合并 由 caption 决定（real-world: <p>母公司资产负债表</p><table>...）。"""
    rows = [
        ["项目", "2025年12月31日", "2024年12月31日"],
        ["货币资金", "1,000", "900"],
        ["应收账款", "200", "150"],
        ["存货", "50", "40"],
        ["资产总计", "5,000", "4,500"],
    ]
    cls = classify_table(_mk_table(rows), caption="母公司资产负债表 单位：千元")
    assert cls.statement_type == StatementType.BALANCE
    assert cls.is_consolidated is False
    assert cls.unit_multiplier == 1_000.0


# ============== extract_lines_from_table ==============


def test_extract_balance_with_unit_thousand():
    rows = [
        ["项目", "2025年12月31日", "2024年12月31日"],
        ["单位：千元", "", ""],
        ["货币资金", "1,000", "900"],
        ["应收账款", "200", "150"],
        ["存货", "50", "40"],
        ["流动资产合计", "1,250", "1,090"],
        ["资产总计", "5,000", "4,500"],
    ]
    lines = extract_lines_from_table(
        _mk_table(rows, idx=3),
        fiscal_year=2025,
        ticker="688981",
        source_path="x.html",
    )
    assert len(lines) == 5
    by_canon = {ln.line_item_canonical: ln for ln in lines}
    assert by_canon["cash"].value == 1_000 * 1_000.0  # 千元 → 元
    assert by_canon["total_assets"].value == 5_000 * 1_000.0
    assert all(ln.fiscal_period == "FY2025" for ln in lines)
    assert all(ln.is_consolidated for ln in lines)


def test_extract_income_picks_correct_year_column():
    """有 2025 / 2024 两列，extract 应取 2025 那列。"""
    rows = [
        ["项目", "2024年度", "2025年度"],   # 顺序倒一下
        ["一、营业总收入", "9,000", "10,000"],
        ["营业成本", "5,500", "6,000"],
        ["研发费用", "700", "800"],
        ["净利润", "1,000", "1,200"],
        ["利润总额", "1,150", "1,400"],
    ]
    lines = extract_lines_from_table(
        _mk_table(rows),
        fiscal_year=2025,
        ticker="688981",
        source_path="x.html",
    )
    by = {ln.line_item_canonical: ln.value for ln in lines}
    assert by["total_revenue"] == 10_000.0   # 不是 9,000
    assert by["net_profit"] == 1_200.0


def test_extract_skips_unmappable_lines():
    rows = [
        ["项目", "2025年度", "2024年度"],
        ["营业收入", "10,000", "9,000"],
        ["不在 taxonomy 里的行", "999", "888"],
        ["营业成本", "6,000", "5,500"],
        ["净利润", "1,200", "1,000"],
        ["利润总额", "1,400", "1,150"],
    ]
    lines = extract_lines_from_table(
        _mk_table(rows),
        fiscal_year=2025,
        ticker="688981",
        source_path="x.html",
    )
    assert "999" not in [str(ln.value) for ln in lines]
    canons = {ln.line_item_canonical for ln in lines}
    assert "revenue" in canons
    assert "net_profit" in canons


def test_extract_handles_paren_negative():
    rows = [
        ["项目", "2025年度", "2024年度"],
        ["营业收入", "10,000", "9,000"],
        ["营业成本", "6,000", "5,500"],
        ["营业利润", "(500)", "(300)"],     # 亏损
        ["净利润", "(800)", "(500)"],
        ["利润总额", "(700)", "(450)"],
    ]
    lines = extract_lines_from_table(
        _mk_table(rows),
        fiscal_year=2025,
        ticker="688981",
        source_path="x.html",
    )
    by = {ln.line_item_canonical: ln.value for ln in lines}
    assert by["operating_profit"] == -500.0
    assert by["net_profit"] == -800.0


def test_extract_returns_empty_for_other_table():
    rows = [
        ["人员构成", "数量"],
        ["研发人员", "500"],
        ["生产人员", "1500"],
    ]
    assert extract_lines_from_table(
        _mk_table(rows),
        fiscal_year=2025,
        ticker="688981",
        source_path="x.html",
    ) == []


# ============== extract_from_report：first-win 去重 ==============


def test_extract_from_report_first_win_dedup():
    """主表先出现的 (statement_type, canonical) 优先；后续表同 key 行被丢弃。

    重现 SMIC FY2024 revenue-overwrite bug：主利润表 revenue=10000 先写入，
    后面的 segment table 也命中"营业收入" canonical=revenue，但其值（96）
    不应该把主表的 10000 覆盖掉。
    """
    main_income = [
        ["项目", "2025年度", "2024年度"],
        ["一、营业总收入", "10,000", "9,000"],
        ["营业收入", "10,000", "9,000"],
        ["二、营业总成本", "8,000", "7,500"],
        ["营业成本", "6,000", "5,500"],
        ["营业利润", "1,500", "1,200"],
        ["利润总额", "1,400", "1,150"],
        ["净利润", "1,200", "1,000"],
        ["归属于母公司股东的净利润", "1,100", "950"],
        ["研发费用", "800", "700"],
        ["销售费用", "200", "180"],
        ["管理费用", "300", "270"],
    ]
    # 后面的 segment-style 表：仍含"营业收入" 但值是分部聚合（96），
    # score=3 + 同类型 prev_main_st=INCOME → 被认作 continuation 而进入抽取
    segment_income = [
        ["项目", "2025年度"],
        ["营业收入", "96"],
        ["营业成本", "70"],
        ["净利润", "10"],
    ]
    rp = ParsedReport(
        ticker="688981",
        fiscal_year=2025,
        source_path="x.html",
        encoding="utf-8",
        sections=[],
        tables=[
            Table(index=0, markdown="", raw_2d=main_income, bbox_anchor="TABLE_0"),
            Table(index=1, markdown="", raw_2d=segment_income, bbox_anchor="TABLE_1"),
        ],
    )
    lines = extract_from_report(rp)

    by_canon: dict[str, list[FinancialLine]] = {}
    for ln in lines:
        by_canon.setdefault(ln.line_item_canonical, []).append(ln)

    # revenue 只出现一次，且取自主表（10000，不是 96）
    assert "revenue" in by_canon
    assert len(by_canon["revenue"]) == 1
    assert by_canon["revenue"][0].value == 10_000.0
    assert by_canon["revenue"][0].source_locator.startswith("TABLE_0")

    # 同样地：营业成本、净利润都来自主表
    assert by_canon["cost_of_revenue"][0].value == 6_000.0
    assert by_canon["cost_of_revenue"][0].source_locator.startswith("TABLE_0")
    assert by_canon["net_profit"][0].value == 1_200.0
    assert by_canon["net_profit"][0].source_locator.startswith("TABLE_0")


def test_extract_from_report_first_win_preserves_consol_vs_parent():
    """first-win 的 key 包含 is_consolidated；合并报表与母公司报表 canonical 仍可并存。"""
    consol_income = [
        ["项目", "2025年度", "2024年度"],
        ["一、营业总收入", "10,000", "9,000"],
        ["营业收入", "10,000", "9,000"],
        ["二、营业总成本", "8,000", "7,500"],
        ["营业成本", "6,000", "5,500"],
        ["营业利润", "1,500", "1,200"],
        ["利润总额", "1,400", "1,150"],
        ["净利润", "1,200", "1,000"],
        ["归属于母公司股东的净利润", "1,100", "950"],
        ["研发费用", "800", "700"],
        ["销售费用", "200", "180"],
        ["管理费用", "300", "270"],
    ]
    parent_income = [
        ["项目", "2025年度", "2024年度"],
        ["一、营业总收入", "5,000", "4,500"],
        ["营业收入", "5,000", "4,500"],
        ["二、营业总成本", "4,000", "3,800"],
        ["营业成本", "3,000", "2,800"],
        ["营业利润", "700", "550"],
        ["利润总额", "650", "500"],
        ["净利润", "550", "420"],
        ["归属于母公司股东的净利润", "550", "420"],
        ["研发费用", "300", "260"],
        ["销售费用", "100", "90"],
        ["管理费用", "120", "100"],
    ]
    rp = ParsedReport(
        ticker="688981",
        fiscal_year=2025,
        source_path="x.html",
        encoding="utf-8",
        sections=[],
        tables=[
            Table(index=0, markdown="", raw_2d=consol_income, bbox_anchor="TABLE_0",
                  caption="合并利润表"),
            # 中间的 OTHER 表打断主表上下文，避免被当成 continuation
            Table(index=1, markdown="",
                  raw_2d=[["人员构成", "数量"], ["研发人员", "500"], ["生产人员", "1500"]],
                  bbox_anchor="TABLE_1"),
            Table(index=2, markdown="", raw_2d=parent_income, bbox_anchor="TABLE_2",
                  caption="母公司利润表"),
        ],
    )
    lines = extract_from_report(rp)

    # 同 canonical 但 is_consolidated 不同应该都保留
    by_key: dict[tuple[str, bool], FinancialLine] = {}
    for ln in lines:
        by_key[(ln.line_item_canonical, ln.is_consolidated)] = ln
    assert by_key[("revenue", True)].value == 10_000.0
    assert by_key[("revenue", False)].value == 5_000.0


# ============== FinancialsStore ==============


def _mk_line(canonical: str, value: float, st: StatementType = StatementType.INCOME,
             fy: str = "FY2025", consol: bool = True, ticker: str = "T") -> FinancialLine:
    return FinancialLine(
        ticker=ticker,
        fiscal_period=fy,
        statement_type=st,
        line_item=canonical,
        line_item_canonical=canonical,
        value=value,
        unit="元",
        is_consolidated=consol,
        source_path="x.html",
        source_locator="t#0",
    )


def test_store_upsert_and_get_value(tmp_path: Path):
    db = tmp_path / "fin.db"
    with FinancialsStore(db) as store:
        n = store.upsert_lines([
            _mk_line("revenue", 1_000.0),
            _mk_line("net_profit", 100.0),
        ])
        assert n == 2
        assert store.count() == 2
        assert store.get_value("T", "FY2025", "revenue") == 1_000.0
        assert store.get_value("T", "FY2025", "missing") is None


def test_store_upsert_replaces_existing(tmp_path: Path):
    db = tmp_path / "fin.db"
    with FinancialsStore(db) as store:
        store.upsert_lines([_mk_line("revenue", 1_000.0)])
        store.upsert_lines([_mk_line("revenue", 2_000.0)])
        assert store.count() == 1
        assert store.get_value("T", "FY2025", "revenue") == 2_000.0


def test_store_consolidated_vs_parent_coexist(tmp_path: Path):
    db = tmp_path / "fin.db"
    with FinancialsStore(db) as store:
        store.upsert_lines([
            _mk_line("revenue", 1_000.0, consol=True),
            _mk_line("revenue", 800.0, consol=False),
        ])
        assert store.count() == 2
        assert store.get_value("T", "FY2025", "revenue", is_consolidated=True) == 1_000.0
        assert store.get_value("T", "FY2025", "revenue", is_consolidated=False) == 800.0


def test_store_get_series(tmp_path: Path):
    db = tmp_path / "fin.db"
    with FinancialsStore(db) as store:
        store.upsert_lines([
            _mk_line("revenue", 100.0, fy="FY2022"),
            _mk_line("revenue", 200.0, fy="FY2023"),
            _mk_line("revenue", 300.0, fy="FY2024"),
            _mk_line("revenue", 400.0, fy="FY2025"),
        ])
        s = store.get_series("T", "revenue")
        assert s == {"FY2022": 100.0, "FY2023": 200.0, "FY2024": 300.0, "FY2025": 400.0}
        s2 = store.get_series("T", "revenue", fiscal_periods=["FY2024", "FY2025"])
        assert s2 == {"FY2024": 300.0, "FY2025": 400.0}


def test_store_query_by_period(tmp_path: Path):
    db = tmp_path / "fin.db"
    with FinancialsStore(db) as store:
        store.upsert_lines([
            _mk_line("revenue", 100.0, st=StatementType.INCOME, fy="FY2024"),
            _mk_line("net_profit", 10.0, st=StatementType.INCOME, fy="FY2024"),
            _mk_line("cash", 50.0, st=StatementType.BALANCE, fy="FY2024"),
            _mk_line("revenue", 200.0, st=StatementType.INCOME, fy="FY2025"),
        ])
        rows_24 = store.query("T", fiscal_period="FY2024")
        assert len(rows_24) == 3
        rows_24_inc = store.query("T", fiscal_period="FY2024",
                                   statement_type=StatementType.INCOME)
        assert len(rows_24_inc) == 2


def test_store_list_periods(tmp_path: Path):
    db = tmp_path / "fin.db"
    with FinancialsStore(db) as store:
        store.upsert_lines([
            _mk_line("revenue", 100.0, fy="FY2023"),
            _mk_line("revenue", 200.0, fy="FY2025"),
            _mk_line("revenue", 150.0, fy="FY2024"),
        ])
        assert store.list_periods("T") == ["FY2023", "FY2024", "FY2025"]


def test_store_persistence(tmp_path: Path):
    db = tmp_path / "fin.db"
    with FinancialsStore(db) as s1:
        s1.upsert_lines([_mk_line("revenue", 1_000.0)])
    # 重新打开
    with FinancialsStore(db) as s2:
        assert s2.count() == 1
        assert s2.get_value("T", "FY2025", "revenue") == 1_000.0


# ============== 端到端：SMIC 2025 ==============


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture missing")
def test_smic_2025_extract_and_store(tmp_path: Path):
    rp = load_html(FIXTURE)
    lines = extract_from_report(rp)

    # 至少 BALANCE / INCOME / CASHFLOW 都有
    sts = {ln.statement_type for ln in lines}
    assert StatementType.BALANCE in sts
    assert StatementType.INCOME in sts
    assert StatementType.CASHFLOW in sts

    # 必须能抽到这些核心 canonical
    canons = {ln.line_item_canonical for ln in lines}
    must_have = {
        "total_assets", "total_equity", "total_liabilities", "cash", "fixed_assets",
        "revenue", "net_profit", "rd_expense", "total_revenue",
        "ocf", "capex",
    }
    missing = must_have - canons
    assert not missing, f"missing canonical: {missing}"

    # 写入 SQLite，再独立打开做精度校验
    db = tmp_path / "fin.db"
    with FinancialsStore(db) as store:
        n = store.upsert_lines(lines)
        assert n == len(lines)

    with FinancialsStore(db) as store2:
        # 每个 canonical 至少要在「合理量级」内（粗校，防止单位错乱再发生）
        rev = store2.get_value("688981", "FY2025", "revenue")
        net = store2.get_value("688981", "FY2025", "net_profit")
        rd = store2.get_value("688981", "FY2025", "rd_expense")
        ta = store2.get_value("688981", "FY2025", "total_assets")
        te = store2.get_value("688981", "FY2025", "total_equity")
        tl = store2.get_value("688981", "FY2025", "total_liabilities")
        ocf = store2.get_value("688981", "FY2025", "ocf")
        capex = store2.get_value("688981", "FY2025", "capex")

        # SMIC 2025 实际营收约 673 亿，净利约 72 亿
        assert 5e10 < rev < 1e11, f"revenue={rev}"
        assert 5e9 < net < 2e10, f"net_profit={net}"
        assert 1e9 < rd < 1e10, f"rd_expense={rd}"

        # 资产负债表：总资产 ~3677 亿
        assert 2e11 < ta < 5e11, f"total_assets={ta}"
        assert 1e11 < te < 4e11, f"total_equity={te}"
        assert 5e10 < tl < 2e11, f"total_liabilities={tl}"
        # 资产 = 负债 + 权益（差额 < 0.1%）
        assert abs(ta - (tl + te)) / ta < 1e-3, f"BS not balanced: A={ta} L+E={tl+te}"

        # 现金流量表：OCF 正、CapEx 正（绝对值都很大）
        assert ocf > 1e10, f"ocf={ocf}"
        assert capex > 1e10, f"capex={capex}"
