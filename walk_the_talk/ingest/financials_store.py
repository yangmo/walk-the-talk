"""SQLite 持久化层：FinancialLine → financial_lines 表。

设计：
- 一个公司一行一年一指标：PRIMARY KEY = (ticker, fiscal_period, statement_type,
  line_item_canonical, is_consolidated)。重复 upsert 会 last-wins，正好对付
  HTML 把一张资产负债表拆成多个 <table> 片段的情况。
- 单值读取用 get_value，聚合查询用 query / get_series。
- Phase 3 verifier agent 通过 lookup_value(ticker, canonical, fy) 这种工具来取数。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..core.enums import StatementType
from ..core.models import FinancialLine

# ============== Schema ==============

_DDL = """
CREATE TABLE IF NOT EXISTS financial_lines (
    ticker              TEXT NOT NULL,
    fiscal_period       TEXT NOT NULL,
    statement_type      TEXT NOT NULL,
    line_item           TEXT NOT NULL,
    line_item_canonical TEXT NOT NULL,
    value               REAL NOT NULL,
    unit                TEXT NOT NULL DEFAULT '元',
    is_consolidated     INTEGER NOT NULL DEFAULT 1,
    source_path         TEXT NOT NULL DEFAULT '',
    source_locator      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ticker, fiscal_period, statement_type,
                 line_item_canonical, is_consolidated)
);
CREATE INDEX IF NOT EXISTS idx_lines_lookup
    ON financial_lines (ticker, line_item_canonical, fiscal_period);
CREATE INDEX IF NOT EXISTS idx_lines_period
    ON financial_lines (ticker, fiscal_period);
"""


# ============== Store ==============


class FinancialsStore:
    """SQLite-backed FinancialLine 仓库。

    线程不安全：sqlite3.Connection 每个线程一份。短任务里复用一个 store 没问题。
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_DDL)

    # ============== 写 ==============

    def upsert_lines(self, lines: list[FinancialLine]) -> int:
        """批量 upsert。返回写入行数。"""
        if not lines:
            return 0
        sql = """
        INSERT OR REPLACE INTO financial_lines
            (ticker, fiscal_period, statement_type, line_item, line_item_canonical,
             value, unit, is_consolidated, source_path, source_locator)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        rows = [
            (
                ln.ticker,
                ln.fiscal_period,
                str(ln.statement_type),
                ln.line_item,
                ln.line_item_canonical,
                float(ln.value),
                ln.unit,
                1 if ln.is_consolidated else 0,
                ln.source_path,
                ln.source_locator,
            )
            for ln in lines
        ]
        with self._conn:
            self._conn.executemany(sql, rows)
        return len(rows)

    # ============== 读 ==============

    def get_value(
        self,
        ticker: str,
        fiscal_period: str,
        line_item_canonical: str,
        statement_type: StatementType | str | None = None,
        is_consolidated: bool = True,
    ) -> float | None:
        """精确取一个数值。找不到返回 None。"""
        params: list = [ticker, fiscal_period, line_item_canonical, 1 if is_consolidated else 0]
        sql = (
            "SELECT value FROM financial_lines "
            "WHERE ticker=? AND fiscal_period=? AND line_item_canonical=? "
            "AND is_consolidated=?"
        )
        if statement_type is not None:
            sql += " AND statement_type=?"
            params.append(str(statement_type))
        sql += " LIMIT 1"
        cur = self._conn.execute(sql, params)
        row = cur.fetchone()
        return float(row["value"]) if row else None

    def get_series(
        self,
        ticker: str,
        line_item_canonical: str,
        fiscal_periods: list[str] | None = None,
        is_consolidated: bool = True,
    ) -> dict[str, float]:
        """取某个 canonical 在多个财年的值。fiscal_periods=None 表示全部。

        返回 {fiscal_period: value}，按 fiscal_period 字典序升序。
        """
        params: list = [ticker, line_item_canonical, 1 if is_consolidated else 0]
        sql = (
            "SELECT fiscal_period, value FROM financial_lines "
            "WHERE ticker=? AND line_item_canonical=? AND is_consolidated=?"
        )
        if fiscal_periods:
            placeholders = ",".join("?" * len(fiscal_periods))
            sql += f" AND fiscal_period IN ({placeholders})"
            params.extend(fiscal_periods)
        sql += " ORDER BY fiscal_period ASC"
        cur = self._conn.execute(sql, params)
        return {row["fiscal_period"]: float(row["value"]) for row in cur.fetchall()}

    def query(
        self,
        ticker: str,
        fiscal_period: str | None = None,
        statement_type: StatementType | str | None = None,
        is_consolidated: bool | None = None,
    ) -> list[FinancialLine]:
        """按条件列出所有行。fiscal_period / statement_type / is_consolidated 都可选。"""
        clauses = ["ticker=?"]
        params: list = [ticker]
        if fiscal_period is not None:
            clauses.append("fiscal_period=?")
            params.append(fiscal_period)
        if statement_type is not None:
            clauses.append("statement_type=?")
            params.append(str(statement_type))
        if is_consolidated is not None:
            clauses.append("is_consolidated=?")
            params.append(1 if is_consolidated else 0)
        sql = (
            "SELECT * FROM financial_lines WHERE "
            + " AND ".join(clauses)
            + " ORDER BY fiscal_period, statement_type, line_item_canonical"
        )
        cur = self._conn.execute(sql, params)
        return [_row_to_line(r) for r in cur.fetchall()]

    def list_periods(self, ticker: str) -> list[str]:
        """该公司已有数据的财年（升序）。"""
        cur = self._conn.execute(
            "SELECT DISTINCT fiscal_period FROM financial_lines WHERE ticker=? ORDER BY fiscal_period ASC",
            (ticker,),
        )
        return [r["fiscal_period"] for r in cur.fetchall()]

    def list_canonicals(self, ticker: str) -> list[str]:
        """该公司出现过的所有 line_item_canonical（升序）。

        Phase 3 query_financials 工具在 line_item 找不到时用它给 agent 列候选。
        """
        cur = self._conn.execute(
            "SELECT DISTINCT line_item_canonical FROM financial_lines "
            "WHERE ticker=? ORDER BY line_item_canonical ASC",
            (ticker,),
        )
        return [r["line_item_canonical"] for r in cur.fetchall()]

    def count(self, ticker: str | None = None) -> int:
        if ticker is None:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM financial_lines")
        else:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM financial_lines WHERE ticker=?", (ticker,))
        return int(cur.fetchone()["n"])

    # ============== 杂项 ==============

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务上下文。批量 upsert 已有自带事务，一般用不到。"""
        with self._conn:
            yield self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> FinancialsStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ============== 内部 ==============


def _row_to_line(row: sqlite3.Row) -> FinancialLine:
    return FinancialLine(
        ticker=row["ticker"],
        fiscal_period=row["fiscal_period"],
        statement_type=StatementType(row["statement_type"]),
        line_item=row["line_item"],
        line_item_canonical=row["line_item_canonical"],
        value=float(row["value"]),
        unit=row["unit"],
        is_consolidated=bool(row["is_consolidated"]),
        source_path=row["source_path"],
        source_locator=row["source_locator"],
    )
