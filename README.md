# walk-the-talk

回溯上市公司历年年报中管理层做出的可验证断言（claim），用后续年份的事实回头打分，量化"管理层是否说到做到"。

## 输入约定

调用时传入一个目录路径，目录下放 `<year>.html`（手动从新浪财经下载的年报全文页）：

```
<data_dir>/
├── 2022.html
├── 2023.html
├── 2024.html
└── 2025.html
```

工作产物（中间态 + 最终输出）落在 `<data_dir>/_walk_the_talk/`。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 使用

```bash
# Phase 1: 解析 HTML、抽 chunk、落 financials.db
walk-the-talk ingest /path/to/data_dir --ticker 688981 --company "中芯国际"

# Phase 2: 抽前瞻 claim（需 .env 里配 DEEPSEEK_API_KEY）
walk-the-talk extract /path/to/data_dir

# Phase 3: 用后续年份事实校验 claim
walk-the-talk verify /path/to/data_dir

# Phase 4: 生成最终 markdown 报告
walk-the-talk report /path/to/data_dir
```

详细设计见仓库根的 `design.md`。
