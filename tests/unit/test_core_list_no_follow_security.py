"""列檔案（``core_list``）沒有 proxy 時（專案工具）也不跟隨連結（ADR-0332）.

舊的備用寫法先問「是不是連結」，再用會跟隨連結的 ``is_dir``／``iterdir``：問完之後那一項被換成
連結，就會列出根外面的名稱。現在和經 proxy 的列法一樣，走訪本身不跟隨（lstat 判種類、開資料夾
不跟隨），代理人資料夾剪掉，排版也是同一份。全部在 pytest ``tmp_path`` 裡。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from memory_server.tools import core_file_ops as cfo
from memory_server.utils.no_follow import LINK_NOT_FOLLOWED
from src.services.resource_manager import resource_manager

if sys.platform == "win32":
    import _winapi

if TYPE_CHECKING:
    from pathlib import Path


def _make_dir_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        _winapi.CreateJunction(str(target), str(link))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """專案根：``sub/notes.txt``、四層深的資料夾、隱藏名稱、指向根外面的連結 ``peek``."""
    base = tmp_path.resolve()
    root, outside = base / "project", base / "outside"
    (root / "sub" / "a" / "b" / "c").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    outside.mkdir()
    (root / "sub" / "notes.txt").write_text("inside\n", encoding="utf-8")
    (root / "sub" / "a" / "b" / "c" / "deep.txt").write_text("deep\n", encoding="utf-8")
    (outside / "guard.py").write_text("SECRET = 1\n", encoding="utf-8")
    _make_dir_link(root / "peek", outside)
    return root


async def test_a_link_the_check_missed_is_still_not_entered(project, monkeypatch):
    """「是不是連結」問完之後才換成連結（這裡讓問的結果一律是「不是」）：走訪本身照樣不進入."""
    monkeypatch.setattr(cfo, "is_link", lambda _path: False, raising=False)
    listing = await cfo.core_list("", str(project), max_depth=3)
    assert f"peek {LINK_NOT_FOLLOWED}" in listing
    assert "guard.py" not in listing
    assert "notes.txt" in listing


@pytest.mark.parametrize("depth", [1, 2, 4])
async def test_listing_without_a_proxy_is_the_same_as_through_the_proxy(project, depth):
    """兩條路同一種結果：同樣隱藏、同樣的深度上限與連結標示（只有第一行的標題不同）."""
    direct = await cfo.core_list("", str(project), max_depth=depth)
    proxied = await cfo.core_list(
        "", str(project), max_depth=depth, resource_proxy=resource_manager
    )
    assert direct.splitlines()[1:] == proxied.splitlines()[1:]
    assert "node_modules" not in direct and ".git" not in direct
