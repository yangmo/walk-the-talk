# 贡献指南（Contributing）

欢迎贡献！本文档记录开发流程、代码风格、测试要求与 PR 流程。

---

## 快速准备开发环境

```bash
git clone git@github.com:yangmo/walk-the-talk.git
cd walk-the-talk
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install                # 装 git pre-commit hook
cp .env.example .env
# 编辑 .env 填 DEEPSEEK_API_KEY，端到端跑批才需要
```

> 不需要 `DEEPSEEK_API_KEY` 也能跑 pytest——所有 LLM 调用在测试里都用 stub 替身。

---

## 日常开发循环

```bash
# 跑测试（端到端用 hash embedder，不用下 BGE 模型）
pytest -v

# 跑单一 phase 的测试
pytest tests/verify/

# 跑覆盖率
pytest --cov=walk_the_talk --cov-report=term-missing

# 跑 lint
ruff check .
ruff format .

# 一次性预检（git commit 时 pre-commit 会自动跑）
pre-commit run --all-files
```

提交前 `pre-commit` 会自动：

- 用 ruff 修可自动修的 lint 问题（imports 排序、未用变量等）
- 用 ruff format 格式化
- 修文件末尾换行 / 行尾空格
- 检 yaml / toml 语法
- 拒收 >500KB 的非 fixture 文件

---

## 项目结构与改动定位

| 想改… | 改动锚点 |
|---|---|
| HTML 解析 / 章节切分 / 表格抽离 | `walk_the_talk/ingest/html_loader.py` |
| 三大表识别 / 单位归一 / line item alias | `walk_the_talk/ingest/{table_extractor,_taxonomy}.py` |
| 抽前瞻断言的 prompt | `walk_the_talk/extract/prompts.py` |
| 验证 agent 的 prompt | `walk_the_talk/verify/prompts.py` |
| `compute / query_financials / query_chunks` 工具 | `walk_the_talk/verify/tools.py` |
| LangGraph 状态机节点 / 边 | `walk_the_talk/verify/agent.py` |
| rescue gate / ceiling 逻辑 | `walk_the_talk/verify/rescue.py` |
| 报告评分公式 / 高亮挑选 | `walk_the_talk/report/{scoring,highlights}.py` |
| 报告 markdown 模板 | `walk_the_talk/report/templates.py` |

完整的设计决策与四阶段流程见 [`design.md`](design.md)。

---

## 代码风格

- **Python 3.10+ 语法**：`int | None` 优于 `Optional[int]`，`list[...]` 优于 `List[...]`。
- **类型注解**：公开 API 必须有；内部 helper 视情况。
- **docstring 风格**：模块顶部用一段简介 + Args/Returns/Raises（reStructuredText 兼容 Sphinx）；测试函数用一句中文说明该用例覆盖什么场景。
- **私有符号**：以 `_` 开头的 helper 不要被外部 import；要被外部 import 的请改成公有名（参见 R3 把 `verify._gate_finalize` 改成 `verify.rescue.gate_finalize`）。
- **logging vs print**：库代码用 `logging.getLogger(__name__)`，CLI 入口用 `rich.console.Console.print`，不要裸 `print`。

---

## 测试约定

- 测试目录按 phase 分组：`tests/{ingest,extract,verify,report}/`。每个 phase 文件命名对应被测模块（如 `test_pipeline.py` 对应 `<phase>/pipeline.py`）。
- **共享 fixture** 集中在 `tests/conftest.py`，phase 专用 fixture 放 `tests/<phase>/conftest.py`（按需创建）。
- 端到端测试种子是 `tests/fixtures/中芯国际/2025.html`，通过 `smic_html_path` / `smic_data_dir` fixture 注入——不要 hard-code `Path(__file__).parent / "fixtures" / ...`。
- 涉及 LLM 的测试一律用 stub `LLMClient` 替身（参见 `tests/verify/test_pipeline.py::_AlwaysNotVerifiableLLM`），不要真发请求。
- 涉及 SQLite / Chroma 的测试用 `tmp_path` fixture，每个测试独立工作目录。

---

## 分支与 commit 约定

- 分支命名：`<type>/<short-desc>`，例如 `fix/unit-normalization-revenue`、`feat/cross-company-comparison`。
- commit message 格式（参考已有 commit）：

  ```
  <type>(<scope>): <subject>

  <body 写为什么这么改、影响哪些模块、为什么没改其他相邻位置>
  ```

  常用 type：`feat` / `fix` / `refactor` / `test` / `docs` / `chore` / `perf`。
- 一个 commit 做一件事；多个独立改动拆多个 commit；本地 squash 合并见 README 上面 R4 那次合并示例。

---

## PR checklist

打开 PR 之前请自查：

- [ ] `ruff check .` 与 `ruff format --check .` 都通过
- [ ] `pytest -v` 全部通过（包括端到端 SMIC fixture）
- [ ] 改动有对应单测；新增公开 API 有 docstring
- [ ] `CHANGELOG.md` 的 `[Unreleased]` 区段记录了本次改动
- [ ] 如果改了数据契约（`claims.json` / `verdicts.json` / `financials.db` schema），在 PR 描述里**明确指出向后兼容性**
- [ ] 如果改了 prompt（`extract/prompts.py` / `verify/prompts.py`），在 PR 描述里贴**前后两次同一份 SMIC 数据的对比**

---

## 关于已知 issue

修 `#unit-normalization-bug`（设计 §14.1）特别欢迎——这是 v0.1 报告数字仍然不可信的根因。如果你打算修，先在 issue 区开个 ticket 讨论复现 + 排查思路。
