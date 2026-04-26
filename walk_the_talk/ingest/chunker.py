"""把 ParsedReport 切成 Chunk 列表。

策略：
1. 遍历 ParsedReport 的每个 Section
2. 用 `classify_section()` 给 section 打 canonical 标签
3. 段落级切分：按 `\\n\\n` 切，再按目标长度贪心合并
4. 表格占位符 `[[TABLE_PLACEHOLDER_N]]` 单独成 chunk（不与文本混合，便于下游 verify 时单独检索表格）
5. 超长段（>max）按句号 `。` 软切

调参：
- target_size:  目标长度（贪心合并的"足够停下来"阈值）
- max_size:    单 chunk 最大长度（绝对上限）
- min_size:    末尾余料兜底（比这小就并入上一个 chunk）
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.ids import chunk_id
from ..core.models import Chunk, ParsedReport, Section
from .section_canonical import classify_section

# ============== 调参（按字符数，非 token） ==============
DEFAULT_TARGET_SIZE = 800
DEFAULT_MAX_SIZE = 1500
DEFAULT_MIN_SIZE = 200
# 短"标题段"贴到下一个表格上的最大长度。常见标题如
# "3.研发投入情况表\n单位：千元币种：人民币" ~ 20 字。
TITLE_ATTACH_MAX_LEN = 60

# 表格占位符整段匹配（一行只有占位符）
TABLE_LINE_RE = re.compile(r"^\s*\[\[TABLE_PLACEHOLDER_(\d+)\]\]\s*$")
# 内嵌占位符（用于提取 contains_table_refs）
TABLE_INLINE_RE = re.compile(r"\[\[TABLE_PLACEHOLDER_(\d+)\]\]")


# ============== 数据结构 ==============


@dataclass
class _Buffer:
    """累积段落到目标长度后吐 chunk。"""
    parts: list[str]
    refs: list[str]

    def __init__(self) -> None:
        self.parts = []
        self.refs = []

    @property
    def length(self) -> int:
        return sum(len(p) for p in self.parts) + max(0, len(self.parts) - 1) * 2  # 双换行连接

    def is_empty(self) -> bool:
        return not self.parts

    def add(self, text: str, refs: list[str]) -> None:
        self.parts.append(text)
        self.refs.extend(refs)

    def flush(self) -> tuple[str, list[str]]:
        text = "\n\n".join(self.parts).strip()
        refs = list(dict.fromkeys(self.refs))  # 保序去重
        self.parts.clear()
        self.refs.clear()
        return text, refs


# ============== 切分原语 ==============


def _split_paragraphs(text: str) -> list[str]:
    """按空行切段；同时把"独占一行的表格占位符"也作为单独段落分出来。"""
    text = text.strip()
    if not text:
        return []
    # 第一步按空行切
    raw_paras = re.split(r"\n{2,}", text)
    out: list[str] = []
    for p in raw_paras:
        p = p.strip()
        if not p:
            continue
        # 把段内单独成行的占位符抽出来（防止占位符被埋在长段中）
        lines = p.split("\n")
        buf: list[str] = []

        def _flush_buf() -> None:
            if buf:
                joined = "\n".join(buf).strip()
                if joined:
                    out.append(joined)
                buf.clear()

        for ln in lines:
            if TABLE_LINE_RE.match(ln):
                _flush_buf()
                out.append(ln.strip())
            else:
                buf.append(ln)
        _flush_buf()
    return out


def _split_long_paragraph(para: str, max_size: int) -> list[str]:
    """长段落按句号/分号软切，确保片段 <= max_size。"""
    if len(para) <= max_size:
        return [para]
    pieces: list[str] = []
    # 优先按 "。" 切；切完再合并到 max_size
    sentences = re.split(r"(?<=[。！？；])", para)
    sentences = [s for s in sentences if s.strip()]

    cur = ""
    for s in sentences:
        if len(s) > max_size:
            # 单句仍然超长（罕见），硬切
            if cur:
                pieces.append(cur)
                cur = ""
            for i in range(0, len(s), max_size):
                pieces.append(s[i:i + max_size])
            continue
        if len(cur) + len(s) > max_size:
            if cur:
                pieces.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        pieces.append(cur)
    return pieces


def _refs_in(text: str) -> list[str]:
    return [f"TABLE_PLACEHOLDER_{m.group(1)}" for m in TABLE_INLINE_RE.finditer(text)]


def _is_table_only(text: str) -> bool:
    """整个段落是否只有一个 table 占位符（独立成 chunk）。"""
    return bool(TABLE_LINE_RE.match(text.strip()))


def _looks_like_title(text: str, max_len: int = TITLE_ATTACH_MAX_LEN) -> bool:
    """判断一段是否像表/段标题：短、不以句号结束。"""
    s = text.strip()
    if not s or len(s) > max_len:
        return False
    return s[-1] not in "。！？.?!"


def _attach_titles_to_tables(items: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """把短"标题段"贴到紧邻的下一个表格段前，作为表 caption。

    例：("3.研发投入情况表\n单位：千元", False) + ("[[TABLE_PLACEHOLDER_45]]", True)
        => ("3.研发投入情况表\n单位：千元\n[[TABLE_PLACEHOLDER_45]]", True)

    合并后仍标记为 is_table=True，独立成 chunk（不进贪心 buffer）。
    """
    out: list[tuple[str, bool]] = []
    i = 0
    while i < len(items):
        text, is_table = items[i]
        if (
            not is_table
            and _looks_like_title(text)
            and i + 1 < len(items)
            and items[i + 1][1]  # 下一项是表格
        ):
            merged = text + "\n" + items[i + 1][0]
            out.append((merged, True))
            i += 2
        else:
            out.append((text, is_table))
            i += 1
    return out


# ============== Section -> Chunks ==============


def chunk_section(
    section: Section,
    *,
    ticker: str,
    fiscal_year: int,
    source_path: str,
    section_seq_offset: int = 0,
    target_size: int = DEFAULT_TARGET_SIZE,
    max_size: int = DEFAULT_MAX_SIZE,
    min_size: int = DEFAULT_MIN_SIZE,
) -> list[Chunk]:
    """把单个 Section 切成 chunks。"""
    canonical = classify_section(section.title)
    fiscal_period = f"FY{fiscal_year}"
    paragraphs = _split_paragraphs(section.text)

    if not paragraphs:
        return []

    # 第一步：把段落归类为「文本段 / 表格段」并拆分超长段
    items: list[tuple[str, bool]] = []  # (text, is_table)
    for p in paragraphs:
        if _is_table_only(p):
            items.append((p, True))
        else:
            for piece in _split_long_paragraph(p, max_size):
                items.append((piece, False))

    # 第二步：把短"标题段"贴到下一个表格上（caption 化）
    items = _attach_titles_to_tables(items)

    # 第三步：贪心合并文本段；表格段独立成 chunk
    raw_chunks: list[tuple[str, list[str], bool]] = []  # (text, refs, is_table)
    buf = _Buffer()

    def _flush_buffer() -> None:
        if not buf.is_empty():
            t, r = buf.flush()
            raw_chunks.append((t, r, False))

    for text, is_table in items:
        if is_table:
            _flush_buffer()
            raw_chunks.append((text, _refs_in(text), True))
            continue
        # 文本段
        if buf.is_empty():
            buf.add(text, _refs_in(text))
            if buf.length >= target_size:
                _flush_buffer()
            continue
        if buf.length + len(text) + 2 > max_size:
            _flush_buffer()
            buf.add(text, _refs_in(text))
        else:
            buf.add(text, _refs_in(text))
            if buf.length >= target_size:
                _flush_buffer()

    _flush_buffer()

    # 第四步：末尾余料兜底（最后一个文本 chunk 太短就并入前一个文本 chunk）
    raw_chunks = _merge_trailing_short(raw_chunks, min_size)

    # 第五步：构造 Chunk 模型
    chunks: list[Chunk] = []
    for p_seq, (text, refs, _is_table) in enumerate(raw_chunks):
        cid = chunk_id(
            ticker=ticker,
            year=fiscal_year,
            section_seq=section_seq_offset + section.seq,
            paragraph_seq=p_seq,
        )
        locator = f"{section.title}#{p_seq}"
        chunks.append(Chunk(
            chunk_id=cid,
            ticker=ticker,
            fiscal_period=fiscal_period,
            section=section.title,
            section_canonical=canonical,
            source_path=source_path,
            locator=locator,
            text=text,
            contains_table_refs=refs,
            is_forward_looking=None,  # 留给 Phase 2 classifier 填
        ))
    return chunks


def _merge_trailing_short(
    raw_chunks: list[tuple[str, list[str], bool]],
    min_size: int,
) -> list[tuple[str, list[str], bool]]:
    """末尾文本 chunk 太短，且紧邻的前一个也是文本 chunk，就并入。

    重要：表格 chunk 作为天然屏障，不允许跨越合并。
    """
    if len(raw_chunks) < 2:
        return raw_chunks
    last_text, last_refs, last_is_table = raw_chunks[-1]
    if last_is_table:
        return raw_chunks
    if len(last_text) >= min_size:
        return raw_chunks
    prev_text, prev_refs, prev_is_table = raw_chunks[-2]
    if prev_is_table:
        return raw_chunks  # 紧邻是表格，保留小尾巴独立
    merged = (
        prev_text + "\n\n" + last_text,
        list(dict.fromkeys(prev_refs + last_refs)),
        False,
    )
    return raw_chunks[:-2] + [merged]


# ============== ParsedReport -> Chunks ==============


def chunk_report(
    report: ParsedReport,
    *,
    target_size: int = DEFAULT_TARGET_SIZE,
    max_size: int = DEFAULT_MAX_SIZE,
    min_size: int = DEFAULT_MIN_SIZE,
) -> list[Chunk]:
    """把整个 ParsedReport 切成 chunks（跨章节 chunk_id 全局唯一）。"""
    chunks: list[Chunk] = []
    for section in report.sections:
        chunks.extend(chunk_section(
            section,
            ticker=report.ticker,
            fiscal_year=report.fiscal_year,
            source_path=report.source_path,
            target_size=target_size,
            max_size=max_size,
            min_size=min_size,
        ))
    return chunks
