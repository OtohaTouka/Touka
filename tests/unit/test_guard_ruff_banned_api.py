"""Unit tests for guard_ruff_banned_api.py (Guard #44, 早期缺口 E09).

驗證 banned-api（TID251）守衛：
- 真實源碼樹乾淨（活體鎖）。
- 對合成的 banned-api 呼叫確實變紅（行為級，非只驗機制存在）。
- 已註冊於 guard_all.py 聚合器（meta-test，比照 #31/#35/#36 慣例）。
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from src.config import get_full_env

_PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

from guard_ruff_banned_api import (  # noqa: E402
    SCAN_DIRS,
    run_guard,
)


@pytest.fixture(autouse=True)
def sandbox_ruff_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every real Ruff child inherits a cache owned by this test."""
    monkeypatch.setenv("RUFF_CACHE_DIR", str(tmp_path / "ruff-cache"))


def test_guard_child_uses_owned_sandbox_cache(tmp_path):
    """Keep the actual guard subprocess and its writes inside the test sandbox."""
    cache = Path(get_full_env()["RUFF_CACHE_DIR"])
    assert cache.is_relative_to(tmp_path)
    test_guard_catches_synthetic_banned_call(tmp_path)
    assert cache.is_dir()


def test_real_source_tree_has_no_banned_api():
    """活體鎖：真實源碼樹（尊重 per-file-ignores）零 TID251 違規。"""
    ok, output = run_guard()
    assert ok, f"真實源碼樹出現 banned-api 違規：\n{output}"


def test_guard_catches_synthetic_banned_call(tmp_path):
    """行為級：對合成的被禁呼叫，守衛必變紅（非只在單一 fixture 上驗機制）。

    在注入的合成樹（``tmp/ToukaProxy/src/services/banned_probe.py``，未被
    per-file-ignores 豁免的路徑名）放 ``os.getcwd()``，經 ``run_guard`` 真實驅動
    ruff TID251 SSOT，斷言被攔。
    """
    pkg = tmp_path / "ToukaProxy" / "src" / "services"
    pkg.mkdir(parents=True)
    (pkg / "banned_probe.py").write_text(
        textwrap.dedent("""
            import os

            def leak() -> str:
                return os.getcwd()
        """),
        encoding="utf-8",
    )
    ok, output = run_guard(project_root=tmp_path)
    assert not ok, "合成的 os.getcwd() 應被 TID251 攔截"
    assert "TID251" in output
    assert "os.getcwd" in output


def test_scan_covers_all_python_subprojects():
    """守衛掃描範圍須涵蓋四個 Python 子專案＋scripts（不漏掃）。"""
    for expected in ["ToukaProxy", "ToukaMemory", "ToukaHeartbeat", "ToukaDispatch"]:
        assert expected in SCAN_DIRS


def test_guard_registered_in_guard_all():
    """meta-test：Guard #44 必須註冊於 guard_all.py（否則 CI 不執行它）。"""
    guard_all = (_PROJECT_ROOT / "scripts" / "guard_all.py").read_text(encoding="utf-8")
    assert "Guard #44" in guard_all
    assert "guard_ruff_banned_api.py" in guard_all
