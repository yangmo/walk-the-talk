"""HTML 加载器：把新浪财经 vCB_AllBulletinDetail.php 风格的年报 HTML 解析成 ParsedReport。

关键设计：
1. **`<table>` 必须在 `get_text()` 之前抽离**，并替换成 `[[TABLE_PLACEHOLDER_N]]` 占位符。
   否则表格列会被 `get_text` 拼成无意义的连续文本，下游再也对不上账。
2. 章节切分基于 `第X节<title>` 行级匹配，过滤目录页与正文中的交叉引用。
3. 编码用 chardet 检测，新浪 99% 是 GBK / GB2312（统一升 GBK）。
"""

from __future__ import annotations

import re
from pathlib import Path

import chardet
from bs4 import BeautifulSoup

from ..core.enums import ReportType, SectionCanonical
from ..core.models import ParsedReport, Section, Table
from ._table import table_to_2d, table_to_markdown

# ============== 常量 ==============

# 候选正文容器，按优先级排列。新浪页面 99% 是 div#content。
CONTENT_SELECTORS: tuple[str, ...] = ("div#content", "div.tagmain", "div#con02-7")

# 行首匹配 "第N节<title>"。后续再用语义过滤剔除 TOC / 交叉引用。
SECTION_HEADER_RE = re.compile(
    r"^(第[一二三四五六七八九十]+节)([^\n]*)$",
    re.MULTILINE,
)

# 占位符正则：用于回填章节内的 table_refs。
TABLE_PLACEHOLDER_RE = re.compile(r"\[\[TABLE_PLACEHOLDER_(\d+)\]\]")

# 章节标题最长字数：超过基本不是真标题（多半是交叉引用整段）。
MAX_SECTION_TITLE_LEN = 30


class UnsupportedHtmlLayoutError(RuntimeError):
    """HTML 不符合预期布局（找不到正文容器等）。"""


# ============== 编码检测 ==============


def _detect_encoding(raw: bytes) -> str:
    """chardet 探测，GB 系列统一升 GBK。失败回退 GBK。"""
    detected = chardet.detect(raw[:200_000]) or {}
    enc = (detected.get("encoding") or "gbk").lower()
    if enc in ("gb2312", "gbk", "gb18030"):
        return "gbk"
    if enc.startswith("utf"):
        return "utf-8"
    return enc


# ============== DOM 处理 ==============


def _find_content(soup: BeautifulSoup):
    for sel in CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 1000:
            return el
    raise UnsupportedHtmlLayoutError(
        f"找不到正文容器，已尝试: {CONTENT_SELECTORS}"
    )


def _strip_noise(content) -> None:
    for el in content(["script", "style", "noscript", "iframe"]):
        el.decompose()


_CAPTION_MAX_CHARS = 500
_CAPTION_BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "table")


def _capture_caption(tb, max_chars: int = _CAPTION_MAX_CHARS) -> str:
    """收集 <table> 之前直到上一个 <table> / 容器顶之间的 <p>/<hN> 文字。

    "单位：千元" / "合并资产负债表" 这类表头说明通常以 <p> 形式紧邻 <table>，
    `get_text()` 之后混入正文里，下游就找不回归属。所以在 _extract_tables 阶段
    主动抓 caption 存到 Table.caption。
    """
    paragraphs: list[str] = []
    total = 0
    # find_all_previous 按文档反序返回；遇到上一个 <table> 即停止。
    for el in tb.find_all_previous(_CAPTION_BLOCK_TAGS):
        if el.name == "table":
            break
        txt = el.get_text(separator=" ", strip=True)
        if not txt:
            continue
        paragraphs.append(txt)
        total += len(txt)
        if total >= max_chars:
            break
    # 反转回文档顺序，截取末尾 max_chars
    return " ".join(reversed(paragraphs))[-max_chars:]


def _extract_tables(content, soup) -> list[Table]:
    """先抽出所有 <table>，再原地替换为 `[[TABLE_PLACEHOLDER_N]]` 文本节点。

    抽取顺序：先把所有 <table> 引用 + caption 收集好，再统一 replace_with。
    （边遍历边 replace_with 会让后续 find_all_previous 看到 NavigableString，
    而非原始 <p>，影响 caption 准确度。）
    """
    raw_tables = content.find_all("table")
    captions = [_capture_caption(tb) for tb in raw_tables]

    tables: list[Table] = []
    for i, (tb, caption) in enumerate(zip(raw_tables, captions)):
        anchor = f"TABLE_PLACEHOLDER_{i}"
        try:
            md = table_to_markdown(tb)
            raw_2d = table_to_2d(tb)
        except Exception:
            md, raw_2d = "", []
        tables.append(Table(
            index=i, markdown=md, raw_2d=raw_2d,
            bbox_anchor=anchor, caption=caption,
        ))
        tb.replace_with(soup.new_string(f"\n[[{anchor}]]\n"))
    return tables


# ============== 文本归一化 ==============


def _normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ")  # NBSP
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ============== 章节切分 ==============


def _is_real_section_header(title_part: str) -> bool:
    """判断 `第N节<X>` 行是不是真章节起点。

    剔除：
    - 目录页 entry：`第一节释义 ...... 5`
    - 交叉引用：`第四节管理层讨论与分析"之"...`
    - 包裹引号 / 书名号 / 含 `之` 的引用
    - 含数字（多半是 TOC 页码）
    - 过长（>30 字基本是连段引用）
    """
    s = title_part.strip()
    if not s:
        return False
    if "..." in s or "……" in s:
        return False
    # 引号 / 书名号
    if any(c in s for c in '"》「」“”'):
        return False
    # "之" 是 "第N节X之Y" 这种交叉引用的强信号
    if "之" in s:
        return False
    # TOC 行末通常带页码
    if any(c.isdigit() for c in s):
        return False
    if len(s) > MAX_SECTION_TITLE_LEN:
        return False
    return True


def _split_sections(text: str) -> list[tuple[str, str]]:
    """按真章节起点把全文切片。返回 [(完整标题, body), ...]，body 不含标题行。"""
    matches = list(SECTION_HEADER_RE.finditer(text))
    real: list[tuple[int, str, str]] = []
    seen_numbers: set[str] = set()
    for m in matches:
        prefix = m.group(1)        # "第二节"
        title = m.group(2)         # "致股东的信"
        if not _is_real_section_header(title):
            continue
        if prefix in seen_numbers:
            continue
        seen_numbers.add(prefix)
        real.append((m.start(), prefix, title.strip()))

    sections: list[tuple[str, str]] = []
    for i, (pos, prefix, title) in enumerate(real):
        end = real[i + 1][0] if i + 1 < len(real) else len(text)
        # body 从标题所在行的下一行开始
        line_end = text.find("\n", pos)
        body_start = line_end + 1 if line_end != -1 else pos + len(prefix) + len(title)
        body = text[body_start:end].strip()
        sections.append((f"{prefix}{title}", body))
    return sections


def _table_refs_in(body: str) -> list[str]:
    """章节内出现过的 `TABLE_PLACEHOLDER_N` 锚点。"""
    return [f"TABLE_PLACEHOLDER_{m.group(1)}" for m in TABLE_PLACEHOLDER_RE.finditer(body)]


# ============== 元数据推断 ==============


def _infer_fiscal_year(path: Path, body: str) -> int:
    """优先用文件名 `<year>.html`，回退到正文里的 `20XX年年度报告`。"""
    m = re.match(r"(\d{4})", path.stem)
    if m:
        year = int(m.group(1))
        if 1990 < year < 2100:
            return year
    m = re.search(r"(20\d{2})\s*年年度报告", body)
    if m:
        return int(m.group(1))
    raise ValueError(f"无法从 {path} 推断财年")


def _infer_ticker(body: str) -> str:
    for pat in (r"公司代码[：:]\s*(\d{6})", r"股票代码[：:]\s*(\d{6})"):
        m = re.search(pat, body)
        if m:
            return m.group(1)
    return "UNKNOWN"


# ============== 主入口 ==============


def load_html(path: str | Path) -> ParsedReport:
    """读 `<year>.html`，产出 ParsedReport。

    Raises:
        UnsupportedHtmlLayoutError: 找不到正文容器
        ValueError: 推不出财年
    """
    p = Path(path)
    raw = p.read_bytes()
    encoding = _detect_encoding(raw)
    text = raw.decode(encoding, errors="replace")

    soup = BeautifulSoup(text, "lxml")
    content = _find_content(soup)
    _strip_noise(content)
    tables = _extract_tables(content, soup)
    body_text = _normalize_text(content.get_text(separator="\n"))

    fy = _infer_fiscal_year(p, body_text)
    ticker = _infer_ticker(body_text)

    raw_sections = _split_sections(body_text)
    sections = [
        Section(
            seq=seq,
            title=title,
            canonical=SectionCanonical.OTHER,  # canonical 映射在 chunker 里做
            text=body,
            table_refs=_table_refs_in(body),
        )
        for seq, (title, body) in enumerate(raw_sections)
    ]

    return ParsedReport(
        ticker=ticker,
        fiscal_year=fy,
        report_type=ReportType.ANNUAL,
        source_path=str(p.resolve()),
        encoding=encoding,
        sections=sections,
        tables=tables,
    )
