"""Touka 目錄頂層不能授權的名稱，與 repo 實際內容同步（iso-c2 U1g）.

``TOUKA_TOPLEVEL_PROTECTED`` 由三部分組成，全部 casefold：

- ``git ls-files`` 的頂層名稱（點開頭的除外，那些由「所有點開頭名稱」的規則涵蓋）；
- ``.gitignore`` 的頂層名稱：不含 ``/``（錨定在子路徑的不算）、不含萬用字元、不是 ``!`` 例外；
  萬用字元列（``*.db`` 等）只會符合檔案或建置產物，頂層檔案本來就一律不授權；
- ``scratch``。

兩個方向都要同步：少了名稱會讓那個位置可以被授權；多了名稱代表清單過時。
"""

from __future__ import annotations

import shutil
import unicodedata
from pathlib import Path

import pytest
from src.services import execution_broker
from src.utils.isolation_policy import TOUKA_TOPLEVEL_PROTECTED

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
EXTRA_NAMES = frozenset({"scratch"})
_WILDCARDS = frozenset("*?[")


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _tracked_toplevel_names() -> set[str]:
    if not GIT:
        pytest.skip("Git unavailable")
    result = execution_broker.run_sync(
        [GIT, "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    paths = [p for p in result.stdout.decode("utf-8").split("\0") if p]
    assert paths, "git ls-files 沒有輸出"
    return {path.split("/", 1)[0] for path in paths}


def _gitignore_toplevel_names() -> set[str]:
    names = set()
    for raw in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        name = line.strip("/")
        if "/" in name or _WILDCARDS & set(name):
            continue
        names.add(name)
    return names


def test_protected_names_match_tracked_and_ignored_toplevel_names() -> None:
    sources = _tracked_toplevel_names() | _gitignore_toplevel_names()
    expected = {_fold(n) for n in sources if not n.startswith(".")} | EXTRA_NAMES
    missing = sorted(expected - TOUKA_TOPLEVEL_PROTECTED)
    stale = sorted(TOUKA_TOPLEVEL_PROTECTED - expected)
    assert not missing, f"頂層名稱沒有受保護：{missing}"
    assert not stale, f"清單裡有 repo 已經沒有的名稱：{stale}"


def test_protected_names_are_folded_and_leave_dot_names_to_the_rule() -> None:
    assert all(name == _fold(name) for name in TOUKA_TOPLEVEL_PROTECTED)
    assert not [name for name in TOUKA_TOPLEVEL_PROTECTED if name.startswith(".")]


def test_gitignore_names_cover_the_known_secret_holders() -> None:
    """解析規則本身的對照：這些名稱一定要被讀到（避免解析壞掉時兩邊一起變空）。"""
    ignored = {_fold(n) for n in _gitignore_toplevel_names()}
    secret_holders = {"data", "backups", "heartbeat-config", "heartbeat-config.json"}
    assert secret_holders | {"venv", "env", "site", "node_modules"} <= ignored
