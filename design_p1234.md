# walk-the-talk · 后续四项改动合并设计文档（P1 / P2 / P3 / P4）

> **范围**：P0（canonical 白名单注入）已上线并验证；本文档锁定 P1-P4 四项后续工作的设计基线。
> **当前现实数据**（以 `verdicts_full_run.json` 为准，2026-04-26 全量 --clean 重跑）：
>
> | 指标 | 数 |
> |---|---|
> | claims | 22 |
> | verdicts | 20 |
> | verified | 2 |
> | partially_verified | 1 |
> | failed | 3 |
> | not_verifiable | 6 |
> | premature | 8 |
> | tool errors | 2（gross_margin × 1，depreciation × 1） |
>
> **不实现，只设计**。落地之前需用户拍板顺序与口径。

---

## 〇、问题地图（指标 → 改动锚点）

| 当前症状 | 锚点 | 归口 |
|---|---|---|
| `query_financials` 还有 2 条 LLM 编造字段（gross_margin / depreciation） | `walk_the_talk/verify/tools.py` + `verify/prompts.py` 白名单 | **P1** |
| `financials.db` 里没有折旧/摊销 line item（即便年报附注里有） | `walk_the_talk/ingest/_taxonomy.py` + `ingest/table_extractor.py` 的 LLM extractor prompt | **P2** |
| pipeline 缺 Phase 4，verdicts.json 没有可读输出 | `walk_the_talk/report/` 模块 + `cli.py` 注册子命令 | **P3** |
| 6 条 not_verifiable 中至少 4 条本可救：定性 claim 没去后续年份 chunk 找佐证、数值 claim 单位错了没复核 | `walk_the_talk/verify/prompts.py` finalize 阶段 + `verify/agent.py` 二次检索策略 | **P4** |
| **附录发现**：FY2023 营收 452 亿 vs FY2024 营收 96 亿，比值不合理 | `ingest/_taxonomy.py` 标准化 + 单位归一 | **附录·独立 ticket** |

---

## 一、P1 · `query_financials` 加派生字段

### 1.1 动机

当前 LLM 唯一遗漏的两条派生字段：

- `gross_margin` = 1 - cost_of_revenue / revenue
- `depreciation`（这条归 P2，不归 P1）

派生字段不存进 `financials.db`（因为派生关系 = 冗余），但 LLM 反复造名字。最快的修法是在 `query_financials` 里**虚拟字段化**：白名单里多出来这 4 个名字，背后用基础字段实时算。

### 1.2 接口（不改函数签名）

```python
# walk_the_talk/verify/tools.py
_DERIVED_RECIPES: dict[str, dict] = {
    "gross_margin": {
        "deps": ["revenue", "cost_of_revenue"],
        "compute": lambda d: 1 - d["cost_of_revenue"] / d["revenue"],
        "unit": "ratio",
        "doc": "毛利率 = 1 - cost_of_revenue / revenue",
    },
    "net_margin": {
        "deps": ["revenue", "net_income"],
        "compute": lambda d: d["net_income"] / d["revenue"],
        "unit": "ratio",
        "doc": "净利率 = net_income / revenue",
    },
    "operating_margin": {
        "deps": ["revenue", "operating_income"],
        "compute": lambda d: d["operating_income"] / d["revenue"],
        "unit": "ratio",
        "doc": "营业利润率 = operating_income / revenue",
    },
    "fcf_margin": {
        "deps": ["revenue", "operating_cash_flow", "capex"],
        "compute": lambda d: (d["operating_cash_flow"] - d["capex"]) / d["revenue"],
        "unit": "ratio",
        "doc": "自由现金流率 = (operating_cash_flow - capex) / revenue",
    },
}
```

`query_financials` 命中链路：

```
1. 先按现状走 store.get_series(ticker, line_item, fy)
2. 命中 → 原路返回
3. 未命中 + line_item ∈ _DERIVED_RECIPES → 进 _compute_derived 分支
4. _compute_derived：
   a. 解析 deps，逐一 store.get_series(ticker, dep, fy)
   b. 任一 dep 缺 → 返回 {"error": "...", "missing_deps": [...]}
   c. 全 dep 齐 → 按 fy 维度逐年算，{"line_item": "gross_margin",
      "values": {"FY2024": 0.41, ...}, "unit": "ratio",
      "derived_from": "1 - cost_of_revenue / revenue",
      "inputs": {"FY2024": {"revenue": ..., "cost_of_revenue": ...}}}
5. 都未命中 → 走原 _suggest_alias 路径
```

### 1.3 白名单注入联动

`FinancialsStore.list_canonicals()` 不动；在 `verify/pipeline.py` 第 4.5 步注入白名单时，**额外拼上派生字段**：

```python
canonicals = financials_store.list_canonicals(settings.ticker)
canonicals = sorted(set(canonicals) | set(_DERIVED_RECIPES.keys()))
```

这样 prompt 里的"白名单"就是真实可查的并集，LLM 不会再碰运气写 gross_margin 然后 miss。

### 1.4 测试用例（pytest 骨架）

```python
def test_query_financials_derived_gross_margin(tmp_store):
    tmp_store.upsert_lines([
        FinancialLine(ticker="688981", fiscal_period="FY2024",
            statement_type="income", line_item="营业收入",
            line_item_canonical="revenue", value=57700e9, unit="元"),
        FinancialLine(ticker="688981", fiscal_period="FY2024",
            statement_type="income", line_item="营业成本",
            line_item_canonical="cost_of_revenue", value=44400e9, unit="元"),
    ])
    out = query_financials(tmp_store, ticker="688981",
        line_item_canonical="gross_margin", fiscal_periods=["FY2024"])
    assert out["values"]["FY2024"] == pytest.approx(0.2305, abs=1e-3)
    assert "derived_from" in out

def test_query_financials_derived_missing_dep(tmp_store):
    # 只塞 revenue，cost_of_revenue 缺
    ...
    assert out["error"]
    assert "cost_of_revenue" in out["missing_deps"]

def test_query_financials_derived_in_canonicals_whitelist():
    # build_system_prompt 里能看到 gross_margin
    sp = build_system_prompt(["revenue", "cost_of_revenue", "gross_margin"])
    assert "gross_margin" in sp
```

### 1.5 风险与回退

- **派生只在 query 时算，不入库**：保持 `financials.db` 单一事实源。
- **fcf_margin 依赖 capex**：SMIC 已有 capex 字段，但口径要确认（是否含 ROU 资产）。先按 P1 实现，单元测试用合成数据；SMIC 真值由 P1 端到端跑出来再核。
- **回退**：`_DERIVED_RECIPES = {}` 一行恢复原行为；不动 schema、不动 ingest。

### 1.6 工作量预估

约 50-80 行新代码（含测试），1 处 `pipeline.py` 1 行修改，0 处 schema 变更。无 ingest 重跑需求。

---

## 二、P2 · ingest 折旧/摊销字段补口

### 2.1 动机

观察：`688981-FY2022-004` 的 claim 是"折旧将控制在某水平"（target=0.2），verifier 查 `depreciation` → DB miss → query_chunks → empty → not_verifiable。

但 SMIC 年报现金流量表里有"固定资产折旧、油气资产折耗、生产性生物资产折旧"这一行，附注里也有更细分的折旧明细。**字段是有的，没被 ingest 抓进 DB**。

### 2.2 根因猜测（待 P2 实施时验证）

两个可能：

1. **`_taxonomy.py` 的 alias 表里没有"折旧"相关条目**。LLM extractor 拿到 raw line item "固定资产折旧、油气资产折耗、生产性生物资产折旧"，找不到 canonical 就 drop 或归 `other`。
2. **`table_extractor.py` 的 LLM prompt 让 LLM 自由打 canonical**，但 prompt 没列折旧、摊销作为优先候选，LLM 觉得"不重要"就 skip。

P2 实施第一步是**先读 `_taxonomy.py`**（约 15 分钟），确认是哪一种。两种都不需要重跑 BGE embedding，只需重跑 ingest 的 financials 子流程（~10 分钟/公司）。

### 2.3 拟新增 canonical 字段

| canonical | 报表位置 | 中文 alias | 备注 |
|---|---|---|---|
| `depreciation` | 现金流量表 · 经营活动 | 固定资产折旧 / 折旧 / 累计折旧 | 主用 |
| `amortization_intangible` | 现金流量表 · 经营活动 | 无形资产摊销 | 半导体公司常有 |
| `amortization_long_term_prepaid` | 现金流量表 · 经营活动 | 长期待摊费用摊销 | 可选 |
| `depreciation_amortization_total` | 派生 | 总折旧摊销 | 算 EBITDA 用，可走 P1 派生路径 |

`depreciation_amortization_total` 走 P1 框架做派生（依赖前三者之一/和），不入 DB。

### 2.4 ingest 改动锚点

```
walk_the_talk/ingest/_taxonomy.py
  - 增加 4 条 alias mapping（具体改动等读完文件再细化）

walk_the_talk/ingest/table_extractor.py
  - 如果 LLM extractor prompt 里有「请优先识别以下字段」的列表，把折旧/摊销加进去
  - 如果 prompt 走的是 raw → canonical 自由匹配，靠 _taxonomy 即可

walk_the_talk/ingest/pipeline.py
  - 不需要改

financials.db schema
  - 不需要改（line_item_canonical 是 TEXT 字段，无枚举约束）
```

### 2.5 验证标准

```bash
# 重跑 ingest（仅 financials 子流程；--years 限定加速）
walk-the-talk ingest /Users/alfy/Desktop/股票/中芯国际 --ticker 688981 \
  --company 中芯国际 --no-resume

# 校验 SQL
sqlite3 _walk_the_talk/financials.db \
  "SELECT fiscal_period, value FROM financial_lines
   WHERE ticker='688981' AND line_item_canonical='depreciation'
   ORDER BY fiscal_period"

# 期望：FY2021..FY2025 各有 1 行非空值，量级 ~10 亿/年（中芯每年折旧约 60-80 亿元，
# 因为它是重资产）
```

如果 P2 修后跑出来折旧仍为空，说明 LLM extractor 在 raw 阶段就漏了，需要回到 prompt 调优（这是更大的题，单独 ticket）。

### 2.6 与 P1 的依赖

**无强依赖，但有协同**：

- P1 的 `_DERIVED_RECIPES` 不依赖 P2（gross_margin / net_margin / operating_margin / fcf_margin 用的都是已有字段）
- 但 `depreciation_amortization_total` 派生需要 P2 先把 depreciation 抓进 DB 才有意义
- 推荐顺序：P1 先上线获益（清掉 1 个错误），P2 单独跑

### 2.7 风险

- ~~**重跑 ingest 可能动到 chunks**~~：**已核查（2026-04-26）**：
  - `pipeline.py:160` `if not settings.resume: progress.reset()` — 清空进度全量重跑
  - `reports_store.py:91` 在 `upsert_chunks` 时调 `embedder.encode(docs)`
  - SMIC 规模（5 年 × ~500 chunks）BGE 重 embed 约 75 秒（CPU），**不是阻塞成本**
  - 结论：**不需要新加 `--skip-embed` flag**；P2 直接 `--no-resume` 重跑即可
- **alias 改动可能误匹配**："折旧" 字符在年报里出现频率高，可能命中"摊销折旧准备"这种非主表条目。P2 测试集要包含这类 corner case。

### 2.8 工作量预估

`_taxonomy.py` 改动 ~10 行；如需调 LLM prompt 再 +30 行。验证环节重跑 ingest ~10 分钟。

---

## 三、Phase 4 · `report` 子命令

### 三-A 设计目标

输入：`<data_dir>/_walk_the_talk/verdicts.json` + `claims.json`
输出：`<data_dir>/_walk_the_talk/report.md`

两个目标读者：

1. **作者本人**回看历年判断（"管理层 FY2022 说要做 X，结果做没做"）
2. **新读者**对该公司管理层信誉的快速画像（综合可信度评分 + 高亮事件）

### 三-B 报告结构（草案）

```
# {公司} 管理层"说到做到"分析报告
> ticker: {ticker} | 报告生成日期: {today} | 当前财年基准: FY{current_fy}
> 总 claims: {N} | 已 verified: {V} | partially: {P} | failed: {F}
>           not_verifiable: {NV} | premature: {PR}

## 综合可信度评分

| 维度 | 分值 (0-100) | 说明 |
|---|---|---|
| 整体可信度 | 76 | (V + 0.5*P) / (V+P+F)，failed 有数据可对照的占比 |
| 量化承诺命中率 | 82 | 5 类 claim 中 quantitative_forecast 子集的命中率 |
| 战略承诺执行率 | — | strategic_commitment 子集（多数 not_verifiable，斟酌是否打分） |
| 资本配置准确度 | 60 | capital_allocation 子集 |

## 历年简史（按 fiscal_year 倒序）

### FY2024 年报（claims 提出于 2025 年报）
- ✅ 验证通过 (2)
  - **[FY2024-001]** "营收增速达到可比同业平均值"  → 实际 -78.76% vs 同业平均 X% → 详见
  - ...
- ❌ 验证不通过 (1)
  - **[FY2024-002]** "FY2025 capex 与上一年持平"  → 实际同比 +9.88% → 9.88% > 5% 持平阈值
- ⚠️ 部分通过 (0)
- ⏳ 未到验证窗口 (8)
- ❓ 无法验证 (1)

### FY2023 年报
...

### FY2022 年报
...

## 突出事件

### 高亮 · 大幅落空 (FAILED)
- **FY2022-005**: 承诺 "capex 与 FY2022 持平"，实际 FY2023 同比 +27.6%
- **FY2023-003**: 承诺 "营收同比中个位数增长"，实际 FY2024 同比 -78.76%
  ⚠️ **数据存疑**：FY2023 营收 452 亿 vs FY2024 营收 96 亿，量级差 5x，
  疑为 ingest 单位/口径错位，需人工复核（详见附录·额外发现）
- **FY2024-002**: capex 承诺持平实际 +9.88%

### 高亮 · 信守承诺 (VERIFIED)
- **{cid}**: ...

### 当前在途 (PREMATURE)
- 8 条 claim 等待 FY2026+ 数据揭晓

## 验证方法说明
- claims 抽取：DeepSeek-chat
- 验证 agent：DeepSeek-chat + 三工具 (compute / query_financials / query_chunks)
- 财务数据来源：财报正文 + 表格抽取，落地 financials.db
- 文本佐证：BGE-small-zh-v1.5 + BM25 混搜，alpha=0.5
- 当前财年基准：FY{current_fy}（早于此的 horizon 才会被 verify）
```

### 三-C 模块结构

```
walk_the_talk/report/
  __init__.py
  builder.py        # 主入口 build_report(verdict_store, claim_store, financials_store) -> str
  scoring.py        # 综合可信度评分公式（独立可测）
  sections.py       # 各 section 渲染函数
  templates.py      # markdown 模板字符串
  highlights.py     # 突出事件挑选规则
```

### 三-D CLI

```bash
walk-the-talk report <data_dir> --ticker 688981 --company 中芯国际 \
  [--out report.md] [--current-fy 2025] [--no-highlights]
```

落点：`<data_dir>/_walk_the_talk/report.md`（与其它产物同目录）

`--out` 允许重定向。`--no-highlights` 关闭突出事件区（用于纯诊断场景）。

### 三-E 评分公式（待商定）

```python
# scoring.py 草案
def overall_credibility(verdicts: list[Verdict]) -> int | None:
    """0-100 整数分；分母为有数据可对照的 claims (V+P+F)。"""
    actionable = [v for v in verdicts
                  if v.verdict in ("verified", "partially_verified", "failed")]
    if not actionable:
        return None  # 全是 premature/not_verifiable，不打分
    score = sum({"verified": 1.0, "partially_verified": 0.5, "failed": 0.0}[v.verdict]
                for v in actionable) / len(actionable)
    return round(score * 100)
```

**待用户决策**：

1. partially_verified 给 0.5 分还是 0.7 分？建议 0.5 偏严格。
2. not_verifiable 是否惩罚？建议**不惩罚**（数据缺失不应归咎于管理层），但在报告里显式列出"x 条 claim 因数据缺失无法验证"提醒读者。
3. 不同 claim_type 是否加权？建议 v1 不加权，v2 再考虑（quantitative > strategic > qualitative 的权重曲线）。

### 三-F 实现优先级（v1 取舍）

| 必须 | 可选 | 推迟 v2 |
|---|---|---|
| 综合可信度评分 | 突出事件高亮 | 跨公司对比 |
| 按年份分组的 claim 列表 | 评分公式可配置 | HTML 输出 |
| FAILED claim 详情 | 定性 claim 与数据 claim 分桶 | 时间轴可视化 |

### 三-G 测试

```python
def test_report_smoke():
    """从内存构造 minimum verdict set，build_report 不崩、含关键 section。"""
    md = build_report(verdict_store=fake_vs, claim_store=fake_cs)
    assert "综合可信度评分" in md
    assert "FY2024" in md

def test_overall_credibility_all_verified():
    assert overall_credibility([fake_v_verified] * 5) == 100

def test_overall_credibility_no_actionable():
    assert overall_credibility([fake_v_premature] * 3) is None
```

### 三-H 工作量预估

约 200-300 行新代码（含模板与测试）。无新依赖（继续用 Pydantic + jinja-style 简单 .format()）。

---

## 四、P4 · verify prompt 救 `not_verifiable`

### 4.1 6 条 not_verifiable 根因表

| claim_id | target | comment 摘要 | 救援可能 | 建议 |
|---|---|---|---|---|
| FY2021-001 | "从全线紧缺转入结构性紧缺" | 定性，chunk 无证据 | **可救** | finalize 前再做一次扩展检索（去 mda 章节、去多年份） |
| FY2021-002 | "有序推进" | 同上 | **可救** | 同上 |
| FY2022-004 | "0.2"（折旧） | DB miss，chunk 无证据 | 部分可救（依赖 P2） | 等 P2 上线后此条自动改善 |
| FY2022-006 | "0"（月产能增量） | DB 无产能字段 | **较难** | 产能不在财报标准字段里，建议归 not_verifiable |
| FY2023-005 | "推进"（12 寸厂建设） | 定性 + capex 不直接对应 | **可救** | 二次检索"12英寸 / 临港 / 京城"等关键词 |
| FY2024-001 | "可比同业的平均值" | 缺同业数据 | 不可救（v1 范围外） | v2 再考虑跨公司对比 |

**结论**：6 条里 4 条可救，2 条接受 not_verifiable。

### 4.2 救援机制设计

观察当前 not_verifiable 的工具调用 trace：

- 4 条只调了 1 次 query_chunks，命中 0 → 直接放弃
- 2 条调了 query_financials + query_chunks，都 miss → 放弃

**问题**：LLM 的"放弃阈值"太低，1 次 chunk miss 就 finalize。

**修法**（prompt 改 + agent 微调）：

#### 4.2.1 Prompt 加段（`verify/prompts.py`）

在 `_TOOLS_DOC_TEMPLATE` 末尾追加：

```
**关于 query_chunks 检索失败的处理**：
- query_chunks 第一次返回 [] 不要立即放弃，尝试以下任一变体再查一次：
  1. 把名词改成同义/近义词（例：「全线紧缺」→「产能紧张 OR 供不应求」）
  2. 拓宽 fiscal_periods（例：[FY2022] → [FY2022, FY2023]）
  3. 降低 top_k 到 5、提高 alpha 到 0.7（更偏向语义检索）
- 第二次仍 miss 才考虑 NOT_VERIFIABLE。
- 对于定性/方向性 claim（如「有序推进」「持续优化」），即使数据没说死，
  只要后续年份原文出现了一致语调（如年报继续提及该项目仍在进行）即可视为
  PARTIALLY_VERIFIED 或 VERIFIED，不要因为「无定量数据」就一律 NOT_VERIFIABLE。
```

#### 4.2.2 Agent 状态机加一条边（`verify/agent.py`）

当前 `plan_node → call_tool → ... → finalize`。

新增：

- `finalize_node` 入口先检查：如果 `verdict == NOT_VERIFIABLE` 且 `tool_call_count < 3` 且 没出现过 `query_chunks` 第二次重试 → **不允许 finalize**，回到 `plan_node` 强制再来一轮，并往 system prompt 注入"上一轮你过早放弃，请按上述 fallback 策略重检索"。

伪代码：

```python
def _gate_finalize(state: VerifyState) -> str:
    """finalize_node 前置守卫；返回下一节点名。"""
    proposed = state.get("proposed_verdict")
    n_tools = len(state.get("tool_calls", []))
    n_chunk_calls = sum(1 for t in state["tool_calls"] if t.tool == "query_chunks")
    has_retried = state.get("chunk_retry_done", False)

    if proposed == "NOT_VERIFIABLE" and n_tools < 3 and not has_retried and n_chunk_calls >= 1:
        state["chunk_retry_done"] = True
        state["force_retry_message"] = (
            "你的初步结论是 NOT_VERIFIABLE，但 query_chunks 只调用了一次。"
            "按 prompt 的 fallback 策略，请用同义词扩展 / 多年份 / 调 alpha 再查一次。"
        )
        return "plan"  # 回 plan
    return "finalize"
```

#### 4.2.3 max_iters 微调

当前 `max_iters=3` 触发 forced_finalize 的有 2 条。把 `max_iters` 调到 **4**，给救援轮一个安全垫，不增加多少成本（DeepSeek prefix cache 命中率高，每多一轮主要是 completion token）。

### 4.3 验证标准

P4 上线后期望：

| 指标 | baseline | 期望 |
|---|---|---|
| not_verifiable | 6 | ≤ 3（救回 3 条） |
| forced_finalize | 2 | ≤ 1 |
| query_chunks 平均次数/claim | 0.3 | 0.6-0.9 |
| 总 prompt_tokens | 81043 | +15% 以内（83000-93000） |

### 4.4 风险

- **过度检索风险**：如果 prompt 鼓励"多查一次"，可能让原本就该是 NOT_VERIFIABLE 的真正模糊 claim 走 PARTIALLY_VERIFIED 路径，引入 false positive。**对策**：救援轮只允许把 NOT_VERIFIABLE 改成 NOT_VERIFIABLE 或 PARTIALLY_VERIFIED，**禁止**改成 VERIFIED（VERIFIED 必须有定量证据，prompt 里强约束）。
- **成本风险**：max_iters +1 会让 worst case prompt token 多 30%，但 DeepSeek 服务端 prefix cache 命中后边际成本很低。监控 `cache_hits` 比率即可。

### 4.5 工作量预估

prompt 改 ~30 行，agent.py 加 `_gate_finalize` ~25 行，1-2 个新单测。

---

## 五、四项改动的实施顺序

```
P1 (派生字段)  ─┬─→ P3 (report)
                │       ↑
                ▼       │ 用 P1 数据做 fcf_margin 高亮
P2 (折旧补口) ──┴─→ P4 (verify 救援)
                              ↑
                              │ P2 让 depreciation 类 claim 不再 not_verifiable
```

**推荐顺序**：

1. **P1**（最小，只动 verify 层）→ 跑端到端验证，确认派生字段进入工作流
2. **P4**（中等，prompt + agent 局部）→ 跑端到端验证，确认 not_verifiable ≤ 3
3. **P2**（涉及 ingest 重跑，较慢）→ 重跑 ingest + verify
4. **P3**（最大，新模块）→ 用 P1+P2+P4 后的 verdicts 跑出最终 markdown

理由：P1/P4 改动小、迭代快、不依赖重跑；P2 涉及 ingest 时间最长；P3 依赖前三者的产出做最终展示。

---

## 六、附录 · 额外发现（独立 ticket，不在 P1-P4 范围）

### 6.1 SMIC 营收量级异常

`verdicts_full_run.json` 中 `688981-FY2023-003` 显示：

```
FY2023 revenue = 45250425000  (≈ 452.5 亿)
FY2024 revenue = 9612775000   (≈ 96.1 亿)
同比 -78.76%
```

事实：SMIC 公开年报 FY2024 营收约 577 亿元（上交所披露 / 公司 IR）。

**疑点**：

- FY2024 的 96 亿是**季度数据**（约 1Q）？还是**美元计价折回**？
- 还是 ingest 表抽取阶段把 "57,789,540 千元" 当成了 "57,789,540 元" → 漏 *1000？

**排查建议**（独立 ticket）：

1. SQL 直查 `SELECT * FROM financial_lines WHERE ticker='688981' AND line_item_canonical='revenue' ORDER BY fiscal_period` 看完整序列与 unit 列
2. 反推到 source_path / source_locator，去原 HTML 看那一行表格
3. 如果是 ingest 单位归一 bug，会影响**所有公司所有金额字段**，是高优 bug

### 6.2 verdicts.json schema 注记

实际 schema：

```json
{
  "company_name": str,
  "ticker": str,
  "claims_processed": int,
  "verifications": {
    "<claim_id>": [
      {
        "fiscal_year": int,         # 该 verification 对应的目标 FY
        "verdict": str (lowercase), # verified/partially_verified/failed/not_verifiable/premature/expired
        "target_value": str,        # claim 的目标量化值（字符串）
        "actual_value": str | null, # 实际取到的值
        "evidence": [...],
        "computation_trace": [{"tool_name", "args", "result", "error"}],
        "confidence": float,
        "comment": str,
        "cost": {prompt_tokens, completion_tokens, total_tokens, cache_hits,
                 chat_calls, iter_count, forced_finalize}
      }
    ]
  }
}
```

P3 的 `report/builder.py` 会基于这个 schema 写。

---

## 七、已决项（用户 2026-04-26 锁定，"全部按你的建议来"）

| # | 决策点 | 选择 | 影响范围 |
|---|---|---|---|
| 1 | 评分公式 partially_verified 权重 | **0.5**（偏严格，宁可低估不高估） | P3 `report/scoring.py` |
| 2 | claim_type 加权 | **v1 不加权**；所有 claim 平权进总分 | P3 `report/scoring.py` |
| 3 | P2 重跑成本 | **已核查不阻塞**：BGE 重 embed ~75s（见 §2.7）；不加 `--skip-embed` flag | P2 实施 |
| 4 | 救援轮 verdict 上限 | **最多升级到 PARTIALLY_VERIFIED**；禁止从 NOT_VERIFIABLE 直接跳 VERIFIED | P4 `_gate_finalize` 守卫 + prompt 强约束 |
| 5 | 附录 6.1 营收异常 | **独立 ticket，先排查**（不并入 P2）；P3 报告里在该条 FAILED 旁加 ⚠️ "数据存疑"标注 | 独立 + P3 |

### 七-A 评分细则（基于 #1 #2 锁定后）

```python
# walk_the_talk/report/scoring.py 实施基线
_VERDICT_WEIGHTS = {
    "verified": 1.0,
    "partially_verified": 0.5,   # ← #1 锁定为 0.5
    "failed": 0.0,
}
# claim_type 不进入权重 ← #2 锁定
# not_verifiable / premature 不计入分母（不惩罚数据缺失）
```

### 七-B 救援轮 verdict 矩阵（基于 #4 锁定）

| 救援前 verdict | 允许的救援后 verdict |
|---|---|
| NOT_VERIFIABLE | NOT_VERIFIABLE / PARTIALLY_VERIFIED |
| ~~NOT_VERIFIABLE → VERIFIED~~ | **禁止**（prompt 强约束 + 后置校验） |

prompt 加段（在 §4.2.1 末尾追加）：

```
**救援轮 verdict 上限**：如果你在第二轮检索后改变结论，最多只能升级到
PARTIALLY_VERIFIED；想升级到 VERIFIED 必须有定量证据（数字 + 单位 + 出处），
否则保持 PARTIALLY_VERIFIED 或 NOT_VERIFIABLE。
```

agent.py 后置校验（在 finalize_node 落库前）：

```python
def _enforce_rescue_ceiling(state: VerifyState) -> VerifyState:
    if state.get("chunk_retry_done") and state["proposed_verdict"] == "VERIFIED":
        # 救援轮触发过，且最终结论是 VERIFIED，必须有定量 evidence
        if not _has_quantitative_evidence(state["evidence"]):
            log.warning("救援轮 VERIFIED 缺定量证据，降级为 PARTIALLY_VERIFIED")
            state["proposed_verdict"] = "PARTIALLY_VERIFIED"
    return state
```

### 七-C 附录 6.1 营收异常的独立 ticket（基于 #5 锁定）

**Ticket 名**：`#unit-normalization-bug` 或类似
**优先级**：HIGH（潜在影响所有公司所有金额字段）
**实施在 P2 之前**还是**与 P2 并行**？建议**先于 P2** 排查清楚，原因：

1. 如果是 ingest 单位归一 bug，FY2024 revenue 应是 5770 亿（千元/百万元误读为元）
2. 这个 bug 一旦修了，**FY2023-003 这条 FAILED 可能直接变 PARTIALLY_VERIFIED 或 VERIFIED**（因为实际营收增幅可能是正的，与"中个位数增长"承诺更接近）
3. 不查清就上 P2，P2 的 depreciation 字段也可能踩同样的单位坑

排查步骤（5 分钟分析任务，不动代码）：

```sql
-- 步骤 1：完整 revenue 序列 + unit 列
SELECT fiscal_period, value, unit, source_path, source_locator
FROM financial_lines
WHERE ticker='688981' AND line_item_canonical='revenue'
ORDER BY fiscal_period;

-- 步骤 2：横向看其它金额字段是否同步异常
SELECT line_item_canonical, fiscal_period, value, unit
FROM financial_lines
WHERE ticker='688981' AND fiscal_period IN ('FY2023','FY2024')
  AND line_item_canonical IN ('revenue','cost_of_revenue','net_income','capex','operating_cash_flow')
ORDER BY fiscal_period, line_item_canonical;
```

期望发现之一：

- **A 类**：FY2024 全量金额都缩小约 60x → ingest 把"百万元"当成了"元"
- **B 类**：FY2024 营收单条数据异常，其它字段正常 → 表抽取漏读了一行/串了行
- **C 类**：unit 列内容不一致（"千元" vs "元"混用）→ FinancialLine.value 应有归一规则

---

## 八、决策日志

| 日期 | 项 | 决策 |
|---|---|---|
| 2026-04-26 (P0) | canonical 白名单注入 verify system prompt | 已上线 |
| 2026-04-26 (本文) | 七-1 partially_verified 权重 = 0.5 | 锁定 |
| 2026-04-26 (本文) | 七-2 v1 claim_type 不加权 | 锁定 |
| 2026-04-26 (本文) | 七-3 ingest 重跑成本可接受，不加新 flag | 锁定（已核查） |
| 2026-04-26 (本文) | 七-4 救援轮 verdict 最多升至 PARTIALLY_VERIFIED | 锁定 |
| 2026-04-26 (本文) | 七-5 营收异常独立 ticket，先于 P2 排查 | 锁定 |

---

*文档结束。本文档不包含任何源码改动；解除约束后按本设计 + 上述已决项实施。*
