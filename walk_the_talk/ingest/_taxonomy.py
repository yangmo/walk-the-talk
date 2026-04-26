"""三大表 line item 同义词表 + 文本/数值归一化。

设计：
- 同义词表只覆盖"对 verifier 有用的 line item"。SMIC 一张资产负债表有 80+ 行，
  绝大多数对验证管理层 claim 没用（如"应收保费"），不必都入库。
- 归一化 line item 时先清洗（去序号前缀、去说明括号），再 lookup。
- 没匹配到的行会被 store 跳过（不报错，仅 logger.debug）。
"""

from __future__ import annotations

import re

from ..core.enums import StatementType

# ============== Line item 同义词表 ==============

BALANCE_LINES: dict[str, str] = {
    # 流动资产
    "货币资金": "cash",
    "交易性金融资产": "trading_financial_assets",
    "应收票据": "notes_receivable",
    "应收账款": "accounts_receivable",
    "预付款项": "prepayments",
    "其他应收款": "other_receivables",
    "存货": "inventory",
    "合同资产": "contract_assets",
    "一年内到期的非流动资产": "current_portion_non_current_assets",
    "其他流动资产": "other_current_assets",
    "流动资产合计": "total_current_assets",
    # 非流动资产
    "长期股权投资": "long_term_equity_investment",
    "其他权益工具投资": "other_equity_investment",
    "其他非流动金融资产": "other_non_current_financial_assets",
    "投资性房地产": "investment_property",
    "固定资产": "fixed_assets",
    "在建工程": "construction_in_progress",
    "使用权资产": "right_of_use_assets",
    "无形资产": "intangible_assets",
    "开发支出": "development_expenditure",
    "商誉": "goodwill",
    "长期待摊费用": "long_term_deferred_expenses",
    "递延所得税资产": "deferred_tax_assets",
    "其他非流动资产": "other_non_current_assets",
    "非流动资产合计": "total_non_current_assets",
    "资产总计": "total_assets",
    # 流动负债
    "短期借款": "short_term_loans",
    "交易性金融负债": "trading_financial_liabilities",
    "应付票据": "notes_payable",
    "应付账款": "accounts_payable",
    "预收款项": "advances_from_customers",
    "合同负债": "contract_liabilities",
    "应付职工薪酬": "employee_compensation_payable",
    "应交税费": "taxes_payable",
    "其他应付款": "other_payables",
    "一年内到期的非流动负债": "current_portion_non_current_liabilities",
    "其他流动负债": "other_current_liabilities",
    "流动负债合计": "total_current_liabilities",
    # 非流动负债
    "长期借款": "long_term_loans",
    "应付债券": "bonds_payable",
    "租赁负债": "lease_liabilities",
    "长期应付款": "long_term_payables",
    "预计负债": "estimated_liabilities",
    "递延收益": "deferred_income",
    "递延所得税负债": "deferred_tax_liabilities",
    "其他非流动负债": "other_non_current_liabilities",
    "非流动负债合计": "total_non_current_liabilities",
    "负债合计": "total_liabilities",
    # 所有者权益
    "实收资本": "paid_in_capital",
    "实收资本(或股本)": "paid_in_capital",
    "实收资本（或股本）": "paid_in_capital",
    "股本": "paid_in_capital",
    "其他权益工具": "other_equity_instruments",
    "资本公积": "capital_reserve",
    "库存股": "treasury_stock",
    "其他综合收益": "other_comprehensive_income",
    "专项储备": "special_reserve",
    "盈余公积": "surplus_reserve",
    "一般风险准备": "general_risk_reserve",
    "未分配利润": "retained_earnings",
    "归属于母公司所有者权益(或股东权益)合计": "equity_attributable_to_parent",
    "归属于母公司所有者权益（或股东权益）合计": "equity_attributable_to_parent",
    "少数股东权益": "minority_interest",
    "所有者权益(或股东权益)合计": "total_equity",
    "所有者权益（或股东权益）合计": "total_equity",
    "负债和所有者权益(或股东权益)总计": "total_liabilities_and_equity",
    "负债和所有者权益（或股东权益）总计": "total_liabilities_and_equity",
}

INCOME_LINES: dict[str, str] = {
    "营业总收入": "total_revenue",
    "营业收入": "revenue",
    "营业总成本": "total_operating_cost",
    "营业成本": "cost_of_revenue",
    "税金及附加": "taxes_and_surcharges",
    "销售费用": "selling_expense",
    "管理费用": "ga_expense",
    "研发费用": "rd_expense",
    "财务费用": "financial_expense",
    "利息费用": "interest_expense",
    "利息收入": "interest_income",
    "其他收益": "other_income",
    "投资收益": "investment_income",
    "汇兑收益": "fx_gain",
    "公允价值变动收益": "fair_value_change_gain",
    "信用减值损失": "credit_impairment_loss",
    "资产减值损失": "asset_impairment_loss",
    "资产处置收益": "asset_disposal_gain",
    "营业利润": "operating_profit",
    "营业外收入": "non_operating_income",
    "营业外支出": "non_operating_expense",
    "利润总额": "profit_before_tax",
    "所得税费用": "income_tax_expense",
    "净利润": "net_profit",
    "持续经营净利润": "continuing_net_profit",
    "终止经营净利润": "discontinued_net_profit",
    "归属于母公司股东的净利润": "net_profit_attributable_to_parent",
    "少数股东损益": "minority_interest_pnl",
    "其他综合收益的税后净额": "other_comprehensive_income_after_tax",
    "归属母公司所有者的其他综合收益的税后净额": "oci_attributable_to_parent",
    "归属于少数股东的其他综合收益的税后净额": "oci_attributable_to_minority",
    "综合收益总额": "total_comprehensive_income",
    "归属于母公司所有者的综合收益总额": "comprehensive_income_attributable_to_parent",
    "归属于少数股东的综合收益总额": "comprehensive_income_attributable_to_minority",
    "基本每股收益": "basic_eps",
    "稀释每股收益": "diluted_eps",
}

CASHFLOW_LINES: dict[str, str] = {
    "销售商品、提供劳务收到的现金": "cash_from_sales",
    "收到的税费返还": "tax_refund_received",
    "收到其他与经营活动有关的现金": "other_operating_cash_in",
    "经营活动现金流入小计": "operating_cash_in_total",
    "购买商品、接受劳务支付的现金": "cash_paid_for_goods",
    "支付给职工及为职工支付的现金": "cash_paid_to_employees",
    "支付的各项税费": "tax_paid",
    "支付其他与经营活动有关的现金": "other_operating_cash_out",
    "经营活动现金流出小计": "operating_cash_out_total",
    "经营活动产生的现金流量净额": "ocf",
    "收回投资收到的现金": "cash_from_investment_recovery",
    "取得投资收益收到的现金": "cash_from_investment_income",
    "处置固定资产、无形资产和其他长期资产收回的现金净额": "cash_from_asset_disposal",
    "收到其他与投资活动有关的现金": "other_investing_cash_in",
    "投资活动现金流入小计": "investing_cash_in_total",
    "购建固定资产、无形资产和其他长期资产支付的现金": "capex",
    "投资支付的现金": "investment_paid",
    "支付其他与投资活动有关的现金": "other_investing_cash_out",
    "投资活动现金流出小计": "investing_cash_out_total",
    "投资活动产生的现金流量净额": "icf",
    "吸收投资收到的现金": "cash_from_capital_raised",
    "取得借款收到的现金": "cash_from_borrowings",
    "收到其他与筹资活动有关的现金": "other_financing_cash_in",
    "筹资活动现金流入小计": "financing_cash_in_total",
    "偿还债务支付的现金": "cash_paid_for_debt",
    "分配股利、利润或偿付利息支付的现金": "cash_paid_for_dividends_interest",
    "支付其他与筹资活动有关的现金": "other_financing_cash_out",
    "筹资活动现金流出小计": "financing_cash_out_total",
    "筹资活动产生的现金流量净额": "fcf",
    "汇率变动对现金及现金等价物的影响": "fx_effect_on_cash",
    "现金及现金等价物净增加额": "net_change_in_cash",
    "期初现金及现金等价物余额": "cash_at_beginning",
    "期末现金及现金等价物余额": "cash_at_end",
    # === 现金流量表补充资料：折旧与摊销 ===
    # 这些一般出现在"将净利润调节为经营活动现金流量"补充表里。
    # 抓进 DB 后，verify 阶段可以校验「折旧/摊销 在 X 区间」类 claim，
    # 同时 P1 派生字段 depreciation_amortization_total 可基于这几条求和。
    "固定资产折旧、油气资产折耗、生产性生物资产折旧": "depreciation",
    "固定资产折旧": "depreciation",
    "使用权资产折旧": "depreciation_right_of_use",
    # 部分公司（如中芯国际）沿用 IFRS 16 之前的旧实操术语，
    # 在 cashflow 补充资料里把使用权资产按"摊销"处理。会计含义等同折旧。
    "使用权资产摊销": "depreciation_right_of_use",
    "投资性房地产折旧": "depreciation_investment_property",
    "无形资产摊销": "amortization_intangible",
    "无形资产的摊销": "amortization_intangible",
    "长期待摊费用摊销": "amortization_long_term_prepaid",
    "长期待摊费用的摊销": "amortization_long_term_prepaid",
}

TAXONOMY: dict[StatementType, dict[str, str]] = {
    StatementType.BALANCE: BALANCE_LINES,
    StatementType.INCOME: INCOME_LINES,
    StatementType.CASHFLOW: CASHFLOW_LINES,
}

# ============== Line item 文本归一化 ==============

# 罗马数字前缀，如 "一、营业总收入" → "营业总收入"
_PREFIX_NUMERAL = re.compile(r"^[一二三四五六七八九十]+[、\.]\s*")
# 中文括号编号 "（一）按..." → "按..."
_PREFIX_BRACKET = re.compile(r"^[（(][一二三四五六七八九十0-9]+[)）]\s*")
# 阿拉伯数字编号 "1.持续经营净利润" → "持续经营净利润"
_PREFIX_ARABIC = re.compile(r"^[0-9]+[\.．、]\s*")
# "其中：" 前缀 — 子项标记，留 sub_of 信息但归一化时去掉
_PREFIX_OF_WHICH = re.compile(r"^其中[：:]\s*")
# 末尾说明括号 "（亏损以"－"号填列）" 等
_TAIL_NOTE = re.compile(r"[（(][^()（）]*?(亏损|损失|净亏损|填列|元/股)[^()（）]*?[)）]")
# 多余空白/全角空格
_WHITESPACE = re.compile(r"[\s\u3000]+")


def normalize_line_item_text(raw: str) -> tuple[str, bool]:
    """清洗 line item 原文，返回 (cleaned, is_sub_item)。

    is_sub_item 表示原文带"其中："，下游可决定是否合并/丢弃。
    """
    if not raw:
        return "", False
    s = raw.strip()
    is_sub = False
    if _PREFIX_OF_WHICH.match(s):
        is_sub = True
        s = _PREFIX_OF_WHICH.sub("", s)
    s = _PREFIX_NUMERAL.sub("", s)
    s = _PREFIX_BRACKET.sub("", s)
    s = _PREFIX_ARABIC.sub("", s)
    s = _TAIL_NOTE.sub("", s)
    s = _WHITESPACE.sub("", s)
    return s.strip(), is_sub


def lookup_canonical(line_item_text: str, statement_type: StatementType) -> str | None:
    """归一化后的文本 → canonical 名。未命中返回 None。"""
    cleaned, _ = normalize_line_item_text(line_item_text)
    if not cleaned:
        return None
    table = TAXONOMY.get(statement_type, {})
    return table.get(cleaned)


# ============== 数值解析 ==============

# 中文负号/会计括号
_NEG_PREFIXES = ("-", "－", "−")
_PAREN_NEG = re.compile(r"^[（(]\s*([\d,，.]+)\s*[)）]$")
_NUMERIC_OK = re.compile(r"^-?[\d,，.]+$")

UNIT_MULTIPLIER = {
    "元": 1.0,
    "千元": 1_000.0,
    "万元": 10_000.0,
    "百万元": 1_000_000.0,
    "亿元": 100_000_000.0,
}


def parse_unit_from_caption(caption: str) -> tuple[str, float]:
    """从 caption / 表头里抓"单位：千元 / 单位：百万元"。

    返回 (unit_label, multiplier_to_yuan)。默认元。
    """
    if not caption:
        return "元", 1.0
    for label, mult in UNIT_MULTIPLIER.items():
        if f"单位：{label}" in caption or f"单位:{label}" in caption:
            return label, mult
    return "元", 1.0


def parse_numeric(raw: str) -> float | None:
    """解析单元格里的数字。

    支持：
    - 千分位逗号（半角/全角）
    - 全角小数点
    - 会计括号 (1,234) → -1234
    - 中文负号 - / － / −
    - "-" "—" → None（未披露/不适用）

    无法解析返回 None。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ("-", "—", "－", "/", "–"):
        return None
    s = s.replace("，", ",").replace("．", ".").replace(" ", "")

    # 会计括号负数
    m = _PAREN_NEG.match(s)
    if m:
        inner = m.group(1).replace(",", "")
        try:
            return -float(inner)
        except ValueError:
            return None

    # 中文负号统一
    sign = 1.0
    for p in _NEG_PREFIXES:
        if s.startswith(p):
            sign = -1.0
            s = s[len(p) :]
            break

    if not _NUMERIC_OK.match(s):
        return None
    s = s.replace(",", "")
    try:
        return sign * float(s)
    except ValueError:
        return None
