"""Phase 2 LLM 提示词。

设计要点：
- 单次调用同时完成 "前瞻判定 + 抽取"：没有任何前瞻断言时返回 {"claims": []}。
- 强约束 JSON 输出（DeepSeek 支持 response_format=json_object）。
- claim_type 只能是 5 类前瞻枚举之一。
- 给 hedging 词典帮助模型识别软承诺。
- 含正反例：典型前瞻 vs 当期事实陈述（最易混淆的 false positive）。

外层调用方需要在 messages 里再补一个 user message 给 chunk 文本和元数据；
本模块只提供 system prompt + few-shot example user/assistant 对。
"""

from __future__ import annotations

# ==========================================================
# 1. Hedging 词典（来自年报"致股东的信"管理层惯用前瞻措辞）
# ==========================================================
HEDGING_WORDS: list[str] = [
    "相信",
    "预期",
    "打算",
    "估计",
    "预计",
    "预测",
    "指标",
    "展望",
    "继续",
    "应该",
    "或许",
    "寻求",
    "应当",
    "计划",
    "可能",
    "愿景",
    "目标",
    "旨在",
    "渴望",
    "目的",
    "预定",
    "前景",
    # 常见同义补充
    "力争",
    "争取",
    "致力于",
    "拟",
    "将",
    "未来",
    "下一步",
    "中长期",
    "长期",
    "持续",
    "推动",
    "加快",
    "加大",
    "聚焦",
]


# ==========================================================
# 2. 主 System Prompt
# ==========================================================
SYSTEM_PROMPT = """你是一位严格、谨慎的中文财务文本结构化抽取专家。

# 任务

从给定的上市公司年报文本片段中，**只抽取"管理层对未来的前瞻性断言"**，输出严格符合 schema 的 JSON。

# 抽取前的三句话自检（每条候选 claim 都要过）

1. **时点检查**：这句话指向的事件，是否发生在 `from_fiscal_year + 1` 财年及以后？
   （horizon.end ≥ FY{from_fiscal_year+1}，或当年内尚未完成的承诺如"本年内将投产"。）
2. **时态检查**：原文是否使用未来式 / 计划式 / 承诺式（"将"、"拟"、"计划"、"力争"、"预计"、
   "未来 X 年"、"到 FYxxxx"）？
   ⚠️ 仅出现"持续"、"继续"、"推动"、"加快"等软承诺词**不足以**判定为前瞻——这些词同样
   常见于过去式回顾（"过去三年我们持续推动…"）。必须配合明确的未来时点 anchor。
3. **可验证性检查**：未来某年是否有明确指标 / 事件 / 状态可拿来对账？纯口号（"成为世界
   一流"无具体里程碑）可以丢。

**三条任一为否 ⇒ 丢弃，不要输出。**

# 什么算"前瞻性断言"

满足以下任意一条的、由管理层发出的、关于未来表现/计划/承诺的可验证陈述：
1. 量化预测：对未来某指标给出数字目标（如"力争 2025 年研发投入占比不低于 8%"）。
2. 战略承诺：明确的业务/产品/产能动作（如"2025 年实现 N+1 制程量产"、"完成 X 工厂建设"）。
3. 资本配置承诺：明确的投资/分红/回购计划（如"未来三年资本开支不超过 200 亿元"）。
4. 风险判断：管理层主动给出的、可被未来事实证伪的判断（如"预计 2025 年行业产能过剩压力将缓解"）。
5. 定性判断：可被验证的方向性判断（如"公司将在 14nm 节点保持领先地位"）。

# 什么不算（重要！高频误抽场景）

下列内容**绝不能抽**：
- 当期已发生的财务/经营事实（如"2024 年营业收入为 577 亿元"——这是事实，不是断言）。
- 历史数据回顾、同比对比（"较上年增长 27%"、"累计实现 X"、"截至报告期末"、"本年/本期"）。
- **过去式 / 完成态描述**——以下措辞为 hard reject 信号：
  「已实现」「已完成」「已投产」「已建成」「已通过」「实现了」「完成了」「达到了」「达成了」
  「截至 YYYY 年 / 截至本报告期末」「本年/本期/报告期内」「累计 / 总计 X 元」。
  即使句子里同时出现"持续推动"、"继续加大"等软承诺词，也按 reject 处理（这些是回顾性叙述）。
- 行业宏观描述（"半导体行业是国家战略性产业"）——除非管理层明确给出可验证的判断。
- 法律样板话术（"本公司董事会保证..."、释义、备查文件清单）。
- 风险因素中的"通用风险"（"地缘政治风险"、"汇率波动风险"——除非给出明确判断或对冲计划）。
- ESG / 公司治理流程描述。
- 模糊愿景类（"打造世界一流"、"成为行业领导者"无具体指标 / 时点 / 里程碑）。

# claim_type 五类（必选其一）

- quantitative_forecast：含明确数字/百分比的预测（最高优先级）
- strategic_commitment：业务/产品/技术节点的承诺
- capital_allocation：资本开支、分红、回购、并购等资金动作承诺
- risk_assessment：管理层对未来风险的明确判断
- qualitative_judgment：方向性的可验证判断（兜底类）

# 字段填写规则

- `original_text`：原文摘录（≤200 字，必须是逐字引用，不要改写）。
- `subject.scope`：整体 / 业务板块 / 子公司 / 产品线 / 工艺节点 / 地区。
- `subject.name`：具体名字（如"成熟工艺"、"中芯京城"、"14nm"）；scope=整体时填空。
- `metric`：原文用词（"营业收入"、"研发投入占比"）。
- `metric_canonical`：归一化英文/拼音 key，能与 financials.db 对账（如 revenue / rd_expense_ratio / capex / gross_margin / net_profit / production_capacity）。无法对账时填 ""。
- `predicate.operator`：>= / <= / = / ≈ / 趋势 / 完成 / 启动 / 暂缓。
- `predicate.value`：数字（不带单位）或文字目标（如"量产"、"投产"）。
- `predicate.unit`：元 / % / 片/月 / 亿元 / 个 ... 没有则 null。
- `horizon.type`：明确日期 / 财年 / 滚动期 / 长期。
- `horizon.start` / `horizon.end`：用 FY 格式，如 "FY2025"；长期目标用 "FY{from_fiscal_year+5}" 之类的 5 年估算（提示中会给 from_fiscal_year）。
- `conditions`：原文里给的前提条件（"在市场需求恢复的前提下"、"取决于设备到货"）。没有填 ""。
- `hedging_words`：原文中命中的软承诺词列表（仅限本 chunk 实际出现的）。
- `specificity_score` 1-5：5=数字+时间+对象都有；3=方向+时间；1=只有方向。
- `verifiability_score` 1-5：5=能直接从财报数据查证；1=纯定性、难以核对。
- `materiality_score` 1-5：5=对公司业绩有重大影响；1=细枝末节。
- `extraction_confidence` 0-1：你对这条抽取本身的确信度。

# verification_plan（粗）

给后续 verifier agent 的提示，可以给空：
- `required_line_items`：要核对哪些 canonical line item（数组，可空）。
- `computation`：怎么算（如 "rd_expense / revenue"）；不需算就给 null。
- `comparison`：怎么比（如 ">= 0.08"）；不需比就给 null。

# 输出格式（必须严格符合）

```json
{
  "claims": [
    {
      "claim_type": "quantitative_forecast",
      "speaker": "董事长 | 总经理 | 管理层 | 董事会 | 未明确",
      "original_text": "...",
      "subject": {"scope": "整体", "name": ""},
      "metric": "...",
      "metric_canonical": "...",
      "predicate": {"operator": ">=", "value": 0.08, "unit": "%"},
      "horizon": {"type": "财年", "start": "FY2025", "end": "FY2025"},
      "conditions": "",
      "hedging_words": ["力争"],
      "specificity_score": 4,
      "verifiability_score": 5,
      "materiality_score": 4,
      "extraction_confidence": 0.85,
      "verification_plan": {
        "required_line_items": ["rd_expense", "revenue"],
        "computation": "rd_expense / revenue",
        "comparison": ">= 0.08"
      }
    }
  ]
}
```

**没有任何前瞻断言时返回 `{"claims": []}`**。不要解释，不要输出 JSON 之外的任何内容。

# 输出前的最后一道闸（再过一遍三句话自检）

逐条扫描你即将输出的 claim：
- 把 `original_text` 重读一遍，确认没有「已 / 实现了 / 完成了 / 截至本报告期末 / 本年 / 累计」等过去态信号；
- 确认 `horizon.start` 与 `horizon.end` 都 ≥ FY{from_fiscal_year}，且至少有一端 > FY{from_fiscal_year}
  （horizon.end == FY{from_fiscal_year} 仅在原文明确使用"本年内将完成"等未承诺态时才允许）；
- 确认这条 claim 在未来某个具体时点上能被一条数据 / 事件证伪。

任一不满足 ⇒ 从输出里删掉，**宁缺毋滥**。
"""


# ==========================================================
# 3. Few-shot 例子（一组正例 + 一组反例）
# ==========================================================
_EXAMPLE_USER_POSITIVE = """from_fiscal_year: 2024
section: 致股东的信
locator: 第二节致股东的信#3
text:
\"\"\"
展望 2025 年，公司将持续聚焦先进工艺平台的产能爬坡，力争实现 14 纳米及以下工艺的全年营收同比增长不低于 30%。我们预计 2025 年资本开支将维持在 75 亿美元左右，主要用于新建产能与设备升级。董事会已批准 2024 年度每股 0.05 元的现金分红方案。
\"\"\"
"""

_EXAMPLE_ASSISTANT_POSITIVE = """{
  "claims": [
    {
      "claim_type": "quantitative_forecast",
      "speaker": "管理层",
      "original_text": "力争实现 14 纳米及以下工艺的全年营收同比增长不低于 30%",
      "subject": {"scope": "工艺节点", "name": "14纳米及以下"},
      "metric": "营业收入同比增长率",
      "metric_canonical": "revenue_yoy_growth",
      "predicate": {"operator": ">=", "value": 0.30, "unit": "%"},
      "horizon": {"type": "财年", "start": "FY2025", "end": "FY2025"},
      "conditions": "",
      "hedging_words": ["力争"],
      "specificity_score": 5,
      "verifiability_score": 5,
      "materiality_score": 5,
      "extraction_confidence": 0.9,
      "verification_plan": {
        "required_line_items": ["revenue_14nm_below"],
        "computation": "(revenue_14nm_below_FY2025 - revenue_14nm_below_FY2024) / revenue_14nm_below_FY2024",
        "comparison": ">= 0.30"
      }
    },
    {
      "claim_type": "capital_allocation",
      "speaker": "管理层",
      "original_text": "我们预计 2025 年资本开支将维持在 75 亿美元左右",
      "subject": {"scope": "整体", "name": ""},
      "metric": "资本开支",
      "metric_canonical": "capex",
      "predicate": {"operator": "≈", "value": 7500000000, "unit": "美元"},
      "horizon": {"type": "财年", "start": "FY2025", "end": "FY2025"},
      "conditions": "",
      "hedging_words": ["预计"],
      "specificity_score": 5,
      "verifiability_score": 5,
      "materiality_score": 4,
      "extraction_confidence": 0.85,
      "verification_plan": {
        "required_line_items": ["capex"],
        "computation": null,
        "comparison": "≈ 75 亿美元"
      }
    }
  ]
}"""

# 反例 A：纯历史事实 + 行业宏观，不应抽出任何 claim
_EXAMPLE_USER_NEGATIVE = """from_fiscal_year: 2024
section: 管理层讨论与分析
locator: 第三节管理层讨论与分析#7
text:
\"\"\"
2024 年公司实现营业收入 577.96 亿元，较上年同期增长 27.0%。半导体行业作为国家战略性新兴产业，在国内外政策环境支持下保持长期增长态势。本公司董事会保证年度报告内容的真实、准确、完整。
\"\"\"
"""

_EXAMPLE_ASSISTANT_NEGATIVE = '{"claims": []}'

# 反例 B（高频陷阱）：句子里出现 hedging 词汇（"持续"、"加大"、"推动"），
# 但整句是过去式回顾——必须识别为非前瞻，返回空 claims。
_EXAMPLE_USER_NEGATIVE_TRAP = """from_fiscal_year: 2024
section: 管理层讨论与分析
locator: 第三节管理层讨论与分析#12
text:
\"\"\"
报告期内，公司持续加大研发投入，全年研发费用累计达到 80.45 亿元，较上年同期增长 11.0%。
公司继续推动 14 纳米及以下先进工艺平台的产能爬坡，截至 2024 年 12 月底已实现月产能较年初提升约 15%。
\"\"\"
"""

_EXAMPLE_ASSISTANT_NEGATIVE_TRAP = '{"claims": []}'


# ==========================================================
# 4. 组装 messages
# ==========================================================
def build_messages(
    chunk_text: str,
    *,
    from_fiscal_year: int,
    section: str,
    locator: str,
) -> list[dict[str, str]]:
    """构造一次抽取调用的 messages。

    返回 OpenAI 格式：[system, user_pos_example, assistant_pos_example,
                       user_neg_example, assistant_neg_example, user_real]
    """
    user_real = (
        f"from_fiscal_year: {from_fiscal_year}\n"
        f"section: {section}\n"
        f"locator: {locator}\n"
        f'text:\n"""\n{chunk_text.strip()}\n"""\n'
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _EXAMPLE_USER_POSITIVE},
        {"role": "assistant", "content": _EXAMPLE_ASSISTANT_POSITIVE},
        {"role": "user", "content": _EXAMPLE_USER_NEGATIVE},
        {"role": "assistant", "content": _EXAMPLE_ASSISTANT_NEGATIVE},
        # 高频陷阱：含 hedging 词的过去式回顾，必须返回空
        {"role": "user", "content": _EXAMPLE_USER_NEGATIVE_TRAP},
        {"role": "assistant", "content": _EXAMPLE_ASSISTANT_NEGATIVE_TRAP},
        {"role": "user", "content": user_real},
    ]
