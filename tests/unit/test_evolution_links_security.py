"""演化不跟隨技能資料夾的連結（ADR-0293 PR 2，Q3）。

代理人的指令寫得到 ``mcp_servers/managed/<skill>``：可以把 ``<skill>`` 換成連結，指到假的設定
資料夾。以前演化的檔案方法先 ``resolve()`` 再讀寫，連結另一端的檔案會被寫進、讀出或送進部署審批卡。
現在五個檔案方法與部署快照都經區內方法：路上有連結就以 ``skill_changed`` 拒絕，另一端不變。

- 流程層（只負責在舊程式上紅，R15）：經三個工具入口，用真的 ResourceManager 與
  ``EvolutionPathGuard``。舊程式的 ``MANAGED_BASE_DIR`` 在 import 時固定成 session 根，新舊程式要
  指到同一處，所以連結放在 session 根（ADR-0293 PR 2「哪個根」）；fixture 給不重複的技能 id，
  結束時只刪連結本身。
- 方法層（殺逐方法的改壞）：直接呼叫五個方法與 ``_deployment_files``，data 根換到
  ``zone_data_root``。檔案本身是連結的案例只在 POSIX 跑。

連結另一端是測試自己建的假資料夾（``tmp_path``），不碰 ``data/``。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from src.config import get_app_data_dir
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation.evolution_command import EvolutionRefused
from src.tools.builtin import evolution
from src.utils import config_utils

from tests.support.links import LINK_FOLLOWED, make_dir_link
from tests.support.zone_records import expect_zone_refusal

CANARY = '{"hooks": "CANARY"}'
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="檔案連結只在 POSIX 測")


def _remove_link(link: Path) -> None:
    """只刪連結本身（symlink 或 junction），不進入另一端."""
    if link.is_symlink():
        link.unlink()
    elif link.is_junction():
        link.rmdir()


@pytest.fixture
def session_link(monkeypatch, tmp_path):
    """session 根的 ``managed/<不重複 id>`` 是連到假資料夾的連結；部署設定換成空的."""
    monkeypatch.setattr(config_utils, "CONFIG_DIR", tmp_path)
    managed = Path(get_app_data_dir()) / "mcp_servers" / "managed"
    managed.mkdir(parents=True, exist_ok=True)
    skill = f"linked-{uuid.uuid4().hex[:12]}"
    fake = tmp_path.resolve() / "fake"
    fake.mkdir()
    link = managed / skill
    make_dir_link(link, fake)
    try:
        yield skill, fake
    finally:
        _remove_link(link)


async def test_propose_does_not_write_through_a_linked_skill(session_link):
    skill, fake = session_link
    with expect_zone_refusal("ZoneLinkRefusedError"):
        result = await evolution.propose_new_capability(skill, "N", "D", {})
        assert not (fake / "spec.json").exists(), LINK_FOLLOWED
    assert json.loads(result)["reason"] == "skill_changed"
    assert list(fake.iterdir()) == []


async def test_implementation_does_not_write_through_a_linked_skill(session_link):
    skill, fake = session_link
    (fake / ".venv").mkdir()  # 舊程式看到 venv 已存在就不建，這個測試不起子行程
    with expect_zone_refusal("ZoneLinkRefusedError"):
        result = await evolution.write_mcp_implementation(skill, "CODE")
        assert not (fake / "server.py").exists(), LINK_FOLLOWED
        assert not (fake / "requirements.txt").exists(), LINK_FOLLOWED
    assert json.loads(result)["reason"] == "skill_changed"
    assert [p.name for p in fake.iterdir()] == [".venv"]


async def test_deployment_does_not_read_through_a_linked_skill(session_link, monkeypatch):
    skill, fake = session_link
    for name, content in {"server.py": CANARY, "requirements.txt": "", "spec.json": "{}"}.items():
        (fake / name).write_text(content, encoding="utf-8")
    card = AsyncMock(return_value=(False, {}))
    monkeypatch.setattr(evolution, "request_approval", card)
    with expect_zone_refusal("ZoneLinkRefusedError"):
        result = await evolution.request_mcp_creation(skill, _interactive=True)
        assert card.await_count == 0, LINK_FOLLOWED
    assert json.loads(result)["reason"] == "skill_changed"
    assert (fake / "server.py").read_text(encoding="utf-8") == CANARY


# ── 方法層（zone_data_root）─────────────────────────────────────────────────


@pytest.fixture
def managed(zone_data_root: Path) -> Path:
    path = zone_data_root / "mcp_servers" / "managed"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def linked(managed: Path, tmp_path: Path) -> Path:
    """``managed/sample`` 是連到假資料夾的連結；假資料夾裡有四個常數檔名的檔案."""
    fake = tmp_path.resolve() / "fake"
    fake.mkdir()
    for name in ("server.py", "spec.json", "requirements.txt", "test_wrapper.py"):
        (fake / name).write_text(CANARY, encoding="utf-8")
    make_dir_link(managed / "sample", fake)
    return fake


def _unchanged(fake: Path) -> None:
    names = {"server.py", "spec.json", "requirements.txt", "test_wrapper.py"}
    assert {p.name for p in fake.iterdir()} == names, LINK_FOLLOWED
    for name in names:
        assert (fake / name).read_text(encoding="utf-8") == CANARY, LINK_FOLLOWED


def test_get_tool_root_is_the_literal_zone_text(managed, linked):
    root = EvolutionPathGuard.get_tool_root("sample")
    assert isinstance(root, str)
    assert os.path.normcase(root) == os.path.normcase(str(managed.resolve() / "sample"))
    assert os.path.normcase(root) != os.path.normcase(str(linked)), LINK_FOLLOWED


@pytest.mark.parametrize("call", [
    lambda: EvolutionPathGuard.mkdir_tool("sample"),
    lambda: EvolutionPathGuard.write_tool_file("sample", "server.py", "NEW"),
    lambda: EvolutionPathGuard.read_tool_file("sample", "server.py"),
    lambda: EvolutionPathGuard.delete_tool_file("sample", "server.py"),
    lambda: EvolutionPathGuard.tool_file_exists("sample", "server.py"),
    lambda: evolution._deployment_files("sample"),
], ids=["mkdir", "write", "read", "delete", "exists", "deployment_files"])
def test_methods_refuse_a_linked_skill(linked, call):
    with expect_zone_refusal("ZoneLinkRefusedError"), pytest.raises(EvolutionRefused) as raised:
        call()
    assert raised.value.reason == "skill_changed"
    assert not isinstance(raised.value.__cause__, FileNotFoundError)
    _unchanged(linked)


@pytest.mark.parametrize("call", [
    lambda: EvolutionPathGuard.get_tool_root("a/b"),
    lambda: EvolutionPathGuard.mkdir_tool("soul"),
    lambda: EvolutionPathGuard.write_tool_file("..", "server.py", "NEW"),
    lambda: EvolutionPathGuard.read_tool_file("", "server.py"),
    lambda: EvolutionPathGuard.delete_tool_file("a\\b", "server.py"),
    lambda: EvolutionPathGuard.tool_file_exists("Personal_Drive", "server.py"),
], ids=["root", "mkdir", "write", "read", "delete", "exists"])
def test_methods_refuse_a_bad_skill_name(managed, call):
    with pytest.raises(EvolutionRefused) as raised:
        call()
    assert raised.value.reason == "invalid_skill_id"
    assert list(managed.iterdir()) == []


@pytest.fixture
def file_links(managed: Path, tmp_path: Path) -> Path:
    """``managed/sample`` 是真的資料夾，四個常數檔名都是連到外面檔案的連結."""
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (managed / "sample").mkdir()
    for name in ("server.py", "spec.json", "requirements.txt", "test_wrapper.py"):
        (outside / name).write_text(CANARY, encoding="utf-8")
        (managed / "sample" / name).symlink_to(outside / name)
    return outside


@posix_only
@pytest.mark.parametrize("name", ["server.py", "spec.json", "requirements.txt", "test_wrapper.py"])
def test_write_refuses_a_linked_file(file_links, name):
    with expect_zone_refusal("ZoneLinkRefusedError"), pytest.raises(EvolutionRefused) as raised:
        EvolutionPathGuard.write_tool_file("sample", name, "NEW")
    assert raised.value.reason == "skill_changed"
    assert (file_links / name).read_text(encoding="utf-8") == CANARY, LINK_FOLLOWED


@posix_only
@pytest.mark.parametrize("name", ["server.py", "spec.json"])
def test_read_refuses_a_linked_file(file_links, name):
    with expect_zone_refusal("ZoneLinkRefusedError"), pytest.raises(EvolutionRefused) as raised:
        EvolutionPathGuard.read_tool_file("sample", name)
    assert raised.value.reason == "skill_changed"


@posix_only
def test_deployment_files_refuse_a_linked_file(file_links):
    with expect_zone_refusal("ZoneLinkRefusedError"), pytest.raises(EvolutionRefused) as raised:
        evolution._deployment_files("sample")
    assert raised.value.reason == "skill_changed"


@posix_only
def test_delete_removes_only_the_link_and_exists_sees_it(file_links, managed):
    assert EvolutionPathGuard.tool_file_exists("sample", "test_wrapper.py") is True
    assert EvolutionPathGuard.delete_tool_file("sample", "test_wrapper.py") is True
    assert not (managed / "sample" / "test_wrapper.py").is_symlink()
    assert (file_links / "test_wrapper.py").read_text(encoding="utf-8") == CANARY, LINK_FOLLOWED
