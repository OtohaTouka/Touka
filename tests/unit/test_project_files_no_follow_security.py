"""專案檔案清單頁（``GET /ui/projects/{id}/files``）不跟隨連結（ADR-0332）.

舊寫法用「解析後還在根裡」決定要不要略過一項，之後用會跟隨連結的 ``is_dir``／``iterdir``／
``stat``：判斷完才被換成連結，就會列出根外面的檔名與大小。現在經 ResourceManager 不跟隨地走訪：
連結（指向根裡或根外）都不列也不進入，大小是 lstat 的，代理人資料夾剪掉。樹的形狀照舊：最多四層、
隱藏名稱略過、資料夾在前、名稱不分大小寫排序。全部在 ``tmp_path`` 與測試沙盒的資料庫。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import pytest
from src.models.identity import AgentIdentity
from src.routers import ui_sessions
from src.services.resource_manager import resource_manager
from src.utils.async_db import init_db_async

if sys.platform == "win32":
    import _winapi

if TYPE_CHECKING:
    from pathlib import Path

AGENT = AgentIdentity.for_role(agent_id="filesAgent", role="Base", is_human=False, capabilities=[])
PROJECT_ID = "p_files"


def _make_dir_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        _winapi.CreateJunction(str(target), str(link))


@pytest.fixture
async def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """工作區裡的專案；根外面有 ``leak.txt``，根裡有指向外面與指向根裡的連結."""
    await init_db_async()
    workspace = (tmp_path / "workspace").resolve()
    root, outside = workspace / "proj", tmp_path.resolve() / "outside"
    (root / "sub").mkdir(parents=True)
    outside.mkdir()
    (root / "real.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "sub" / "inner.txt").write_text("12345", encoding="utf-8")
    (outside / "leak.txt").write_text("leak", encoding="utf-8")
    _make_dir_link(root / "link_out", outside)
    _make_dir_link(root / "link_in", root / "sub")

    async def _workspace():
        return workspace

    async def _agents(project_id):
        return [AGENT.agent_id] if project_id == PROJECT_ID else []

    async def _root(_project_id):
        return str(root)

    monkeypatch.setattr(resource_manager, "_get_workspace_root", _workspace)
    monkeypatch.setattr(resource_manager, "_is_docker", False)
    monkeypatch.setattr(ui_sessions.session_service, "get_project_agent_ids", _agents)
    monkeypatch.setattr(ui_sessions, "get_project_root_path", _root)
    return root


def _flatten(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for node in nodes:
        found[node["path"]] = node
        found |= _flatten(node.get("children", []))
    return found


async def _tree() -> list[dict[str, Any]]:
    return (await ui_sessions.get_project_files(PROJECT_ID, AGENT))["tree"]


async def test_links_are_neither_listed_nor_entered(project):
    nodes = _flatten(await _tree())
    assert set(nodes) == {"real.py", "sub", "sub/inner.txt"}
    assert nodes["sub/inner.txt"]["size"] == 5


async def test_a_link_that_passes_the_old_check_is_still_not_followed(project, monkeypatch):
    """判斷之後才被換成連結（這裡讓舊的判斷一律說「在根裡」）：走訪本身照樣不跟隨."""
    monkeypatch.setattr(ui_sessions, "validate_no_symlink_escape", lambda *_a: True, raising=False)
    nodes = _flatten(await _tree())
    assert not any(path.startswith("link_") for path in nodes)
    assert "leak.txt" not in {node["name"] for node in nodes.values()}


async def test_tree_shape_depth_hidden_names_and_order(project):
    """最多四層（第四層的資料夾只列出、children 是空的）、隱藏名稱略過、資料夾在前."""
    deep = project / "Alpha" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "too_deep.txt").write_text("x", encoding="utf-8")
    for hidden in (".env", "node_modules", "venv", "__pycache__", ".git"):
        (project / hidden).mkdir() if "." not in hidden[1:] else (project / hidden).write_text("")
    (project / "zeta.txt").write_text("z", encoding="utf-8")
    tree = await _tree()
    assert [node["name"] for node in tree] == ["Alpha", "sub", "real.py", "zeta.txt"]
    nodes = _flatten(tree)
    assert nodes["Alpha/b/c/d"] == {
        "name": "d", "type": "directory", "path": "Alpha/b/c/d", "children": []
    }
    assert not any(part in nodes for part in (".env", "node_modules", "venv", "__pycache__"))
