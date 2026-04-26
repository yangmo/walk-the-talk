"""共享 pytest fixtures，覆盖所有 phase 子目录下的测试。

按目录约定：
- ``tests/conftest.py``     — 全局 fixture（本文件）
- ``tests/<phase>/conftest.py`` — phase 专用 fixture（按需添加）
- ``tests/fixtures/``       — 静态测试数据（如 SMIC 2025 年报 HTML）

公开 fixture：

- :func:`fixtures_dir`     — ``tests/fixtures/`` 目录的绝对路径
- :func:`smic_html_path`   — SMIC FY2025 年报 HTML（848KB GBK）的绝对路径
- :func:`smic_data_dir`    — 把 SMIC HTML 复制到 tmp_path/smic 下，模拟用户数据目录

设计原则：fixture 路径解析全部集中在 conftest.py，避免每个 test 文件
``Path(__file__).parent / "fixtures" / ...`` 重复一次。这样测试文件挪位置
不会破坏路径。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# tests/ 目录绝对路径，所有相对路径以此为锚点
_TESTS_ROOT = Path(__file__).resolve().parent


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """``tests/fixtures/`` 绝对路径（session 级，无 IO 开销）。"""
    return _TESTS_ROOT / "fixtures"


@pytest.fixture(scope="session")
def smic_html_path(fixtures_dir: Path) -> Path:
    """中芯国际 FY2025 年报 HTML（GBK，848KB）。

    该文件入仓库 (`tests/fixtures/中芯国际/2025.html`)，是端到端测试种子。
    用 ``shutil.copy`` 进 tmp 路径而不是直接读：避免测试污染原 fixture。
    """
    return fixtures_dir / "中芯国际" / "2025.html"


@pytest.fixture
def smic_data_dir(tmp_path: Path, smic_html_path: Path) -> Path:
    """把 SMIC HTML 复制到 ``tmp_path/smic/2025.html``，模拟用户的 data_dir。

    端到端测试入口：用 ``walk-the-talk ingest <smic_data_dir>`` 的形态消费。
    """
    if not smic_html_path.exists():
        pytest.skip(f"SMIC fixture missing: {smic_html_path}")
    dst = tmp_path / "smic"
    dst.mkdir()
    shutil.copy(smic_html_path, dst / "2025.html")
    return dst
