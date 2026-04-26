"""ID / key 生成 helpers：chunk_id / claim_id / canonical_key / fingerprint。

设计原则：
- 全部是纯函数，零 I/O；可在任何 phase 自由调用。
- ID 字符串保持人类可读（``688981-FY2024-001`` 而不是 UUID），便于在
  ``claims.json`` / ``verdicts.json`` 里肉眼追踪同一条 claim。
- canonical_key 用于跨 chunk / 跨年同义 claim 的去重——不是主键，但保证
  同主体 + 同 metric + 同 horizon 的 claim 落到同一个 key。
"""

from __future__ import annotations

import hashlib
import re

__all__ = [
    "canonical_key",
    "chunk_id",
    "claim_id",
    "fiscal_period",
    "slug",
    "text_fingerprint",
]


# 仅保留拉丁字母、数字、下划线、短横线、中日韩统一表意文字（含中文）。
# 其余（标点、空白、emoji 等）压成单个下划线。
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9一-龥_-]+")


def slug(s: str, max_len: int = 24) -> str:
    """把任意字符串截成可放进 ID 片段的安全 slug。

    Args:
        s: 任意来源字符串（可含中文）。
        max_len: 截断长度，默认 24 个字符。

    Returns:
        安全 slug；前后下划线已 strip。空输入返回空字符串。
    """
    s = _SAFE_CHARS.sub("_", s.strip())
    return s[:max_len].strip("_")


def fiscal_period(year: int) -> str:
    """统一财年格式：``2024 → "FY2024"``。"""
    return f"FY{year}"


def chunk_id(ticker: str, year: int, section_seq: int, paragraph_seq: int = 0) -> str:
    """构造 chunk_id：``<ticker>-FY<year>-sec<NN>-p<NNN>``。

    Args:
        ticker: 股票代码（A 股 6 位数字）。
        year: 财年。
        section_seq: 该年报内的章节序号（0-based）。
        paragraph_seq: 章节内段落序号（0-based）。

    Returns:
        例：``"688981-FY2024-sec03-p012"``。
    """
    return f"{ticker}-FY{year}-sec{section_seq:02d}-p{paragraph_seq:03d}"


def claim_id(ticker: str, year: int, seq: int) -> str:
    """构造 claim_id：``<ticker>-FY<year>-<NNN>``。

    seq 是该年报内 claim 的全局序号（跨 chunk 累加）。
    例：``"688981-FY2022-005"``。
    """
    return f"{ticker}-FY{year}-{seq:03d}"


def canonical_key(
    metric_canonical: str,
    subject_canonical: str,
    horizon_start: str,
    horizon_end: str,
) -> str:
    """同年 / 跨年 claim 去重的 key（不是主键，仅用于聚合）。

    形如 ``"revenue|整体|FY2024~FY2024"``——同 metric + 同 subject + 同 horizon
    的 claim 应聚到同一个 key。
    """
    return f"{metric_canonical}|{subject_canonical}|{horizon_start}~{horizon_end}"


def text_fingerprint(text: str, length: int = 16) -> str:
    """跨年法律样板文本的去重指纹（SHA-256 前缀）。

    年报里"本公司董事会保证..." 这类样板每年原文不变，需要稳定 hash 来
    跨年识别。``length`` 默认 16 hex（64 bit），冲突概率可忽略。
    """
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return h[:length]
