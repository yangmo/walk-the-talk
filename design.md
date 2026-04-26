# walk-the-talk 设计文档

> **walk-the-talk**：回溯上市公司历年年报中管理层做出的可验证断言（claim），用后续年份的事实回头打分，量化"管理层是否说到做到"。
>
> GitHub 仓库名：**`walk-the-talk`**
> 本地代码路径：**`/Users/alfy/Desktop/workspace/walk-the-talk`**（待创建）
>
> 本文档是 clean-slate 设计基线，开始实现时直接对照。
>
> 文档内若提到 "v1" / "v2"，指的是 **walk-the-talk 自身的版本范围**：v1 = 本次实现（年报、单公司、HTML 输入），v2 = 未来扩展（季报、跨公司对比、reranker 等）。

## 决策基线（已确认）

| 维度 | 选择 |
|---|---|
| GitHub 仓库名 | `walk-the-talk` |
| 本地代码路径 | `/Users/alfy/Desktop/workspace/walk-the-talk` |
| **输入格式** | **HTML（新浪财经年报全文页）** |
| **输入约定** | 调用时传入一个目录路径，目录下放 `<year>.html`（如 `2024.html`、`2025.html`） |
| 数据获取 | **手动下载**（不做爬虫，不做 fetcher） |
| Embedding 模型 | **BGE-small-zh**（512 维，~100MB，本地 CPU） |
| 向量库 | **Chroma** |
| BM25 | 本地 `rank_bm25` + jieba 分词 |
| 结构化表 | v1 必做（财务三表 + 关键附注），从 HTML `<table>` 二维直读 |
| 季报支持 | v1 只年报，v2 再扩季报 |
| Classifier 模型 | DeepSeek-chat（前瞻 chunk 二分类） |
| Extractor 模型 | DeepSeek-chat（chat 失败两级降级到 reasoner） |
| Verifier 模型 | reasoner（planner / synthesizer）+ chat（executor）混合 |
| `verification_plan` 产出阶段 | extract 阶段产出粗 plan，verify 第一轮自检修正 |
| 测试 fixture | 主链路跑通后补 smoke test（HTML loader / table_extractor） |
| 编排框架 | **LangGraph（per-Phase 内部状态机）** |
| Checkpointer | **SQLite（LangGraph `SqliteSaver`）**——落盘到 `<workdir>/checkpoints.db` |

---

## 零、输入约定与 HTML 解析

### 0.1 为什么选 HTML

实测同份年报（中芯国际 FY2025）的 PDF 与 HTML 两路提取效果对比：

| 维度 | PDF（PyMuPDF） | HTML（BeautifulSoup） |
|---|---|---|
| 页眉/页脚噪音 | 出现 100+ 次 | 公司名只出现 34 次（仅在该出现的语义位置） |
| 章节切分 | 启发式（字号/位置）+ 易错 | `re.split(r"第[一二三四五六七八九十]+节")` 一行搞定 |
| "致股东的信"块 | 经常被分页打散 | 完整 1118 字一整块 |
| 总文本量 | ~250K 字符（含噪音） | 199K 字符 |
| 文件体积 | 5–10 MB | 848 KB（199 KB 纯文本） |
| 编码 | UTF-8 直出 | GBK，需要先转码 |
| 表格 | 列对齐易错（要 camelot/tabula 二次校正） | `<tr><td>` 二维直读 |
| 段落断裂 | 长段落被分页规则切碎 | 段落天然完整 |

净结论：**HTML 在章节切分、段落完整性、噪音剔除、表格保真四方面显著优于 PDF**；唯一额外成本是 GBK 转码（一行 `chardet` 解决）。本项目只支持 HTML 输入，不做 PDF 解析。

### 0.2 HTML 来源

新浪财经的"全部公告"详情页：

```
https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=<ticker>&id=<bulletin_id>
```

特点：

- 每份年报对应一个 `bulletin_id`，HTML 全文渲染在 `div#content` 内
- charset = GBK，必须显式声明
- 几乎不含 JS 渲染（纯静态 DOM 即可解析）
- A 股主板 / 科创板 / 创业板的年报基本都覆盖

**用户负责手动下载 HTML 并放入指定目录**，本项目不内置爬虫（反爬策略与 IP 频控不可控，徒增维护成本）。

### 0.3 输入约定

CLI 调用形式：

```bash
walk-the-talk ingest <data_dir> --ticker 688981 --company "中芯国际"
```

`<data_dir>` 是包含年报 HTML 文件的目录，约定文件名为 `<year>.html`：

```
<data_dir>/
├── 2022.html
├── 2023.html
├── 2024.html
└── 2025.html
```

不需要其他子目录，不需要 metadata 文件。`<year>` 必须是 4 位数字 + `.html`，其他文件忽略。

工作产物（中间态 + 最终输出）默认放在 `<data_dir>/_walk_the_talk/`：

```
<data_dir>/
├── 2022.html, 2023.html, ...                # 用户提供的输入
└── _walk_the_talk/
    ├── reports/                              # Chroma 持久化目录
    ├── financials.db                         # SQLite，结构化财务数据
    ├── claims.json                           # Phase 2 输出
    ├── verdicts.json                         # Phase 3 输出
    ├── checkpoints.db                        # LangGraph SqliteSaver
    └── reports/                              # 最终 markdown 报告
        ├── history.md
        └── credibility.md
```

### 0.4 HTML 解析的具体处理

正文流程（`ingest/html_loader.py`）：

```python
def load_html(path: Path) -> ParsedReport:
    raw = path.read_bytes()
    enc = chardet.detect(raw)["encoding"] or "gbk"
    soup = BeautifulSoup(raw.decode(enc, errors="replace"), "lxml")

    # 去掉脚本/样式/导航
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    content = soup.find("div", id="content")
    if not content:
        raise UnsupportedHtmlLayoutError(path)

    # 1. 把 <table> 单独拎出来 → markdown，避免 get_text 拍扁丢列
    tables: list[Table] = []
    for tbl in content.find_all("table"):
        md = _table_to_markdown(tbl)
        tables.append(Table(index=len(tables), markdown=md, raw_2d=_table_to_2d(tbl)))
        tbl.replace_with(soup.new_string(f"\n{TABLE_PLACEHOLDER}_{len(tables)-1}\n"))

    # 2. 文本流抽出
    text = content.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 3. 章节切分
    sections = _split_sections(text)  # 用 r"第[一二三四五六七八九十]+节[^\d.]"
    return ParsedReport(sections=sections, tables=tables, encoding=enc, ...)
```

要点：

- 编码用 `chardet` 自适应；新浪 95%+ 是 GBK，但留容错位
- **`<table>` 必须在 `get_text` 之前用 `_table_to_markdown` 抽出来**，否则列会被拍成连续行（实测主要踩坑点）
- markdown 表用 `| col1 | col2 | ... |` 形式，LLM 与下游表抽取脚本都能解析；同时保留原始 `raw_2d` 供 `table_extractor.py` 直接消费
- 章节切分用全角"第X节"前缀正则，无歧义
- 用占位符 `TABLE_PLACEHOLDER_N` 在文本流里标记表所在位置，方便 chunk 阶段把表绑回原章节
- "致股东的信"内含管理层使用的 hedging 词词典（"相信/预期/打算/估计/预计/预测/指标/展望/继续/应该/或许/寻求/应当/计划/可能/愿景/目标/旨在/渴望/目的/预定/前景"），**`extract/prompts.py` 的 hedging_words 字段可以从此处自动同步**，覆盖度比硬编码更全
- 校验失败（如 `div#content` 缺失、章节数 < 5 等）抛 `UnsupportedHtmlLayoutError`，由 ingest 主流程跳过该年份并记录到错误日志，不阻塞其他年份

---

## 一、顶层架构：三段式独立 Phase

```
┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐
│  Ingest      │ -> │  Extract     │ -> │  Verify (RAG agent)  │
│  (reports)   │    │  (claims)    │    │  (verdicts)          │
└──────────────┘    └──────────────┘    └──────────────────────┘
        │                  │                       │
        v                  v                       v
   reports/           claims.json            verdicts.json
   financials.db      period_facts.json
```

每个 phase 独立可重跑：

- 改 prompt → 只重跑 Extract
- 改 verifier 工具 / 提示词 → 只重跑 Verify
- 加新年份的年报 → 增量 Ingest，再增量 Extract + Verify

**LangGraph 用法**：把 LangGraph 下沉到每个 Phase 内部作为遍历器/编排器：

- 顶层是三个独立 CLI 子命令（`ingest` / `extract` / `verify`），靠落盘文件解耦
- 每个 Phase 内部用 LangGraph 状态机驱动遍历（年报 / chunk / claim）
- LangGraph 的 `Send()` + Semaphore 做受限并发，保护本地资源（embedding 推理、Chroma 写入）和 API 速率
- LangGraph checkpointer 用 **SQLite（`SqliteSaver`）**，落盘到 `<data_dir>/_walk_the_talk/checkpoints.db`，被打断后能从最后一份完成的年报 / chunk / claim 接上（一行配置即可换 in-memory，本地调试时方便）

---

## 二、仓库目录

```
walk-the-talk/
├── pyproject.toml
├── README.md
├── CLAUDE.md                       # 项目上下文
├── config.yaml
├── .env

├── cli.py                          # 子命令入口：ingest / extract / verify / report
│
├── core/
│   ├── models.py                   # Pydantic：Claim / PeriodFact / Chunk / Table / TableRow / Verdict / ParsedReport
│   ├── ids.py                      # canonical_key、claim_id 生成
│   └── enums.py                    # claim_type、metric、verdict 等枚举
│
├── orchestration/
│   ├── checkpointer.py             # LangGraph SqliteSaver 共享配置
│   └── concurrency.py              # Send()/Semaphore 包装、速率限制
│
├── ingest/
│   ├── graph.py                    # LangGraph：年报遍历状态机
│   ├── html_loader.py              # BeautifulSoup + GBK 转码 + <table> 抽离
│   ├── _table.py                   # <table> → markdown / 2D 数组 工具
│   ├── chunker.py                  # 按章节切，保留表格占位符位置
│   ├── table_extractor.py          # 三大财务表 + 关键附注的结构化解析
│   ├── embedding.py                # BGE-small-zh 包装
│   ├── reports_store.py            # Chroma 写入 + BM25 索引
│   └── financials_store.py         # SQLite 三大表
│
├── extract/
│   ├── graph.py                    # LangGraph：per-chunk 分类 + 抽取 + 后处理
│   ├── classifier.py               # 便宜模型：每个 chunk 是否含前瞻
│   ├── extractor.py                # deepseek-chat 主调 + reasoner 降级
│   ├── prompts.py
│   └── postprocess.py              # 去重、trivial 过滤、canonical_key 归一化
│
├── verify/
│   ├── outer_graph.py              # LangGraph：claim 队列遍历 + 受限 fan-out
│   ├── agent_graph.py              # LangGraph 子图：ReAct 循环
│   ├── tools.py                    # retrieve / query_table / list_items / compute / submit_verdict
│   └── prompts.py
│
├── report/
│   ├── history_report.py           # 历年发展简史
│   └── credibility_report.py       # 管理层可信度报告（兑现率等）
│
├── llm/
│   ├── client.py                   # OpenAI-compatible 抽象
│   ├── deepseek.py
│   └── retry.py                    # 限速 + 重试
│
└── tests/
    ├── fixtures/
    │   └── 中芯国际/
    │       ├── 2025.html           # 新浪 HTML 原文（GBK，可入版本库，848KB）
    │       └── 2025_expected_sections.json
    ├── test_html_loader.py         # 编码检测、章节切分、<table>→markdown 正确性
    ├── test_table_extractor.py
    ├── test_verifier_agent.py
    └── test_dedup.py
```

注意：

- 不设 `loaders/` 子目录、不设 `fetcher/` 子目录——只有一个输入源（手动下载的新浪 HTML），一个 loader 模块（`html_loader.py`）
- `_table.py` 是 `html_loader` 内部用的工具（前缀下划线表示模块私有）
- fixture 用 HTML 文件（< 1MB，可直接入版本库）

---

## 三、数据模型（4 个核心 store）

### 3.1 reports/（Chroma 持久化目录，年报文本 chunk）

```python
class Chunk(BaseModel):
    chunk_id: str                    # ticker-FY-section-seq
    ticker: str
    fiscal_period: str               # FY2024（v2 加 2024Q3 等）
    report_type: Literal["annual"]   # v2 加 semi/q1/q2/q3
    section: str                     # 章节名（标准化后）
    section_canonical: str           # mgmt_letter / mda / outlook / risk / notes / esg / governance / legal_template
    source_path: str                 # 原 HTML 文件路径
    locator: str                     # 章节名 + 段落序号，如 "第二节致股东的信#3"
    text: str
    embedding: list[float]           # BGE-small-zh, 512 维
    is_forward_looking: bool | None  # 由 classifier 填，None=未分类
    contains_table_refs: list[str]   # 该 chunk 内包含的 TABLE_PLACEHOLDER_N 列表
```

`contains_table_refs` 让 verifier 在检索到 chunk 时知道顺手把哪几张表也拉出来一起看。

BM25 索引另建（用 `rank_bm25` + jieba 分词，索引文件与 Chroma 持久化目录并列存储）。

### 3.2 financials.db（SQLite，结构化财务数据）

设计成宽表，按 `(ticker, fiscal_period, statement_type, line_item)` 索引：

```sql
CREATE TABLE financial_lines (
    ticker          TEXT,
    fiscal_period   TEXT,           -- FY2024
    statement_type  TEXT,           -- income / balance / cashflow / segment / rd / capex
    line_item       TEXT,           -- 营业收入 / 营业成本 / 折旧 / ...
    line_item_canonical TEXT,       -- 归一化：revenue / cogs / depreciation
    value           REAL,
    unit            TEXT,           -- 元 / 千元 / 百万元（写库前已归一为元）
    is_consolidated BOOLEAN,        -- 合并 vs 母公司
    source_path     TEXT,           -- 原 HTML 文件路径
    source_locator  TEXT,           -- table_index + row_index，如 "table_12#row_5"
    PRIMARY KEY (ticker, fiscal_period, statement_type, line_item, is_consolidated)
);
```

`line_item_canonical` 是关键。建一张映射表（"主营业务收入" → revenue, "营业总收入" → total_revenue 等），verifier 工具按 canonical 查询，不依赖原文写法。

**表抽取流程**：

1. `ingest/html_loader.py` 把 `<table>` 转成 `Table(index, markdown, raw_2d=[[headers...], [row1...], ...])`
2. `ingest/table_extractor.py` 用关键字匹配头行 + 第一列，识别这张表是利润表 / 资产负债表 / 现金流量表 / 分部信息 / ...
3. 单元格数字按 `unit` 转成统一计量（千元 / 百万元 → 元）
4. 写入 `financial_lines`

HTML `<table>` 二维直读完全规避了 PDF 路径下的 OCR / 列对齐问题。

> 已知风险：跨页大表在新浪 HTML 里偶尔会被切成相邻的两个 `<table>`（按"待续"标识）。`table_extractor.py` 需要做相邻表合并的启发式（看头行是否一致 + 第一列是否连续）。

### 3.3 claims.json（前瞻断言）

```jsonc
{
  "company_name": "中芯国际",
  "ticker": "688981",
  "years_processed": [2022, 2023, 2024, 2025],
  "claims": {
    "<ticker>-FY<year>-<seq>": {
       "claim_id": "...",
       "claim_type": "quantitative_forecast | strategic_commitment | capital_allocation | risk_assessment | qualitative_judgment",
       "section": "原文章节名",
       "section_canonical": "...",
       "speaker": "董事长 | 总经理 | 管理层 | 董事会 | 未明确",
       "original_text": "原文摘录",
       "locator": "章节名 + 段落序号",
       "subject": { "scope": "整体|业务板块|...", "name": "..." },
       "metric": "...",
       "metric_canonical": "...",
       "predicate": { "operator": ">=|<=|=|≈|趋势|完成|启动|暂缓", "value": "...", "unit": "..." },
       "horizon":   { "type": "明确日期|财年|...", "start": "FY2024", "end": "FY2024" },
       "conditions": "前提条件",
       "hedging_words": ["预计", "力争"],
       "specificity_score": 1,
       "verifiability_score": 1,
       "materiality_score": 1,
       "extraction_confidence": 0.9,
       "from_fiscal_year": 2024,
       "canonical_key": "metric_canonical|subject_canonical|FY2024~FY2024",
       "verification_plan": {
         "required_line_items": ["revenue", "rd_expense"],
         "computation": "rd_expense / revenue",
         "comparison": ">= 0.08"
       },
       "status": "open | verified | partially_verified | failed | not_verifiable | expired",
       "verifications": []  // 由 Phase 3 填
    }
  }
}
```

设计要点：

- `claim_type` 只有 5 类前瞻枚举，无 `historical_disclosure` 这种逃生口
- `canonical_key` 用于同年/跨年去重
- `verification_plan` 在 extract 阶段就产出（粗），Phase 3 verifier 直接照着执行
- 不抽"历史事实"——当期已发生的财务/经营数据不构成 claim，只作为后续年份验证别人 claim 时的"对账数据源"（直接进 `financials.db`）

### 3.4 period_facts.json（当期事实，与 claims 解耦）

仅在需要"对账数据源不在 financials.db 里"时才建（比如管理层在董事长致辞里口头说的"前三季度营收同比 +30%"）。99% 情况下 verifier 直接从 `financials.db` 取数据，不需要这张表。

**v1 完全不建**，先看 verifier 实跑下来 financials.db 覆盖率怎么样，再决定要不要补。

### 3.5 verdicts.json（验证结果）

每条 claim 一份验证记录列表：

```python
class Verdict(BaseModel):
    claim_id: str
    verified_at_period: str          # FY2024
    verdict: Literal["verified", "partially_verified", "failed", "not_verifiable", "expired"]
    target_value: Any
    actual_value: Any
    computation_trace: list[ToolCall]   # agent 的工具调用历史，可审计
    evidence_quotes: list[Evidence]
    confidence: float
    cost: dict                       # tokens / api_calls，方便回溯成本
```

`computation_trace` 把 agent 每一轮 retrieve / query_table / compute 的输入输出存下来。出现误判时能 replay。

---

## 四、Phase 1：Ingest（详细）

输入：`<data_dir>/<year>.html`（多份）
输出：`<data_dir>/_walk_the_talk/reports/`（Chroma） + `<data_dir>/_walk_the_talk/financials.db`

**LangGraph 节点图**（顺序遍历年报，每份处理完再进下一份，避免本地内存峰值）：

```
discover_html ──→ has_more? ──yes──→ load_html → split_sections → chunk_text
                                              ├→ embed_batch    → write_chunks
                                              ├→ extract_tables → write_tables
                                              └→ advance ↺
                              ──no──→ build_bm25_index → END
```

State：`html_queue: list[(year, path)]`、`cursor: int`、`current_chunks`、`current_tables`、`errors`。
Checkpointer 把 cursor 存到 `<data_dir>/_walk_the_talk/checkpoints.db`（SQLite），被打断后下次启动从未完成的那份年报继续。

步骤：

1. **报告发现**：`glob(<data_dir>/[0-9][0-9][0-9][0-9].html)`，按年份排序，输出 `(year, path)` 队列。非 4 位数字命名的 HTML 忽略并 warning。
2. **HTML 解析**（`html_loader.py`）：见 §0.4。输出 `ParsedReport(sections, tables, meta)`。失败抛 `UnsupportedHtmlLayoutError`，主流程跳过该年份并记录。
3. **章节归一化分类**：手维护一张映射表，把"董事长致辞"、"主席函"、"董事长报告"统一映射到 `mgmt_letter`。HTML 路径下章节名稳定（来自正文标题），映射表维护成本低。
4. **文本 chunk**：按章节切，章节过长（>3000 字）按段落进一步切。chunk 元数据记录所属章节 + 含哪些 `TABLE_PLACEHOLDER_N`。
5. **Embedding**：BGE-small-zh（512 维，本地 CPU，批量编码）。
6. **BM25 索引**：基于 jieba 分词，`rank_bm25` 实现。索引文件落盘 `<data_dir>/_walk_the_talk/reports/bm25.pkl`。
7. **结构化表抽取**：
   - 直接消费 `html_loader` 输出的 `Table` 对象（二维数组 + 表头），无需 camelot/tabula
   - 用关键字匹配（"营业收入"、"资产总计"、"经营活动产生的现金流量净额"等）识别表类型
   - 单位归一化 → 元
   - 跨页表合并（启发式：相邻 `<table>` 头行一致即合并）
   - 写入 `financials.db`

---

## 五、Phase 2：Extract（详细）

输入：`<data_dir>/_walk_the_talk/reports/`
输出：`<data_dir>/_walk_the_talk/claims.json`

**LangGraph 节点图**（每个 chunk 是一个独立任务，用 `Send()` 受限 fan-out）：

```
load_chunks → fan_out (sem=N) ──→ classifier ──→ is_forward? ──no──→ skip
                                                              ──yes─→ extract_with_chat
                                                                    → schema_valid?
                                                                       ──no─→ retry_with_reasoner
                                                                       ──yes→ collect ↺
                                              ↘ fan_in → postprocess_dedup → write_claims → END
```

`Send()` 把每个 chunk 发到子节点，Semaphore 控制最大并发（默认 N=10，避开 DeepSeek 速率限制）。
Schema 校验失败的两级降级也是图里的边，不需要在 Python 里手写 try/except。

步骤：

1. **拉取候选 chunks**：`section_canonical IN ('mgmt_letter', 'mda', 'outlook', 'risk', 'guidance')`。这一步过滤就直接砍掉法律附注 / ESG 等噪声章节。
2. **前瞻分类**（每 chunk 一次小模型调用）：
   - prompt：给 chunk 文本，问"是否含管理层对未来的判断/预测/承诺"，返回 yes/no + 一句话理由
   - 模型：deepseek-chat（或本地 Qwen2.5-7B 走 OpenAI 兼容 API）
   - 缓存：`(chunk_id, prompt_version) → result`
3. **Claim 抽取**（每个 yes-chunk 一次 deepseek-chat 调用）：
   - 5 类前瞻 claim_type，无 `historical_disclosure` 旁路
   - 输出包含 `verification_plan`（粗）：要 verify 这条 claim 需要查哪些 line_item、怎么计算。verifier 第一轮通过 `list_available_line_items` 自检，字段不存在时再 reasoner 修正
   - prompt 原则首条："只抽前瞻性内容（管理层对未来的判断/预测/承诺）"，并附排除清单 + 正反例
   - **hedging_words 字段**直接从"致股东的信"里管理层自报的词典同步
   - **两级降级**：chat 输出 schema 校验失败 → 同 chunk 用 deepseek-reasoner 重试一次
4. **后处理**：
   - 同年内按 canonical_key 去重，保留 specificity 最高的
   - 跨年法律样板指纹去重
   - `horizon.end ≤ from_fiscal_year` 硬过滤（兜底——抽到了当期已发生的事实，丢弃）
   - section 黑名单兜底
   - trivial 阈值过滤（specificity ≤ 2 且 materiality ≤ 2）
5. **写入 claims.json**：保留 canonical_key 索引，方便 Phase 3 查找。

---

## 六、Phase 3：Verify（agent 设计）

输入：`<data_dir>/_walk_the_talk/claims.json`（status=open）+ `reports/` + `financials.db`
输出：`<data_dir>/_walk_the_talk/verdicts.json`

### 6.1 双层 LangGraph 设计

外层：claim 队列遍历图（`outer_graph.py`）

```
load_open_claims → fan_out (sem=10) ──→ [agent sub-graph] ──→ fan_in → write_verdicts → END
```

内层：ReAct agent 子图（`agent_graph.py`）

```
plan(reasoner)               # 第一轮基于 verification_plan 决定首个 action
   ↓
execute_tool                 # 路由到具体 tool 节点
   ↓
tool_router ─→ retrieve ──┐
            ─→ query_table ┤
            ─→ list_items  ┼──→ next_step?  ──continue──→ execute_tool ↺ (max 5 rounds)
            ─→ compute ────┘                ──done──────→ synthesize(reasoner)
                                                              ↓
                                                       submit_verdict → END (sub-graph)

# 超过 5 轮：not_verifiable 兜底分支
```

State：`claim`、`plan`、`history: list[ToolCall]`、`round_count`。
退出条件由 `next_step?` 节点的 reasoner 输出（`continue` / `done` / `give_up`）决定。
所有数值比较和算术由 `compute` 工具节点完成，LLM 只决定调用什么工具。

### 6.2 工具集

```python
@tool
def retrieve(query: str, ticker: str, fiscal_period: str | None,
             section_filter: list[str] | None, top_k: int = 5) -> list[Chunk]:
    """语义 + BM25 hybrid 检索。fiscal_period=None 表示跨年检索。
    返回的 Chunk 含 contains_table_refs，可顺手拉取相关表。"""

@tool
def query_table(ticker: str, fiscal_period: str, line_item_canonical: str,
                statement_type: str | None = None, is_consolidated: bool = True) -> TableRow | None:
    """从 financials.db 精确查询一行财务数据。"""

@tool
def list_available_line_items(ticker: str, fiscal_period: str) -> list[str]:
    """列出该期可查询的 line_item_canonical 列表，避免 agent 瞎猜字段名。"""

@tool
def compute(expression: str, variables: dict[str, float]) -> float:
    """安全执行算术表达式。例：'a + b - c', {'a': 100, 'b': 50, 'c': 30}。"""

@tool
def submit_verdict(verdict: Literal[...], target_value, actual_value,
                   evidence: list, confidence: float, comment: str) -> None:
    """终止循环并落盘。"""
```

> 关键设计：**所有数值比较和算术由 `compute` 工具完成，LLM 只决定调用什么工具**。这能消除大部分数值幻觉。

### 6.3 两阶段执行（成本优化）

每条 claim 实际是这样：

```
Round 0: planner (reasoner)   → 产出 verification_plan
Rounds 1-4: executor (chat)   → 按 plan 调工具，遇到分歧或边界再升级到 reasoner
Round final: synthesizer (reasoner) → 综合证据，提交 verdict
```

平均每条 claim：1 次 reasoner 计划 + 2-3 次 chat 执行 + 1 次 reasoner 总结 ≈ 2 reasoner + 3 chat。比纯 reasoner 5 轮便宜很多。

### 6.4 并发设计（LangGraph 内）

外层图用 `Send()` 把每个 claim 发给 agent sub-graph，Semaphore 限制最大并发：

```python
def fan_out(state: OuterState) -> list[Send]:
    return [Send("agent_subgraph", {"claim": c, ...}) for c in state["open_claims"]]

# Semaphore 通过 RunnableConfig.max_concurrency 或自定义中间件限制
```

注意点：

- DeepSeek 并发上限取决于套餐，默认 10 起步，遇 429 由 LangGraph 重试节点指数退避
- 检索（BGE-small-zh + Chroma）是 CPU bound：本地并发查询会争 CPU，retrieve 工具内部用线程池（`ThreadPoolExecutor(max_workers=4)`）防止 oversubscription
- compute 工具即时，不计成本
- 单条 claim 整体超时设 5 分钟（5 轮 × 60 秒），超时由 LangGraph `interrupt` 终止子图，主图收到 `not_verifiable` verdict

---

## 七、v1（本次实现）vs v2（未来扩展）

| 项 | v1 | v2 |
|---|---|---|
| 报告类型 | 年报 | + 季报、半年报 |
| verification window | 单一 fiscal_period | partial（季报）→ final（年报）滚动验证 |
| claims 抽取范围 | 财务 + 战略 + 风险 | + 业绩说明会、电话会议纪要 |
| RAG 索引 | BGE-zh + 本地 BM25 | + reranker（如 BGE-reranker） |
| 多公司 | 单公司一套库 | 跨公司同业对比 |
| `period_facts.json` | 视 financials.db 覆盖率决定建不建 | 必建 |

---

## 八、开发顺序建议

1. **仓库脚手架 + core/models.py + cli.py 骨架 + orchestration/checkpointer.py**（半天）
2. **ingest/html_loader.py + _table.py + chunker + 章节归一化映射表**（1-2 天）—— 拿用户已下载的中芯国际 4 份 HTML 直接做 fixture
3. **ingest/embedding + reports_store**（半天）—— BGE-small-zh + Chroma 接进来
4. **ingest/graph.py（LangGraph 年报遍历）+ 跑通中芯国际 4 年**（半天）
5. **ingest/table_extractor 三大表（HTML 路径直读）**（1-2 天）—— `<table>` 二维直读，无需 OCR
6. **verify/tools.py + verify/agent_graph.py 最小骨架**（1 天）—— 先在 reports/ + financials.db 上手跑 1-2 条手工 claim
7. **extract/classifier + extractor + extract/graph.py + postprocess**（2 天）
8. **verify/outer_graph.py + 全量跑通**（1 天）
9. **report/history_report + credibility_report**（半天）

总体 **≈ 1.5 周**做完 v1 主链路，用中芯国际做端到端回归。

---

## 九、决策落地记录

1. **embedding 模型尺寸**：定 **BGE-small-zh**（512 维，~100MB）。本地 CPU 验证优先，召回不够再升 v1.5。
2. **vector store 选 LanceDB 还是 Chroma**：定 **Chroma**。求职考虑（国内招聘描述里出现频率更高），技术上在本项目数据量级差异可忽略。
3. **verification_plan 产出阶段**：定 **extract 阶段产出粗 plan**。verifier 第一轮通过 `list_available_line_items` 自检字段名，错了再 reasoner 修正。extract 主调用走 deepseek-chat，schema 校验失败两级降级到 reasoner。
4. **GitHub 仓库名 + 本地路径**：定 `walk-the-talk`，本地路径 `/Users/alfy/Desktop/workspace/walk-the-talk`。
5. **测试 fixture**：v1 主链路跑通后补 smoke test（`html_loader.py` 和 `table_extractor.py`），断言一条已知值（如中芯国际 FY2024 营收）防解析静默崩坏。HTML 文件体积小可直接入版本库。
6. **LangGraph checkpointer**：定 **SQLite（`SqliteSaver`）**，文件 `<data_dir>/_walk_the_talk/checkpoints.db`。开发期想纯内存只需把 `orchestration/checkpointer.py` 切到 `MemorySaver`，整体代码不变。
7. **输入格式**：定 **HTML（新浪财经 `vCB_AllBulletinDetail.php`）**。理由见 §零；实证依据：中芯国际 FY2025 同份年报的 PDF / HTML 提取对比，HTML 在章节切分、段落完整性、噪音剔除、表格保真四维度均胜出。
8. **数据获取方式**：**手动下载**，不内置爬虫/fetcher。反爬策略与 IP 频控不可控，徒增维护成本。
9. **输入约定**：调用 `walk-the-talk ingest <data_dir>`，`<data_dir>` 直接放 `<year>.html`（如 `2024.html`），无需嵌套子目录、无需 metadata 文件。

---

## 十、成本估算（粗算，按中芯国际 4 年量级）

| 阶段 | 单次调用 token 量 | 调用次数 | 模型 | 估算成本 |
|---|---|---|---|---|
| classifier | ~2K in / 200 out | ~350 | deepseek-chat | ¥0.25 |
| extractor | ~2.5K in / 1.5K out | ~80 | deepseek-chat | ¥0.4–0.8 |
| verifier (planner+synth) | ~3K in / 1K out | ~70 | deepseek-reasoner | ¥1.5–2 |
| verifier (executor) | ~3K in / 500 out | ~100 | deepseek-chat | ¥0.5 |
| **合计** | | | | **¥2.7–4 / 公司·全量跑** |

按 ¥1/M input + ¥2/M output（chat）和 ¥4/M + ¥16/M（reasoner）粗估，缓存命中后还能更便宜。50 公司同业对比量级 ≈ ¥135–200。

---

> 设计基线已就位。开始实现时直接进 §八 的开发顺序拆 task。**§八 第 2 步**（`html_loader.py`）的 fixture 已经有了——用户当前目录下的 `2025.html`（中芯国际 FY2025 年报全文，848KB GBK），可以直接拿来跑通第一份 `ParsedReport`，作为下游所有 Phase 的种子数据。
