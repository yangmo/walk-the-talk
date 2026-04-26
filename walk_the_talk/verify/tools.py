"""Phase 3 verifier agent 的三个原子工具。

设计原则：纯函数 + 关键字入参 + dict 出参；step 3 LangGraph 节点会做 JSON 适配。

工具列表：
    compute(expr)              ：安全表达式求值，消除 LLM 算术幻觉
    query_financials(...)      ：从 financials.db 查 canonical line_item 的时间序列
    query_chunks(...)          ：在 reports_store 里 BM25+向量混搜原文证据

为什么用 dict 出参而不是 Pydantic 模型：
- 工具产物会进 ToolCall.result（已是 Any），不强求模型化
- 留余地给 agent 自己解析 / LLM 直接读 JSON
- 错误以 {"error": "..."} 表达，与正常返回保持同构（agent 简单分支判断）

派生字段（P1）：
    query_financials 在基础字段 miss 后，若请求的 canonical 在 _DERIVED_RECIPES 里，
    会自动从基础字段实时计算（不入库，保持 financials.db 单一事实源）。
    白名单注入由 verify/pipeline.py 拼上 _DERIVED_RECIPES.keys() 完成，
    LLM 看到的"白名单"=DB 直查 ∪ 派生可算。
"""

from __future__ import annotations

import ast
import difflib
import operator as op
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..ingest.financials_store import FinancialsStore

# ============== compute ==============

# 白名单：算术 / 比较 / 布尔 / 内置 abs/min/max。其余 AST 节点一律拒绝。
_BIN_OPS: dict[type, Any] = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Mod: op.mod,
    ast.Pow: op.pow,
    ast.FloorDiv: op.floordiv,
}
_UNARY_OPS: dict[type, Any] = {
    ast.USub: op.neg,
    ast.UAdd: op.pos,
    ast.Not: op.not_,
}
_COMPARE_OPS: dict[type, Any] = {
    ast.Lt: op.lt,
    ast.LtE: op.le,
    ast.Gt: op.gt,
    ast.GtE: op.ge,
    ast.Eq: op.eq,
    ast.NotEq: op.ne,
}
_BOOL_OPS: dict[type, Any] = {
    ast.And: lambda xs: all(xs),
    ast.Or: lambda xs: any(xs),
}
_FUNCS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
}


class ComputeError(ValueError):
    """compute 表达式语义/语法错误（区别于 ZeroDivision 等运行时错误）。"""


def compute(expr: str) -> dict[str, Any]:
    """安全求值 expr（白名单 AST + 不允许 import / Name / Attribute / Subscript / Call 之外的危险节点）。

    Returns:
        命中：{"expr": "...", "value": <float|int|bool>}
        失败：{"expr": "...", "error": "..."}

    例：
        compute("(57796 - 45525) / 45525 >= 0.30") → {"expr": "...", "value": True}
        compute("__import__('os').system('rm -rf /')") → {"expr": "...", "error": "function not allowed: __import__"}
    """

    cleaned = (expr or "").strip()
    if not cleaned:
        return {"expr": expr, "error": "empty expression"}

    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as e:
        return {"expr": expr, "error": f"syntax error: {e.msg}"}

    try:
        value = _safe_eval(tree.body)
    except ZeroDivisionError:
        return {"expr": expr, "error": "division by zero"}
    except (ComputeError, ValueError, TypeError, OverflowError) as e:
        return {"expr": expr, "error": str(e)}

    # 数值规整：超长浮点截 12 位有效数字（避免 0.30000000000000004 这种）
    if isinstance(value, float) and not isinstance(value, bool):
        value = round(value, 12)
    return {"expr": expr, "value": value}


def _safe_eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, bool)):
            raise ComputeError(f"unsupported constant type: {type(node.value).__name__}")
        return node.value

    if isinstance(node, ast.BinOp):
        fn = _BIN_OPS.get(type(node.op))
        if fn is None:
            raise ComputeError(f"unsupported binop: {type(node.op).__name__}")
        return fn(_safe_eval(node.left), _safe_eval(node.right))

    if isinstance(node, ast.UnaryOp):
        fn = _UNARY_OPS.get(type(node.op))
        if fn is None:
            raise ComputeError(f"unsupported unary op: {type(node.op).__name__}")
        return fn(_safe_eval(node.operand))

    if isinstance(node, ast.Compare):
        # Python 链式比较语义：a < b < c → a<b and b<c
        left = _safe_eval(node.left)
        for op_node, comparator in zip(node.ops, node.comparators, strict=True):
            fn = _COMPARE_OPS.get(type(op_node))
            if fn is None:
                raise ComputeError(f"unsupported compare op: {type(op_node).__name__}")
            right = _safe_eval(comparator)
            if not fn(left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.BoolOp):
        fn = _BOOL_OPS.get(type(node.op))
        if fn is None:
            raise ComputeError(f"unsupported bool op: {type(node.op).__name__}")
        return fn(_safe_eval(v) for v in node.values)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ComputeError("only direct function calls allowed (no attribute access)")
        if node.func.id not in _FUNCS:
            raise ComputeError(f"function not allowed: {node.func.id}")
        if node.keywords:
            raise ComputeError("keyword arguments not allowed")
        return _FUNCS[node.func.id](*[_safe_eval(a) for a in node.args])

    raise ComputeError(f"unsupported node: {type(node).__name__}")


# ============== query_financials + 派生字段 (P1) ==============


# 派生字段 recipe：所有 recipe 都按"同一 fiscal_period 内的几个基础 canonical 算一个比率"的形态。
# - requires：必需的 canonical 列表（用于显示给 LLM "派生需要哪些基础字段"，以及缺失检测）
# - compute：拿到 {dep_canonical: value} 后返回派生值；分母为 0 / 缺值时返回 None
#
# 为什么用 lambda 而不是表达式字符串：
# - 表达式字符串走 compute() 也行，但每个 recipe 还要写一次解析逻辑，啰嗦
# - lambda 写起来一行，缺值返回 None 的语义最直接
@dataclass(frozen=True)
class _DerivedRecipe:
    """派生字段配方。

    requires：强制依赖，store 里完全没这条 canonical → recipe 不可用 → 返回 error。
    optional_requires：可选依赖（用于 D&A 这类"有几条算几条"的求和场景）。store 里
        某条 canonical 不存在则该项视为缺失；compute 拿到的 dict 里该 key 对应 None。
        逐 fy 计算时具体某个 fy 的某条可选依赖缺失也按 None 传给 compute。
    optional_requires 全部缺失（且没有 requires）→ 视为 recipe 整体不可用。
    """

    name: str
    requires: tuple[str, ...]
    compute: Callable[[dict[str, float | None]], float | None]
    unit: str
    description: str
    optional_requires: tuple[str, ...] = ()


def _safe_div(num: float | None, denom: float | None) -> float | None:
    """num/denom；任一缺失或 denom==0 → None。"""
    if num is None or denom is None or denom == 0:
        return None
    return num / denom


def _sum_optional_components(values: dict[str, float | None]) -> float | None:
    """将 dict 中所有非 None 数值加起来；全 None 返回 None。

    用于 D&A 合计这类"有几项算几项"的派生字段。
    """
    nums = [v for v in values.values() if v is not None]
    if not nums:
        return None
    return sum(nums)


_DERIVED_RECIPES: dict[str, _DerivedRecipe] = {
    "gross_margin": _DerivedRecipe(
        name="gross_margin",
        requires=("revenue", "cost_of_revenue"),
        compute=lambda v: _safe_div(
            (v["revenue"] - v["cost_of_revenue"])
            if v["revenue"] is not None and v["cost_of_revenue"] is not None
            else None,
            v["revenue"],
        ),
        unit="ratio",
        description="毛利率 = (营业收入 - 营业成本) / 营业收入",
    ),
    "net_margin": _DerivedRecipe(
        name="net_margin",
        requires=("revenue", "net_profit"),
        compute=lambda v: _safe_div(v["net_profit"], v["revenue"]),
        unit="ratio",
        description="净利率 = 净利润 / 营业收入",
    ),
    "operating_margin": _DerivedRecipe(
        name="operating_margin",
        requires=("revenue", "operating_profit"),
        compute=lambda v: _safe_div(v["operating_profit"], v["revenue"]),
        unit="ratio",
        description="营业利润率 = 营业利润 / 营业收入",
    ),
    "fcf_margin": _DerivedRecipe(
        name="fcf_margin",
        requires=("revenue", "ocf", "capex"),
        # 注意：财务报表里 capex 一般以"购建固定资产支付的现金"形态入库（正数流出），
        # 所以 FCF = OCF - capex（capex 不取绝对值，因为它本就是正数）
        compute=lambda v: _safe_div(
            (v["ocf"] - v["capex"]) if v["ocf"] is not None and v["capex"] is not None else None,
            v["revenue"],
        ),
        unit="ratio",
        description="自由现金流率 = (经营活动现金流 - 资本开支) / 营业收入",
    ),
    "depreciation_amortization_total": _DerivedRecipe(
        name="depreciation_amortization_total",
        # D&A 合计没有强制依赖；全部 5 条都是可选求和
        requires=(),
        optional_requires=(
            "depreciation",
            "depreciation_right_of_use",
            "depreciation_investment_property",
            "amortization_intangible",
            "amortization_long_term_prepaid",
        ),
        compute=_sum_optional_components,
        unit="元",
        description=(
            "折旧与摊销合计 = 固定资产折旧 + 使用权资产折旧 + 投资性房地产折旧 + "
            "无形资产摊销 + 长期待摊费用摊销（缺失项视为 0；至少 1 项有数才输出）"
        ),
    ),
}


def list_derived_canonicals() -> list[str]:
    """暴露给 verify/pipeline.py 拼白名单：派生字段的 canonical 名（不含基础字段）。"""
    return list(_DERIVED_RECIPES.keys())


def _query_derived(
    store: FinancialsStore,
    *,
    ticker: str,
    name: str,
    requested: list[str] | None,
) -> dict[str, Any]:
    """拿到 derived recipe 的所有依赖字段，逐 fiscal_period 算派生值。

    与基础查询返回 dict 同构 + 额外字段：
        "derived": True
        "description": "..."
        "requires": [...]
        "optional_requires": [...]   # 仅在非空时出现

    依赖处理：
    - requires（强制）：store 里完全没这条 canonical → 整 recipe 不可用 → error。
    - optional_requires（可选）：store 里没的视为 None，compute 自行决定语义；
      但若 requires 为空且所有 optional_requires 都没数据，整 recipe 不可用 → error。
    - 单 fy 缺依赖：把 None 传给 compute，由 recipe.compute 决定 None 行为。
    """
    recipe = _DERIVED_RECIPES[name]

    all_deps: tuple[str, ...] = recipe.requires + recipe.optional_requires
    dep_series: dict[str, dict[str, float]] = {}
    for dep in all_deps:
        dep_series[dep] = store.get_series(ticker, dep) or {}

    def _err_dict(msg: str, **extra: Any) -> dict[str, Any]:
        d: dict[str, Any] = {
            "line_item": name,
            "values": {},
            "derived": True,
            "description": recipe.description,
            "requires": list(recipe.requires),
            "error": msg,
        }
        if recipe.optional_requires:
            d["optional_requires"] = list(recipe.optional_requires)
        d.update(extra)
        return d

    # 完全缺失的强制依赖
    missing_strict = [d for d in recipe.requires if not dep_series[d]]
    if missing_strict:
        return _err_dict(
            f"derived '{name}' requires {list(recipe.requires)} but missing "
            f"in financials.db for ticker {ticker}: {missing_strict}"
        )

    # 无强制依赖 + 全部 optional 都缺 → 整体不可用
    if not recipe.requires and recipe.optional_requires:
        optional_avail = [d for d in recipe.optional_requires if dep_series[d]]
        if not optional_avail:
            return _err_dict(
                f"derived '{name}': none of optional_requires "
                f"{list(recipe.optional_requires)} present in financials.db "
                f"for ticker {ticker}"
            )

    # 计算可计算的 fy
    all_fys: set[str] = set()
    for s in dep_series.values():
        all_fys.update(s.keys())
    if requested is not None:
        all_fys = all_fys & set(requested)

    values: dict[str, float] = {}
    for fy in sorted(all_fys):
        v = {dep: dep_series[dep].get(fy) for dep in all_deps}
        result = recipe.compute(v)
        if result is None:
            continue
        # 浮点截 12 位，保持和 compute() 一致
        values[fy] = round(result, 12) if isinstance(result, float) else result

    if not values:
        # 所有 fy 都被缺值/分母 0/requested miss 拒掉
        full_fys: set[str] = set()
        for s in dep_series.values():
            full_fys.update(s.keys())
        return _err_dict(
            f"no '{name}' computable for requested fiscal_periods {requested}; "
            f"available base data: {sorted(full_fys)}",
            available_fiscal_periods=sorted(full_fys),
        )

    out: dict[str, Any] = {
        "line_item": name,
        "values": values,
        "unit": recipe.unit,
        "derived": True,
        "description": recipe.description,
        "requires": list(recipe.requires),
    }
    if recipe.optional_requires:
        out["optional_requires"] = list(recipe.optional_requires)
    return out


def query_financials(
    store: FinancialsStore,
    *,
    ticker: str,
    line_item_canonical: str,
    fiscal_periods: list[str] | None = None,
) -> dict[str, Any]:
    """查 ticker × line_item_canonical 在指定 fiscal_periods 的取值。

    fiscal_periods=None 表示该 line_item 所有出现过的财年。

    line_item_canonical 可以是 financials.db 里的基础 canonical（revenue / capex / ...），
    也可以是 _DERIVED_RECIPES 里的派生字段（gross_margin / net_margin / operating_margin /
    fcf_margin）；后者会从基础字段实时算（不入库），返回 dict 多带 "derived": True 标记。

    Returns:
        基础命中：{
            "line_item": "capex",
            "values": {"FY2024": 7.5e9, "FY2025": 7.3e9},
            "unit": "元"
        }
        派生命中：{
            "line_item": "gross_margin",
            "values": {"FY2024": 0.184, "FY2025": 0.215},
            "unit": "ratio",
            "derived": True,
            "description": "毛利率 = (营业收入 - 营业成本) / 营业收入",
            "requires": ["revenue", "cost_of_revenue"]
        }
        line_item 不存在（且不是派生）：{
            "line_item": "capex_yoy",
            "error": "line_item 'capex_yoy' not found",
            "available_canonicals": [...],
            "hint": "did you mean 'capex'?"   # 可能 None
        }
        line_item 存在但请求的 fiscal_periods 都没数据：{
            "line_item": "capex",
            "values": {},
            "available_fiscal_periods": ["FY2022", "FY2023", "FY2024", "FY2025"],
            "error": "no data for requested fiscal_periods FY2030"
        }
    """

    requested = list(fiscal_periods) if fiscal_periods else None

    # 派生字段优先匹配（避免和基础字段重名时被基础查询挡住，虽然目前没冲突）
    if line_item_canonical in _DERIVED_RECIPES:
        return _query_derived(store, ticker=ticker, name=line_item_canonical, requested=requested)

    series = store.get_series(ticker, line_item_canonical, fiscal_periods=requested)

    if series:
        return {
            "line_item": line_item_canonical,
            "values": series,
            "unit": "元",
        }

    # 0 hit：判断 line_item 不存在 vs fiscal_periods 都没命中
    full_series = store.get_series(ticker, line_item_canonical)
    if full_series:
        # line_item 存在，只是 fiscal_periods 错了
        return {
            "line_item": line_item_canonical,
            "values": {},
            "available_fiscal_periods": list(full_series.keys()),
            "error": (
                f"no data for requested fiscal_periods {requested}; available: {list(full_series.keys())}"
            ),
        }

    # line_item 真不存在：候选里也带上派生字段，方便 LLM 看到"哦还能算这些比率"
    canonicals = store.list_canonicals(ticker)
    candidates = canonicals + list(_DERIVED_RECIPES.keys())
    hint = _suggest_alias(line_item_canonical, candidates)
    return {
        "line_item": line_item_canonical,
        "error": f"line_item '{line_item_canonical}' not found for ticker {ticker}",
        "available_canonicals": canonicals,
        "available_derived": list(_DERIVED_RECIPES.keys()),
        "hint": hint,
    }


def _suggest_alias(target: str, candidates: list[str]) -> str | None:
    """基于编辑距离 + 子串包含给一个候选；找不到返回 None。"""
    if not candidates:
        return None
    # 子串优先（"capex_yoy" → "capex"）
    target_lower = target.lower()
    for c in candidates:
        c_lower = c.lower()
        if c_lower in target_lower or target_lower in c_lower:
            return f"did you mean '{c}'?"
    matches = difflib.get_close_matches(target, candidates, n=1, cutoff=0.6)
    if matches:
        return f"did you mean '{matches[0]}'?"
    return None


# ============== query_chunks ==============


class ChunkSearcher(Protocol):
    """ReportsStore 满足的最小搜索接口；测试时用 stub 替身。"""

    def query_hybrid(
        self,
        text: str,
        k: int = 10,
        where: dict[str, Any] | None = None,
        alpha: float = 0.5,
    ) -> list[tuple[str, float, dict]]: ...

    def get_texts(self, ids: list[str]) -> dict[str, str]: ...


@dataclass(frozen=True)
class _DateRange:
    """fiscal_period 字符串 ($in list) 的构造助手。"""

    after: int | None = None  # 严格大于该年（找后续年份证据时用）
    explicit: list[str] | None = None  # 显式列出


def query_chunks(
    store: ChunkSearcher,
    *,
    query: str,
    after_fiscal_year: int | None = None,
    fiscal_periods: list[str] | None = None,
    top_k: int = 3,
    snippet_chars: int = 400,
    alpha: float = 0.5,
) -> list[dict[str, Any]]:
    """混合检索找原文证据，返回 top_k 个 chunk 摘要。

    after_fiscal_year + fiscal_periods 二选一：
      - after_fiscal_year: 取 (year, current_year+5] 的年份；用于"找后续年份的佐证"
      - fiscal_periods: 显式指定（如 ["FY2024", "FY2025"]）
      - 都不给：全库检索

    Returns:
        [
            {
                "chunk_id": "688981-FY2025-sec03-p012",
                "score": 0.0123,
                "fiscal_period": "FY2025",
                "section": "管理层讨论与分析",
                "section_canonical": "mda",
                "locator": "管理层讨论与分析#3",
                "source_path": "/path/2025.html",
                "text": "<截断到 snippet_chars 的原文片段>"
            }, ...
        ]
        无命中：[]
    """

    where = _build_where(after_fiscal_year=after_fiscal_year, fiscal_periods=fiscal_periods)
    hits = store.query_hybrid(query, k=top_k, where=where, alpha=alpha)
    if not hits:
        return []

    text_map = store.get_texts([cid for cid, _, _ in hits])
    out: list[dict[str, Any]] = []
    for cid, score, meta in hits:
        text = text_map.get(cid, "")
        snippet = text[:snippet_chars]
        if len(text) > snippet_chars:
            snippet += "…"
        out.append(
            {
                "chunk_id": cid,
                "score": round(float(score), 6),
                "fiscal_period": str(meta.get("fiscal_period", "")),
                "section": str(meta.get("section", "")),
                "section_canonical": str(meta.get("section_canonical", "")),
                "locator": str(meta.get("locator", "")),
                "source_path": str(meta.get("source_path", "")),
                "text": snippet,
            }
        )
    return out


# 后续年份 fiscal_period 候选：当年 +5 年（够覆盖任何前瞻 horizon）
_FUTURE_LOOKAHEAD = 5


def _build_where(
    *,
    after_fiscal_year: int | None,
    fiscal_periods: list[str] | None,
) -> dict[str, Any] | None:
    """把过滤条件翻成 ChromaDB where 表达式。

    fiscal_period 是 'FY2024' 字符串，Chroma 不支持字符串字段 $gt，因此用 $in 显式枚举。
    """
    if fiscal_periods:
        return {"fiscal_period": {"$in": list(fiscal_periods)}}
    if after_fiscal_year is not None:
        candidates = [
            f"FY{y}" for y in range(after_fiscal_year + 1, after_fiscal_year + 1 + _FUTURE_LOOKAHEAD)
        ]
        return {"fiscal_period": {"$in": candidates}}
    return None


__all__ = [
    "ChunkSearcher",
    "ComputeError",
    "compute",
    "list_derived_canonicals",
    "query_chunks",
    "query_financials",
]
