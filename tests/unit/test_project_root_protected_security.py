"""一般使用者的專案根不能是 Touka 保護的地方（ADR-0292）。

原本只要求專案根在工作區根底下；原生安裝的工作區根預設就是安裝資料夾，裡面有設定（含
``config/hooks/``）、資料與程式碼。一般使用者把專案登記在那裡，代理人就能經專案檔案工具
改到不經隔離執行的 hook 程式。現在：

- 登記專案、列專案檔案、專案檔案工具都用 ``profiles.project_root_reason``：固定受保護集合
  （解析連結後比對，它們的上層也算）；原生安裝時安裝資料夾底下只有使用者自建的頂層資料夾
  可以用（點開頭與程式、文件等名稱不行，同禁令 66 ③）；容器裡另做 Touka 目錄偵測（工作區
  掛載可能是同一批檔案的另一個路徑），沒查完也不放行，但回不同的原因；
- 專案檔案工具每次呼叫都查，登記後才改的也擋；拒絕時回結構化錯誤；
- admin、Debugger 與管理員擁有的靈魂不受限。
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.user import User
from src.routers import ui_sessions
from src.services import authorization_gateway, tool_errors
from src.services.isolation import profiles
from src.services.isolation.profiles import SandboxLayout
from src.services.resource_manager import resource_manager
from src.tools import project_tools as pt
from src.utils.touka_dirs import PROTECTED_LOCATION, SCAN_LIMIT

if TYPE_CHECKING:
    from pathlib import Path

if sys.platform == "win32":
    import _winapi

ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True, capabilities=[])
SOUL = AgentIdentity.for_role(agent_id="Alice", role="Owner", is_human=False, capabilities=[])


def _make_dir_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        _winapi.CreateJunction(str(target), str(link))


def _layout(root: Path, *, in_container: bool) -> SandboxLayout:
    for name in ("config/hooks", "data/personal_drive/Alice", "ToukaProxy", "myproj"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return SandboxLayout(
        data_root=root / "data",
        config_dir=root / "config",
        install_root=root,
        proxy_root=root / "ToukaProxy",
        env_file=root / ".env",
        state_root=root / "state",
        kernel_dir=None,
        executor_dir=None,
        mirror_roots=(root,),
        in_container=in_container,
    )


@pytest.fixture
def native(tmp_path, monkeypatch) -> SandboxLayout:
    layout = _layout(tmp_path, in_container=False)
    monkeypatch.setattr(profiles, "current_layout", lambda: layout)

    async def no_scan(_path, _layout):
        pytest.fail("原生安裝不做 Touka 目錄偵測")

    monkeypatch.setattr(profiles, "touka_directory_verdict", no_scan)
    return layout


@pytest.mark.parametrize(
    "relative", ["config/hooks", "config", "data/personal_drive/Alice", "ToukaProxy", "."]
)
async def test_protected_places_and_their_parents_are_refused(native, tmp_path, relative):
    assert await profiles.project_root_reason(tmp_path / relative, native) == PROTECTED_LOCATION


async def test_a_user_folder_is_allowed(native, tmp_path):
    assert await profiles.project_root_reason(tmp_path / "myproj", native) is None


@pytest.mark.parametrize("relative", [".venv", ".git/hooks", ".claude", "ToukaChat", "Scripts/x"])
async def test_only_user_made_top_folders_of_the_install_are_allowed(native, tmp_path, relative):
    """原生安裝：安裝資料夾底下點開頭與程式、文件等頂層名稱也受保護（不分大小寫）。"""
    (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    assert await profiles.project_root_reason(tmp_path / relative, native) == PROTECTED_LOCATION


async def test_a_link_into_a_protected_place_is_refused(native, tmp_path):
    _make_dir_link(tmp_path / "myproj" / "cfg", tmp_path / "config")
    reason = await profiles.project_root_reason(tmp_path / "myproj" / "cfg", native)
    assert reason == PROTECTED_LOCATION


@pytest.mark.parametrize("verdict", [PROTECTED_LOCATION, None])
async def test_container_also_checks_touka_directories(tmp_path, monkeypatch, verdict):
    """容器裡工作區掛載可能是 Touka 目錄的另一個路徑：結果交給 Touka 目錄偵測。"""
    mount = tmp_path / "workspace"
    (mount / "myproj").mkdir(parents=True)
    layout = replace(_layout(tmp_path, in_container=True), mirror_roots=(mount,))
    seen: list[Path] = []

    async def scan(path: Path, _layout: SandboxLayout) -> str | None:
        seen.append(path)
        return verdict

    monkeypatch.setattr(profiles, "touka_directory_verdict", scan)
    assert await profiles.project_root_reason(mount / "myproj", layout) == verdict
    assert seen == [(mount / "myproj").resolve()]
    # 安裝資料夾裡工作區掛載以外的地方，固定規則就擋下，不必偵測
    assert await profiles.project_root_reason(tmp_path / "myproj", layout) == PROTECTED_LOCATION
    assert len(seen) == 1


def _project_tools_at(monkeypatch, root: Path, writes: list) -> None:
    async def project_path(_project_id, _identity=None):
        return str(root)

    async def write(path, root_bound, **_kwargs):
        writes.append((path, root_bound))
        return "Success: wrote"

    async def no_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pt.resource_manager, "get_project_path", project_path)
    monkeypatch.setattr(pt, "core_write", write)
    monkeypatch.setattr(pt, "add_project_event", no_event)
    monkeypatch.setattr(pt, "owned_by_admin", lambda _identity: False)


@pytest.mark.parametrize("identity", [SOUL, None])
async def test_project_tools_refuse_a_protected_root(native, tmp_path, monkeypatch, identity):
    """專案根在登記後被改成 ``config/hooks`` 也擋：每次呼叫都查。"""
    writes: list = []
    _project_tools_at(monkeypatch, tmp_path / "config" / "hooks", writes)
    result = await pt.project_tools(
        "write", path="guard.py", content="x", project_id="p1", identity=identity
    )
    payload = json.loads(result)
    assert payload["error_class"] == tool_errors.PERMISSION_DENIED
    assert payload["reason"] == PROTECTED_LOCATION
    assert "protects" in payload["message"]
    assert writes == []


async def test_an_unfinished_check_says_so_and_does_not_allow(tmp_path, monkeypatch):
    """容器裡沒查完（資料夾太多、讀不到或還在查）：照樣不放行，但不說成受保護。"""
    mount = tmp_path / "workspace"
    (mount / "big").mkdir(parents=True)
    layout = replace(_layout(tmp_path, in_container=True), mirror_roots=(mount,))
    monkeypatch.setattr(profiles, "current_layout", lambda: layout)

    async def unfinished(_path, _layout):
        return SCAN_LIMIT

    monkeypatch.setattr(profiles, "touka_directory_verdict", unfinished)
    writes: list = []
    _project_tools_at(monkeypatch, mount / "big", writes)
    result = await pt.project_tools(
        "write", path="a.py", content="x", project_id="p1", identity=SOUL
    )
    payload = json.loads(result)
    assert payload["reason"] == SCAN_LIMIT
    assert "protects" not in payload["message"]
    assert "try again" in payload["suggestion"].lower()
    assert writes == []


async def test_souls_an_admin_owns_are_not_limited(native, tmp_path, monkeypatch):
    writes: list = []
    _project_tools_at(monkeypatch, tmp_path / "config" / "hooks", writes)
    monkeypatch.setattr(pt, "owned_by_admin", lambda identity: identity.agent_id == "Alice")
    result = await pt.project_tools(
        "write", path="guard.py", content="x", project_id="p1", identity=SOUL
    )
    assert result.startswith("Success")


def _user(role: str, owned: list[str]) -> User:
    return User(
        id=role, email=f"{role}@example.com", hashed_password="x", role=role, owned_agents=owned
    )


@pytest.mark.parametrize(
    ("users", "expected"),
    [
        ([_user("admin", ["Alice"])], True),
        ([_user("normal", ["Alice"])], False),
        ([_user("admin", ["Bob"])], False),
        ([], False),
    ],
)
def test_owned_by_admin_reads_the_owner_role(monkeypatch, users, expected):
    monkeypatch.setattr(authorization_gateway.AuthService, "get_all_users", lambda _self: users)
    assert authorization_gateway.owned_by_admin(AgentIdentity(agent_id="Alice")) is expected


async def test_project_tools_still_work_in_a_user_folder(native, tmp_path, monkeypatch):
    writes: list = []
    _project_tools_at(monkeypatch, tmp_path / "myproj", writes)
    result = await pt.project_tools(
        "write", path="a.py", content="x", project_id="p1", identity=SOUL
    )
    assert result.startswith("Success")
    assert writes == [("a.py", str(tmp_path / "myproj"))]


async def test_admin_is_not_limited(native, tmp_path, monkeypatch):
    writes: list = []
    _project_tools_at(monkeypatch, tmp_path / "config" / "hooks", writes)
    result = await pt.project_tools(
        "write", path="guard.py", content="x", project_id="p1", identity=ADMIN
    )
    assert result.startswith("Success")


@pytest.fixture
def workspace(native, tmp_path, monkeypatch) -> Path:
    """工作區根＝安裝資料夾（原生安裝的預設）。"""

    async def root() -> Path:
        return tmp_path

    monkeypatch.setattr(resource_manager, "_get_workspace_root", root)
    monkeypatch.setattr(resource_manager, "_is_docker", False)
    return tmp_path


async def test_registering_a_protected_root_is_refused(workspace):
    hooks = str(workspace / "config" / "hooks")
    assert await ui_sessions._project_root_allowed(SOUL, hooks) is False
    assert await ui_sessions._project_root_allowed(SOUL, str(workspace)) is False
    assert await ui_sessions._project_root_allowed(SOUL, str(workspace / "myproj")) is True
    assert await ui_sessions._project_root_allowed(ADMIN, str(workspace / "config")) is True


@pytest.mark.parametrize(
    ("relative", "reason", "phrase"),
    [
        ("config/hooks", PROTECTED_LOCATION, "outside the folders Touka protects"),
        ("../elsewhere", "outside_workspace", "inside the workspace root"),
    ],
)
async def test_refused_registration_says_why(workspace, relative, reason, phrase):
    with pytest.raises(ToukaException) as refused:
        await ui_sessions._check_project_root(SOUL, str(workspace / relative))
    assert refused.value.code == ErrorCode.VALIDATION_ERROR
    assert refused.value.detail["reason"] == reason
    assert phrase in refused.value.detail["message"]


async def test_registration_whose_check_did_not_finish_says_so(workspace, monkeypatch):
    """檢查沒做完（資料夾太多、讀不到、還在掃）不說成受保護的位置，也不放行。"""

    async def unfinished(_target: Path, _layout: SandboxLayout) -> str:
        return SCAN_LIMIT

    monkeypatch.setattr(profiles, "project_root_reason", unfinished)
    with pytest.raises(ToukaException) as refused:
        await ui_sessions._check_project_root(SOUL, str(workspace / "myproj"))
    assert refused.value.detail["reason"] == SCAN_LIMIT
    assert "could not finish checking" in refused.value.detail["message"]
    await ui_sessions._check_project_root(ADMIN, str(workspace / "myproj"))
