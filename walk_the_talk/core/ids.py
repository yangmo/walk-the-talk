"""ID 生成：chunk_id / claim_id / canonical_key 等。"""

from __future__ import annotations

import hashlib
import re

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9\u4e00-\u9fa5_-]+")


def _slug(s: str, max_len: int = 24) -> str:
    """把任意字符串截成可放进 ID 的安全片段。"""
    s = _SAFE_CHARS.sub("_", s.strip())
    return s[:max_len].strip("_")


def fiscal_period(year: int) -> str:
    """统一格式：FY2024。"""
    return f"FY{year}"


def chunk_id(ticker: str, year: int, section_seq: int, paragraph_seq: int = 0) -> str:
    """chunk_id = ticker-FYyear-secNN-pNN。section_seq 是该年报内章节序号，paragraph_seq 是章节内段落序号。"""
    return f"{ticker}-FY{year}-sec{section_seq:02d}-p{paragraph_seq:03d}"


def claim_id(ticker: str, year: int, seq: int) -> str:
    """claim_id = ticker-FYyear-NNN，seq 是该年内 claim 序号。"""
    return f"{ticker}-FY{year}-{seq:03d}"


def canonical_key(metric_canonical: str, subject_canonical: str, horizon_start: str, horizon_end: str) -> str:
    """同年/跨年 claim 去重的 key。"""
    return f"{metric_canonical}|{subject_canonical}|{horizon_start}~{horizon_end}"


def text_fingerprint(text: str, length: int = 16) -> str:
    """跨年法律样板文本去重指纹。"""
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return h[:length]


def section_seq_id(section_seq: int) -> str:
    """章节内 chunk 编号前缀（仅用于内部）。"""
    return f"sec{section_seq:02d}"
