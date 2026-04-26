# walk-the-talk — 项目上下文（给 Claude 用）

## 一句话

回溯上市公司历年年报中管理层做出的可验证断言（claim），用后续年份的事实回头打分。

## 输入约定

CLI: `walk-the-talk ingest <data_dir> --ticker <T> --company <C>`

`<data_dir>` 是含 `<year>.html` 的目录（如 `2024.html`）。HTML 来源是新浪财经
`vCB_AllBulletinDetail.php` 全文页，**用户手动下载**，本仓库不内置爬虫。

工作产物落在 `<data_dir>/_walk_the_talk/`。

## 四个 Phase

1. **Ingest**：HTML → chunks（Chroma） + financials.db（SQLite 三大表）
2. **Extract**：chunks → claims.json（前瞻断言）
3. **Verify**：claims + financials → verdicts.json（agent 用工具校验）
4. **Report**：verdicts → markdown（历年简史 + 可信度报告）

每个 phase 独立 CLI 子命令，落盘文件解耦。改 prompt 只重跑对应 phase。

## 技术选型

- HTML: BeautifulSoup4 + lxml + chardet（GBK 兼容）
- Embedding: BGE-small-zh-v1.5（512 维，本地 CPU）
- 向量库: Chroma
- BM25: rank_bm25 + jieba
- 编排: LangGraph（per-Phase 内部状态机），SqliteSaver 做 checkpointer
- LLM: DeepSeek-chat / DeepSeek-reasoner

## 重要原则

1. **Claim 只关注"管理层对未来的判断/预测/承诺"**。当期已发生的财务/经营数据是事实，进 financials.db，不进 claims。
2. `claim_type` 收敛到 5 类：quantitative_forecast / strategic_commitment / capital_allocation / risk_assessment / qualitative_judgment。
3. **数值比较和算术由 `compute` 工具完成，LLM 只决定调用什么工具**——消除数值幻觉。
4. **`<table>` 在 HTML loader 里必须先抽离再 `get_text`**，否则列对齐丢失。

## 开发习惯

- 用户偏好中文沟通
- 写代码前先有 plan，确认后再动
- 每个 phase 完成后用真实数据（中芯国际）端到端验证
- 详细设计文档在仓库根 `design.md`（如果还没有，从老的 `Credibility Check/v2_design.md` 复制过来）
