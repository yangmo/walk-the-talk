"""Phase 3 verify 三个原子工具测试：compute / query_financials / query_chunks。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from walk_the_talk.core.enums import StatementType
from walk_the_talk.core.models import FinancialLine
from walk_the_talk.ingest.financials_store import FinancialsStore
from walk_the_talk.verify.tools import (
    _build_where,
    _suggest_alias,
    compute,
    query_chunks,
    query_financials,
)

# ============== compute ==============


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        # 基础算术
        ("1 + 2", 3),
        ("3 * 4", 12),
        ("10 / 4", 2.5),
        ("10 // 4", 2),
        ("10 % 3", 1),
        ("2 ** 10", 1024),
        # 一元
        ("-5", -5),
        ("+5", 5),
        ("not False", True),
        # 复合
        ("(57796 - 45525) / 45525", round(12271 / 45525, 12)),
        # 比较
        ("3 >= 2", True),
        ("3 == 3", True),
        ("3 != 4", True),
        # 链式比较
        ("1 < 2 < 3", True),
        ("1 < 5 < 3", False),
        # 布尔
        ("True and False", False),
        ("True or False", True),
        # 内置函数
        ("abs(-7.5)", 7.5),
        ("min(1, 2, 3)", 1),
        ("max(1, 2, 3)", 3),
        ("round(3.14159, 2)", 3.14),
        # 真实业务场景
        ("(57796 - 45525) / 45525 >= 0.30", False),  # 27% 增长不到 30% 门槛
        ("abs(7.3 - 7.5) / 7.5 <= 0.05", True),  # 偏离 ≤5%
    ],
)
def test_compute_happy_paths(expr: str, expected: Any) -> None:
    out = compute(expr)
    assert out["expr"] == expr
    assert "error" not in out
    assert out["value"] == expected


def test_compute_division_by_zero() -> None:
    out = compute("1 / 0")
    assert "error" in out
    assert "zero" in out["error"].lower()


def test_compute_empty_expr() -> None:
    assert "error" in compute("")
    assert "error" in compute("   ")


def test_compute_syntax_error() -> None:
    out = compute("1 + ")
    assert "error" in out
    assert "syntax" in out["error"].lower()


@pytest.mark.parametrize(
    "expr",
    [
        # 危险节点：拒绝
        "__import__('os').system('rm -rf /')",  # Call to disallowed name
        "open('/etc/passwd').read()",  # disallowed function
        "exec('print(1)')",
        "eval('1+1')",
        "lambda x: x",  # Lambda
        "[x for x in range(10)]",  # ListComp
        "{1: 2}",  # Dict
        "(1, 2, 3)",  # Tuple
        "[1, 2, 3]",  # List
        "x + 1",  # bare Name
        "'hello'.upper()",  # Attribute
        "globals()",
        "abs(1, 2, 3)",  # 这个不报 ComputeError，但运行时 abs() 拒绝多参数 → TypeError
    ],
)
def test_compute_rejects_dangerous(expr: str) -> None:
    out = compute(expr)
    assert "error" in out, f"应该被拒绝但通过了: {expr!r} → {out!r}"


def test_compute_string_constant_rejected() -> None:
    """字符串常量不允许（避免 LLM 误传 'capex' 这种）。"""
    out = compute("'hello'")
    assert "error" in out
    assert "constant" in out["error"].lower()


def test_compute_floating_point_normalized() -> None:
    """浮点截 12 位，避免 0.30000000000000004 这种污染输出。"""
    out = compute("0.1 + 0.2")
    assert out["value"] == 0.3


def test_compute_with_keyword_args_rejected() -> None:
    out = compute("round(3.14, ndigits=1)")
    assert "error" in out
    assert "keyword" in out["error"].lower()


# ============== query_financials ==============


@pytest.fixture
def fs(tmp_path: Path):
    """SMIC 风格小型 financials.db。"""
    store = FinancialsStore(tmp_path / "financials.db")
    store.upsert_lines(
        [
            FinancialLine(
                ticker="688981",
                fiscal_period=f"FY{y}",
                statement_type=StatementType.INCOME,
                line_item="营业收入",
                line_item_canonical="revenue",
                value=v,
            )
            for y, v in [(2022, 4.55e10), (2023, 4.52e10), (2024, 5.78e10), (2025, 6.50e10)]
        ]
    )
    store.upsert_lines(
        [
            FinancialLine(
                ticker="688981",
                fiscal_period=f"FY{y}",
                statement_type=StatementType.CAPEX,
                line_item="资本性支出",
                line_item_canonical="capex",
                value=v,
            )
            for y, v in [(2024, 7.5e9), (2025, 7.3e9)]
        ]
    )
    yield store
    store.close()


def test_query_financials_hit_subset(fs: FinancialsStore) -> None:
    out = query_financials(
        fs,
        ticker="688981",
        line_item_canonical="revenue",
        fiscal_periods=["FY2024", "FY2025"],
    )
    assert "error" not in out
    assert out["line_item"] == "revenue"
    assert out["values"] == {"FY2024": 5.78e10, "FY2025": 6.50e10}
    assert out["unit"] == "元"


def test_query_financials_hit_all_periods(fs: FinancialsStore) -> None:
    """fiscal_periods=None → 全量。"""
    out = query_financials(fs, ticker="688981", line_item_canonical="capex")
    assert out["values"] == {"FY2024": 7.5e9, "FY2025": 7.3e9}


def test_query_financials_line_item_not_found_with_alias(fs: FinancialsStore) -> None:
    """capex_yoy 不存在 → 返回 hint='did you mean capex?'"""
    out = query_financials(
        fs,
        ticker="688981",
        line_item_canonical="capex_yoy",
        fiscal_periods=["FY2025"],
    )
    assert "error" in out
    assert "not found" in out["error"]
    assert "capex" in out["available_canonicals"]
    assert out["hint"] == "did you mean 'capex'?"


def test_query_financials_line_item_not_found_no_alias(fs: FinancialsStore) -> None:
    """完全无关的 line_item → hint=None。"""
    out = query_financials(
        fs,
        ticker="688981",
        line_item_canonical="完全不存在的指标XYZ",
        fiscal_periods=["FY2025"],
    )
    assert "error" in out
    assert out["hint"] is None
    assert "revenue" in out["available_canonicals"]


def test_query_financials_period_not_found(fs: FinancialsStore) -> None:
    """line_item 存在，但请求的 fiscal_periods 都没数据。"""
    out = query_financials(
        fs,
        ticker="688981",
        line_item_canonical="revenue",
        fiscal_periods=["FY2030", "FY2031"],
    )
    assert "error" in out
    assert out["values"] == {}
    assert "FY2024" in out["available_fiscal_periods"]


def test_query_financials_wrong_ticker(fs: FinancialsStore) -> None:
    """ticker 不存在 → 走 not_found 分支，available_canonicals 空。"""
    out = query_financials(
        fs,
        ticker="999999",
        line_item_canonical="revenue",
    )
    assert "error" in out
    assert out["available_canonicals"] == []
    assert out["hint"] is None


def test_query_financials_partial_period_hit(fs: FinancialsStore) -> None:
    """请求 FY2024 + FY2030：只命中 FY2024，应返回部分命中而不是 error。"""
    out = query_financials(
        fs,
        ticker="688981",
        line_item_canonical="revenue",
        fiscal_periods=["FY2024", "FY2030"],
    )
    # 这里 series 非空 → 走 hit 分支
    assert "error" not in out
    assert out["values"] == {"FY2024": 5.78e10}


# ============== query_financials 派生字段（P1） ==============


@pytest.fixture
def fs_derived(tmp_path: Path):
    """SMIC 风格 financials.db，含派生字段计算所需的所有基础 canonical。

    数据按 SQL 3 验证结果的真实量级（亿元 -> 元）。
    """
    store = FinancialsStore(tmp_path / "financials.db")
    # 所有金额单位为元（store 的事实约定）
    base_data = {
        "revenue": {2023: 4.525e10, 2024: 5.78e10, 2025: 6.732e10},
        "cost_of_revenue": {2023: 3.530e10, 2024: 4.71e10, 2025: 5.28e10},
        "net_profit": {2023: 6.4e9, 2024: 5.4e9, 2025: 7.2e9},
        "operating_profit": {2023: 5.0e9, 2024: 4.2e9, 2025: 6.0e9},
        "ocf": {2023: 2.30e10, 2024: 2.27e10, 2025: 2.01e10},
        "capex": {2023: 5.39e10, 2024: 5.46e10, 2025: 6.00e10},
    }
    lines = []
    for canonical, by_year in base_data.items():
        for y, v in by_year.items():
            lines.append(
                FinancialLine(
                    ticker="688981",
                    fiscal_period=f"FY{y}",
                    statement_type=(
                        StatementType.CASHFLOW if canonical in ("ocf", "capex") else StatementType.INCOME
                    ),
                    line_item=canonical,
                    line_item_canonical=canonical,
                    value=v,
                )
            )
    store.upsert_lines(lines)
    yield store
    store.close()


def test_query_financials_derived_gross_margin(fs_derived: FinancialsStore) -> None:
    """gross_margin = (revenue - cost_of_revenue) / revenue。"""
    out = query_financials(
        fs_derived,
        ticker="688981",
        line_item_canonical="gross_margin",
    )
    assert "error" not in out
    assert out["line_item"] == "gross_margin"
    assert out["unit"] == "ratio"
    assert out["derived"] is True
    assert out["requires"] == ["revenue", "cost_of_revenue"]
    # FY2024: (5.78e10 - 4.71e10) / 5.78e10 ≈ 0.18512...
    assert abs(out["values"]["FY2024"] - (5.78e10 - 4.71e10) / 5.78e10) < 1e-9
    assert abs(out["values"]["FY2025"] - (6.732e10 - 5.28e10) / 6.732e10) < 1e-9
    # 三年都能算出来
    assert set(out["values"].keys()) == {"FY2023", "FY2024", "FY2025"}


def test_query_financials_derived_net_margin(fs_derived: FinancialsStore) -> None:
    out = query_financials(
        fs_derived,
        ticker="688981",
        line_item_canonical="net_margin",
        fiscal_periods=["FY2024", "FY2025"],
    )
    assert "error" not in out
    assert out["unit"] == "ratio"
    assert abs(out["values"]["FY2024"] - 5.4e9 / 5.78e10) < 1e-9
    assert abs(out["values"]["FY2025"] - 7.2e9 / 6.732e10) < 1e-9
    # fiscal_periods 过滤生效
    assert "FY2023" not in out["values"]


def test_query_financials_derived_operating_margin(fs_derived: FinancialsStore) -> None:
    out = query_financials(
        fs_derived,
        ticker="688981",
        line_item_canonical="operating_margin",
    )
    assert "error" not in out
    assert abs(out["values"]["FY2024"] - 4.2e9 / 5.78e10) < 1e-9


def test_query_financials_derived_fcf_margin(fs_derived: FinancialsStore) -> None:
    """fcf_margin = (ocf - capex) / revenue。SMIC 由于 capex>ocf 所以 fcf 为负。"""
    out = query_financials(
        fs_derived,
        ticker="688981",
        line_item_canonical="fcf_margin",
    )
    assert "error" not in out
    # FY2024: (2.27e10 - 5.46e10) / 5.78e10 ≈ -0.5519
    expected = (2.27e10 - 5.46e10) / 5.78e10
    assert abs(out["values"]["FY2024"] - expected) < 1e-9
    assert out["values"]["FY2024"] < 0  # 负 FCF


def test_query_financials_derived_missing_dependency(tmp_path: Path) -> None:
    """缺基础依赖时返回带错误信息的 dict。"""
    store = FinancialsStore(tmp_path / "fin.db")
    # 只塞 revenue，没有 cost_of_revenue → gross_margin 算不出来
    store.upsert_lines(
        [
            FinancialLine(
                ticker="T",
                fiscal_period="FY2025",
                statement_type=StatementType.INCOME,
                line_item="营业收入",
                line_item_canonical="revenue",
                value=1.0e10,
            )
        ]
    )
    out = query_financials(store, ticker="T", line_item_canonical="gross_margin")
    assert "error" in out
    assert "cost_of_revenue" in out["error"]
    assert out["derived"] is True
    assert out["requires"] == ["revenue", "cost_of_revenue"]
    store.close()


def test_query_financials_derived_zero_revenue(tmp_path: Path) -> None:
    """分母 revenue=0 → 该 fy 跳过，不返回 inf/NaN。"""
    store = FinancialsStore(tmp_path / "fin.db")
    store.upsert_lines(
        [
            FinancialLine(
                ticker="T",
                fiscal_period="FY2025",
                statement_type=StatementType.INCOME,
                line_item="营业收入",
                line_item_canonical="revenue",
                value=0.0,
            ),
            FinancialLine(
                ticker="T",
                fiscal_period="FY2025",
                statement_type=StatementType.INCOME,
                line_item="净利润",
                line_item_canonical="net_profit",
                value=5.0,
            ),
            FinancialLine(
                ticker="T",
                fiscal_period="FY2024",
                statement_type=StatementType.INCOME,
                line_item="营业收入",
                line_item_canonical="revenue",
                value=100.0,
            ),
            FinancialLine(
                ticker="T",
                fiscal_period="FY2024",
                statement_type=StatementType.INCOME,
                line_item="净利润",
                line_item_canonical="net_profit",
                value=20.0,
            ),
        ]
    )
    out = query_financials(store, ticker="T", line_item_canonical="net_margin")
    # FY2025 revenue=0 跳过；FY2024 正常算
    assert "FY2025" not in out["values"]
    assert abs(out["values"]["FY2024"] - 0.20) < 1e-9
    store.close()


def test_query_financials_derived_partial_period(fs_derived: FinancialsStore) -> None:
    """fiscal_periods 里既有可算的也有库里没的 → 命中的进 values，不报错。"""
    out = query_financials(
        fs_derived,
        ticker="688981",
        line_item_canonical="gross_margin",
        fiscal_periods=["FY2024", "FY2030"],
    )
    assert "error" not in out
    assert "FY2024" in out["values"]
    assert "FY2030" not in out["values"]


def test_query_financials_derived_no_data_for_requested(fs_derived: FinancialsStore) -> None:
    """请求的 fy 在依赖里全无数据 → error，但 derived/description 仍带回。"""
    out = query_financials(
        fs_derived,
        ticker="688981",
        line_item_canonical="gross_margin",
        fiscal_periods=["FY2030", "FY2031"],
    )
    assert "error" in out
    assert out["derived"] is True
    assert "available_fiscal_periods" in out


def test_query_financials_unknown_canonical_lists_derived(fs_derived: FinancialsStore) -> None:
    """完全未知字段的 not-found 返回里也应暴露派生字段（hint LLM 知道有这些）。"""
    out = query_financials(
        fs_derived,
        ticker="688981",
        line_item_canonical="完全未知XYZ",
    )
    assert "error" in out
    assert "available_derived" in out
    assert set(out["available_derived"]) == {
        "gross_margin",
        "net_margin",
        "operating_margin",
        "fcf_margin",
        "depreciation_amortization_total",
    }


def test_list_derived_canonicals() -> None:
    """暴露给 pipeline.py 的 helper 应返回全部派生字段名。"""
    from walk_the_talk.verify.tools import list_derived_canonicals

    names = list_derived_canonicals()
    assert set(names) == {
        "gross_margin",
        "net_margin",
        "operating_margin",
        "fcf_margin",
        "depreciation_amortization_total",
    }


# ============== P2 · D&A 派生字段（求和型 + optional_requires） ==============


def _seed_da(tmp_path: Path, components: dict[str, dict[int, float]]) -> FinancialsStore:
    """便捷塞 D&A 各分量数据；components 形如 {canonical: {year: value}}。"""
    store = FinancialsStore(tmp_path / "fin.db")
    lines = []
    for canonical, by_year in components.items():
        for y, v in by_year.items():
            lines.append(
                FinancialLine(
                    ticker="688981",
                    fiscal_period=f"FY{y}",
                    statement_type=StatementType.CASHFLOW,
                    line_item=canonical,
                    line_item_canonical=canonical,
                    value=v,
                )
            )
    store.upsert_lines(lines)
    return store


def test_query_financials_derived_da_total_full_components(tmp_path: Path) -> None:
    """5 个 D&A 分量都有 → 合计 = 简单求和。"""
    store = _seed_da(
        tmp_path,
        {
            "depreciation": {2024: 6.0e9},
            "depreciation_right_of_use": {2024: 8.0e8},
            "depreciation_investment_property": {2024: 2.0e8},
            "amortization_intangible": {2024: 3.0e8},
            "amortization_long_term_prepaid": {2024: 1.0e8},
        },
    )
    try:
        out = query_financials(
            store,
            ticker="688981",
            line_item_canonical="depreciation_amortization_total",
        )
        assert "error" not in out
        assert out["derived"] is True
        assert out["unit"] == "元"
        assert out["requires"] == []
        assert out["optional_requires"] == [
            "depreciation",
            "depreciation_right_of_use",
            "depreciation_investment_property",
            "amortization_intangible",
            "amortization_long_term_prepaid",
        ]
        # 合计 = 6.0e9 + 0.8e9 + 0.2e9 + 0.3e9 + 0.1e9 = 7.4e9
        assert abs(out["values"]["FY2024"] - 7.4e9) < 1e-3
    finally:
        store.close()


def test_query_financials_derived_da_total_partial_components(tmp_path: Path) -> None:
    """只有部分分量（depreciation + amortization_intangible）→ 合计 = 已有的求和，缺失项视为 0。"""
    store = _seed_da(
        tmp_path,
        {
            "depreciation": {2023: 5.0e9, 2024: 6.0e9, 2025: 7.0e9},
            "amortization_intangible": {2024: 3.0e8, 2025: 4.0e8},
        },
    )
    try:
        out = query_financials(
            store,
            ticker="688981",
            line_item_canonical="depreciation_amortization_total",
        )
        assert "error" not in out
        # FY2023 只有 depreciation
        assert abs(out["values"]["FY2023"] - 5.0e9) < 1e-3
        # FY2024 有两项
        assert abs(out["values"]["FY2024"] - (6.0e9 + 3.0e8)) < 1e-3
        # FY2025 有两项
        assert abs(out["values"]["FY2025"] - (7.0e9 + 4.0e8)) < 1e-3
    finally:
        store.close()


def test_query_financials_derived_da_total_all_optional_missing(tmp_path: Path) -> None:
    """所有可选依赖都不在 store 里 → 整 recipe 不可用，返回 error 但带 derived/description。"""
    store = FinancialsStore(tmp_path / "fin.db")
    # 仅塞 revenue（无关分量）
    store.upsert_lines(
        [
            FinancialLine(
                ticker="688981",
                fiscal_period="FY2024",
                statement_type=StatementType.INCOME,
                line_item="revenue",
                line_item_canonical="revenue",
                value=1.0e10,
            )
        ]
    )
    try:
        out = query_financials(
            store,
            ticker="688981",
            line_item_canonical="depreciation_amortization_total",
        )
        assert "error" in out
        assert "none of optional_requires" in out["error"]
        assert out["derived"] is True
        assert out["requires"] == []
        assert "depreciation" in out["optional_requires"]
    finally:
        store.close()


def test_query_financials_derived_da_total_single_component(tmp_path: Path) -> None:
    """只有 1 个分量在 store 里 → 仍能给出合计（=该分量值）。"""
    store = _seed_da(
        tmp_path,
        {
            "depreciation": {2024: 6.0e9, 2025: 7.0e9},
        },
    )
    try:
        out = query_financials(
            store,
            ticker="688981",
            line_item_canonical="depreciation_amortization_total",
        )
        assert "error" not in out
        assert abs(out["values"]["FY2024"] - 6.0e9) < 1e-3
        assert abs(out["values"]["FY2025"] - 7.0e9) < 1e-3
    finally:
        store.close()


def test_query_financials_derived_da_total_period_filter(tmp_path: Path) -> None:
    """fiscal_periods 过滤对求和派生字段同样生效。"""
    store = _seed_da(
        tmp_path,
        {
            "depreciation": {2023: 5.0e9, 2024: 6.0e9, 2025: 7.0e9},
            "amortization_intangible": {2023: 2.0e8, 2024: 3.0e8, 2025: 4.0e8},
        },
    )
    try:
        out = query_financials(
            store,
            ticker="688981",
            line_item_canonical="depreciation_amortization_total",
            fiscal_periods=["FY2024"],
        )
        assert set(out["values"].keys()) == {"FY2024"}
        assert abs(out["values"]["FY2024"] - (6.0e9 + 3.0e8)) < 1e-3
    finally:
        store.close()


def test_sum_optional_components_helper() -> None:
    """直接验证 _sum_optional_components 的行为。"""
    from walk_the_talk.verify.tools import _sum_optional_components

    assert _sum_optional_components({"a": 1.0, "b": 2.0, "c": 3.0}) == 6.0
    assert _sum_optional_components({"a": 1.0, "b": None, "c": 3.0}) == 4.0
    assert _sum_optional_components({"a": None, "b": None, "c": None}) is None
    assert _sum_optional_components({}) is None


# ============== P2 · 折旧/摊销 taxonomy alias ==============


def test_taxonomy_lookup_da_aliases() -> None:
    """新增的折旧/摊销 alias 都能映射到对应 canonical。"""
    from walk_the_talk.core.enums import StatementType
    from walk_the_talk.ingest.taxonomy import lookup_canonical

    # 主用 depreciation
    assert (
        lookup_canonical(
            "固定资产折旧、油气资产折耗、生产性生物资产折旧",
            StatementType.CASHFLOW,
        )
        == "depreciation"
    )
    assert lookup_canonical("固定资产折旧", StatementType.CASHFLOW) == "depreciation"

    # 使用权资产折旧 / 摊销（中芯沿用旧实操术语用"摊销"，会计含义等同折旧）
    assert lookup_canonical("使用权资产折旧", StatementType.CASHFLOW) == "depreciation_right_of_use"
    assert lookup_canonical("使用权资产摊销", StatementType.CASHFLOW) == "depreciation_right_of_use"

    # 投资性房地产折旧
    assert lookup_canonical("投资性房地产折旧", StatementType.CASHFLOW) == "depreciation_investment_property"

    # 无形资产摊销（带/不带"的"）
    assert lookup_canonical("无形资产摊销", StatementType.CASHFLOW) == "amortization_intangible"
    assert lookup_canonical("无形资产的摊销", StatementType.CASHFLOW) == "amortization_intangible"

    # 长期待摊费用摊销（带/不带"的"）
    assert lookup_canonical("长期待摊费用摊销", StatementType.CASHFLOW) == "amortization_long_term_prepaid"
    assert lookup_canonical("长期待摊费用的摊销", StatementType.CASHFLOW) == "amortization_long_term_prepaid"


def test_taxonomy_lookup_da_with_numeral_prefix() -> None:
    """带罗马数字 / 阿拉伯数字前缀 normalize 后仍能命中。"""
    from walk_the_talk.core.enums import StatementType
    from walk_the_talk.ingest.taxonomy import lookup_canonical

    # "一、固定资产折旧" → 归一化后 "固定资产折旧"
    assert lookup_canonical("一、固定资产折旧", StatementType.CASHFLOW) == "depreciation"
    # "1、无形资产摊销"
    assert lookup_canonical("1、无形资产摊销", StatementType.CASHFLOW) == "amortization_intangible"


# ============== _suggest_alias ==============


def test_suggest_alias_substring_match() -> None:
    assert _suggest_alias("capex_yoy", ["revenue", "capex", "depreciation"]) == "did you mean 'capex'?"


def test_suggest_alias_difflib_fallback() -> None:
    # "depreciaton" 和 "depreciation" 编辑距离接近
    out = _suggest_alias("depreciaton", ["revenue", "capex", "depreciation"])
    assert out == "did you mean 'depreciation'?"


def test_suggest_alias_no_match() -> None:
    assert _suggest_alias("foobar", ["revenue", "capex"]) is None


def test_suggest_alias_empty_candidates() -> None:
    assert _suggest_alias("revenue", []) is None


# ============== query_chunks (with stub store) ==============


class _StubReportsStore:
    """模拟 ReportsStore 的 query_hybrid + get_texts，零 chromadb 依赖。"""

    def __init__(self, hits_by_query: dict[str, list[tuple[str, float, dict]]], texts: dict[str, str]):
        self._hits_by_query = hits_by_query
        self._texts = texts
        self.last_call: dict[str, Any] = {}

    def query_hybrid(
        self,
        text: str,
        k: int = 10,
        where: dict[str, Any] | None = None,
        alpha: float = 0.5,
    ) -> list[tuple[str, float, dict]]:
        self.last_call = {"text": text, "k": k, "where": where, "alpha": alpha}
        hits = self._hits_by_query.get(text, [])
        # 模拟 where 过滤（只支持 fiscal_period $in）
        if where and "fiscal_period" in where:
            allowed = where["fiscal_period"].get("$in", [])
            hits = [h for h in hits if h[2].get("fiscal_period") in allowed]
        return hits[:k]

    def get_texts(self, ids: list[str]) -> dict[str, str]:
        return {cid: self._texts.get(cid, "") for cid in ids}


def _stub_store_with_smic_data() -> _StubReportsStore:
    hits = {
        "capex 持平": [
            (
                "688981-FY2025-sec03-p012",
                0.95,
                {
                    "fiscal_period": "FY2025",
                    "section": "管理层讨论",
                    "section_canonical": "mda",
                    "locator": "管理层讨论#3",
                    "source_path": "/data/2025.html",
                },
            ),
            (
                "688981-FY2024-sec03-p008",
                0.80,
                {
                    "fiscal_period": "FY2024",
                    "section": "管理层讨论",
                    "section_canonical": "mda",
                    "locator": "管理层讨论#2",
                    "source_path": "/data/2024.html",
                },
            ),
            (
                "688981-FY2023-sec02-p005",
                0.50,
                {
                    "fiscal_period": "FY2023",
                    "section": "致股东的信",
                    "section_canonical": "mgmt_letter",
                    "locator": "致股东的信#1",
                    "source_path": "/data/2023.html",
                },
            ),
        ],
    }
    texts = {
        "688981-FY2025-sec03-p012": "公司2025年资本开支约为73亿美元，与2024年的75亿美元基本持平…" * 5,
        "688981-FY2024-sec03-p008": "2024年资本开支为75亿美元。",
        "688981-FY2023-sec02-p005": "短文本",
    }
    return _StubReportsStore(hits, texts)


def test_query_chunks_no_filter() -> None:
    """无 filter → 全部 hit 被返回。"""
    store = _stub_store_with_smic_data()
    out = query_chunks(store, query="capex 持平", top_k=3)
    assert len(out) == 3
    assert out[0]["chunk_id"] == "688981-FY2025-sec03-p012"
    assert out[0]["score"] == 0.95
    assert out[0]["fiscal_period"] == "FY2025"
    assert "持平" in out[0]["text"]
    assert store.last_call["where"] is None


def test_query_chunks_after_fiscal_year_filter() -> None:
    """after_fiscal_year=2024 → 只取 FY2025+。"""
    store = _stub_store_with_smic_data()
    out = query_chunks(store, query="capex 持平", after_fiscal_year=2024, top_k=10)
    assert len(out) == 1
    assert out[0]["fiscal_period"] == "FY2025"
    # where 应该是 $in {FY2025..FY2029}
    where = store.last_call["where"]
    assert "FY2025" in where["fiscal_period"]["$in"]
    assert "FY2024" not in where["fiscal_period"]["$in"]


def test_query_chunks_explicit_periods_filter() -> None:
    """fiscal_periods 显式列表 → 只命中该年。"""
    store = _stub_store_with_smic_data()
    out = query_chunks(
        store,
        query="capex 持平",
        fiscal_periods=["FY2024"],
        top_k=10,
    )
    assert len(out) == 1
    assert out[0]["fiscal_period"] == "FY2024"


def test_query_chunks_top_k_limit() -> None:
    store = _stub_store_with_smic_data()
    out = query_chunks(store, query="capex 持平", top_k=2)
    assert len(out) == 2


def test_query_chunks_empty_hits() -> None:
    store = _stub_store_with_smic_data()
    out = query_chunks(store, query="不存在的 query", top_k=5)
    assert out == []


def test_query_chunks_snippet_truncation() -> None:
    """长文本应被截到 snippet_chars 并加 …"""
    store = _stub_store_with_smic_data()
    out = query_chunks(store, query="capex 持平", top_k=1, snippet_chars=20)
    assert len(out[0]["text"]) <= 20 + 1  # +1 是省略号
    assert out[0]["text"].endswith("…")


def test_query_chunks_short_text_no_truncation() -> None:
    """短文本不应被加省略号。"""
    store = _stub_store_with_smic_data()
    out = query_chunks(
        store,
        query="capex 持平",
        fiscal_periods=["FY2023"],
        top_k=1,
        snippet_chars=400,
    )
    assert out[0]["text"] == "短文本"
    assert not out[0]["text"].endswith("…")


# ============== _build_where ==============


def test_build_where_none() -> None:
    assert _build_where(after_fiscal_year=None, fiscal_periods=None) is None


def test_build_where_explicit_periods() -> None:
    out = _build_where(after_fiscal_year=None, fiscal_periods=["FY2024", "FY2025"])
    assert out == {"fiscal_period": {"$in": ["FY2024", "FY2025"]}}


def test_build_where_after_year_expands_to_5_years() -> None:
    out = _build_where(after_fiscal_year=2024, fiscal_periods=None)
    assert out is not None
    periods = out["fiscal_period"]["$in"]
    assert periods == ["FY2025", "FY2026", "FY2027", "FY2028", "FY2029"]


def test_build_where_explicit_overrides_after_year() -> None:
    """同时给 → fiscal_periods 优先。"""
    out = _build_where(after_fiscal_year=2024, fiscal_periods=["FY2024"])
    assert out == {"fiscal_period": {"$in": ["FY2024"]}}
