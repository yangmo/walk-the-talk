# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Refactor

- **R0**：删杂物（`SMIC_report_preview.md`、`config.yaml.example`、误建 venv），加 `LICENSE`，建 `.github/` 占位。
- **R1**：重写 `README.md`（架构图 + SMIC 真实数据 + 技术选型表 + 开发中免责声明 + High-level RAG/LangGraph 架构说明）；合并 `design.md` + `design_p1234.md` 为单一真相源；新建 `CHANGELOG.md`；删 `design.md` 决策日志（信息归 CHANGELOG）。
- **R3**：抽 rescue gate / ceiling 到独立模块 `verify/rescue.py`；删 verify/pipeline.py 两个 deprecated 函数（`_verify_one_claim`、`_detect_current_fiscal_year`）；ruff 配置 + auto-fix，Phase 2/3/4 + llm/core/cli/config 全部 ruff 零 warning。
- **R4**：测试目录按 phase 分子目录（`tests/{ingest,extract,verify,report}/`），git mv 保 history；新建 `tests/conftest.py` 提供共享 fixture（`fixtures_dir` / `smic_html_path` / `smic_data_dir`），消除 5 处散落的 `Path(__file__).parent / "fixtures" / ...`；新建 `tests/verify/test_rescue.py` 单测 P4 rescue 边界（14 个 case）。

## [0.1.0] – 2026-04-26

首版。SMIC（688981）FY2021-FY2025 五年端到端跑通：22 条 claim，整体可信度 58/100。

### Added

- **Phase 1 · Ingest**：HTML（新浪财经全文页）→ chunks（Chroma + BM25）+ financials.db（SQLite，三大表 + 折旧/摊销补口）。BGE-small-zh-v1.5 + jieba 分词。`_taxonomy.py` 覆盖 80+ 中文 line item alias，三大表关键词命中数 + 行数判定 statement_type，多片段表格单位继承。
- **Phase 2 · Extract**：DeepSeek-chat 抽前瞻 claim，response_format=json_object。Postprocess 链：section 黑名单 → horizon 时效过滤 → trivial 阈值 → 同年 canonical_key 去重 → 跨年法律样板指纹去重。LLM 失败两级降级到 deepseek-reasoner。
- **Phase 3 · Verify**：LangGraph 状态机驱动单 claim 验证（plan → tool → finalize）。三工具：`compute`（AST 白名单求值，消除算术幻觉）/ `query_financials`（含派生字段 gross_margin / fcf_margin 等）/ `query_chunks`（dense + BM25 混搜，RRF 融合）。
- **Phase 4 · Report**：合成 markdown 可信度报告。三维评分（整体 / 量化承诺 / 资本配置）；FAILED / VERIFIED / PREMATURE 高亮挑选；FAILED 条目自动检测 actual_value 量级偏差 ≥ 5x 标"数据存疑"。
- **CLI**：`walk-the-talk ingest / extract / verify / report` + `inspect` 调试子命令。Typer + rich 终端表格输出。
- **LLM 层**：抽象 `LLMClient` + `DeepSeekClient` 实现；SQLite (WAL) prompt cache；指数退避重试（仅 5xx / 429 / 网络错）。
- **测试**：pytest 100+ 用例，含中芯国际 FY2025 端到端 fixture（848KB GBK HTML）。

### P0–P4 上线后优化

- **P0**：canonical 白名单注入 verify system prompt（减少 80% line_item miss-and-retry）。
- **P1**：`query_financials` 加 5 个派生字段（gross_margin / net_margin / operating_margin / fcf_margin / depreciation_amortization_total）。
- **P2**：ingest `_taxonomy.py` 补 5 条折旧/摊销 alias（覆盖 SMIC 现金流补充资料表）。
- **P3**：Phase 4 report 实装。评分公式：`(V*1.0 + P*0.5 + F*0) / (V+P+F) × 100`，NV/PR/EXP 不进分母。
- **P4**：verify rescue gate（NOT_VERIFIABLE 强制重试一轮）+ ceiling（rescue 路径下 verified 强制下调为 partially_verified）。

### Known Issues

- `#unit-normalization-bug`：FY2023 → FY2024 营收量级差 5x，疑为多片段表格单位继承漏处理。详见 [design.md §14.1](design.md#141-已知-issue-unit-normalization-bug高优先级)。

[Unreleased]: https://github.com/yangmo/walk-the-talk/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/yangmo/walk-the-talk/releases/tag/v0.1.0
