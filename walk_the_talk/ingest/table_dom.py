"""Table HTML 辅助函数：<table> → 2D list & markdown。

不处理 colspan / rowspan 的多行合并逻辑（年报里 99% 是规则表）。
有需要的话后续在 table_extractor.py 里做更精细的解构。
"""

from __future__ import annotations

from bs4 import Tag


def _cell_text(cell: Tag) -> str:
    """提取单元格文本，单格内多行折成空格。"""
    return cell.get_text(separator=" ", strip=True)


def table_to_2d(table: Tag) -> list[list[str]]:
    """把 <table> 拆成二维数组。空行被丢弃。"""
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        row = [_cell_text(c) for c in cells]
        if any(s for s in row):  # 整行空就丢
            rows.append(row)
    return rows


def table_to_markdown(table: Tag) -> str:
    """把 <table> 转成 GitHub flavored markdown 表格。

    第 0 行作表头；列数不齐时按最大列数右补空。
    单元格里的 `|` 转义为 `\\|`，`\\n` 折成空格。
    """
    rows = table_to_2d(table)
    if not rows:
        return ""

    n_cols = max(len(r) for r in rows)
    norm = [r + [""] * (n_cols - len(r)) for r in rows]

    header = norm[0]
    body = norm[1:]

    def _line(cells: list[str]) -> str:
        safe = [c.replace("|", "\\|").replace("\n", " ").strip() for c in cells]
        return "| " + " | ".join(safe) + " |"

    lines = [_line(header), "|" + "|".join(["---"] * n_cols) + "|"]
    for r in body:
        lines.append(_line(r))
    return "\n".join(lines)
