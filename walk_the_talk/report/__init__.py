"""report 包入口：合成 markdown 可信度报告。

公共 API：
- build_report(claim_store, verdict_store, *, current_fy, ...) -> str
- run_report(settings, on_log=...) -> dict  # CLI 用
"""

from __future__ import annotations

from .builder import build_report, run_report

__all__ = ["build_report", "run_report"]
