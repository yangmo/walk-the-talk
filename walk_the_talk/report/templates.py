"""markdown 模板字符串集合。

不引入 jinja，纯 str.format() + 拼接。所有可变内容在 sections.py 里组装好
再 .format(**kwargs) 进对应模板。
"""

from __future__ import annotations

# ============== 整体结构 ==============

REPORT_TPL = """# {company} 管理层"说到做到"分析报告

> ticker: {ticker} | 报告生成日期: {today} | 当前财年基准: FY{current_fy}
> 总 claims: {n_claims} | verified: {n_v} | partially_verified: {n_p} | failed: {n_f} | not_verifiable: {n_nv} | premature: {n_pr} | expired: {n_exp}

{scoreboard_section}

{timeline_section}

{highlights_section}

{method_section}
"""

# ============== 评分板 ==============

SCOREBOARD_HEADER = """## 综合可信度评分

| 维度 | 分值 (0-100) | 说明 |
|---|---|---|
"""

SCOREBOARD_ROW = "| {dim} | {score} | {note} |\n"

SCOREBOARD_NO_DATA_NOTE = (
    "\n> ⚠️ 当前所有 claim 均为 PREMATURE / NOT_VERIFIABLE / EXPIRED，"
    "暂无可对照打分。等到验证窗口的财年 ingest 完成后再跑 verify 即可。\n"
)

# ============== 历年简史 ==============

TIMELINE_HEADER = "## 历年简史（按 from_fiscal_year 倒序）\n"

YEAR_BLOCK_HEADER = "\n### FY{fy} 年报\n"

# bucket 顺序：先 V/F/P（最有信号量），再 NV/PR/EXP
BUCKET_ORDER = [
    ("verified", "✅ 验证通过"),
    ("failed", "❌ 验证不通过"),
    ("partially_verified", "⚠️ 部分通过"),
    ("not_verifiable", "❓ 无法验证"),
    ("premature", "⏳ 未到验证窗口"),
    ("expired", "⏰ 已过期"),
]

BUCKET_HEADER = "- {emoji_label} ({n})\n"
BUCKET_ITEM = "  - **[{cid}]** {summary}\n"

# ============== 突出事件 ==============

HIGHLIGHTS_HEADER = "## 突出事件\n"

HIGHLIGHT_FAILED_HEADER = "\n### 高亮 · 大幅落空 (FAILED)\n"
HIGHLIGHT_VERIFIED_HEADER = "\n### 高亮 · 信守承诺 (VERIFIED)\n"
HIGHLIGHT_PREMATURE_HEADER = "\n### 当前在途 (PREMATURE)\n"

HIGHLIGHT_ITEM = "- **[{cid}]** {summary}{anomaly_suffix}\n"
HIGHLIGHT_ANOMALY_SUFFIX = "\n  ⚠️ **数据存疑**：{detail}，疑为 ingest 单位/口径错位，建议人工复核"

# ============== 验证方法 ==============

METHOD_TPL = """## 验证方法说明

- claims 抽取：DeepSeek-chat（前瞻断言识别 + 五类 claim_type 归类）
- 验证 agent：DeepSeek-chat + 三工具（compute / query_financials / query_chunks）
- 财务数据来源：财报 HTML 表格抽取，归一为元，落地 financials.db
- 文本佐证：BGE-small-zh-v1.5 + BM25 混搜（alpha=0.5）
- 当前财年基准：FY{current_fy}（horizon.end ≤ FY{current_fy} 的 claim 才会被验证）
- 评分：(verified*1.0 + partially_verified*0.5 + failed*0.0) / (V+P+F) × 100
- not_verifiable / premature / expired 不计入分母（不惩罚数据缺失，不预先打分）
"""
