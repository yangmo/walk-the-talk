"""SQLite-backed (prompt, model) → response 缓存。

为什么落 SQLite 而不是 JSON：
- claims 抽取可能跑成百上千次 LLM 调用，JSON load/dump 大文件吃力。
- SQLite 单文件、并发安全（WAL）、查询快、好审计。

落盘路径默认 `<work_dir>/llm_cache.db`，由调用方传入。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS llm_cache (
    key            TEXT PRIMARY KEY,        -- sha256(model + messages + extras)
    model          TEXT NOT NULL,
    response_text  TEXT NOT NULL,
    prompt_tokens  INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens   INTEGER NOT NULL DEFAULT 0,
    raw_json       TEXT NOT NULL DEFAULT '{}',
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_cache_model ON llm_cache(model);
"""


def _hash_key(model: str, messages: list[dict[str, str]], extras: dict[str, Any]) -> str:
    payload = json.dumps(
        {"model": model, "messages": messages, "extras": extras},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PromptCache:
    """SQLite 单文件缓存。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL：多线程读 + 偶尔写不阻塞
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._conn:
            self._conn.executescript(_DDL)

    def make_key(
        self,
        model: str,
        messages: list[dict[str, str]],
        extras: dict[str, Any] | None = None,
    ) -> str:
        return _hash_key(model, messages, extras or {})

    def get(self, key: str) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "SELECT model, response_text, prompt_tokens, completion_tokens, total_tokens, raw_json "
            "FROM llm_cache WHERE key=?",
            (key,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "model": row["model"],
            "response_text": row["response_text"],
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
            "total_tokens": int(row["total_tokens"]),
            "raw": json.loads(row["raw_json"] or "{}"),
        }

    def put(
        self,
        key: str,
        *,
        model: str,
        response_text: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        raw: dict[str, Any] | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO llm_cache "
                "(key, model, response_text, prompt_tokens, completion_tokens, total_tokens, raw_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    model,
                    response_text,
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(total_tokens),
                    json.dumps(raw or {}, ensure_ascii=False),
                    time.time(),
                ),
            )

    def count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM llm_cache")
        return int(cur.fetchone()["n"])

    def close(self) -> None:
        self._conn.close()
