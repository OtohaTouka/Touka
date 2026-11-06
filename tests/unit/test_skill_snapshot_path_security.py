"""技能名稱與內容快照的代理人名稱不能被當成路徑；系統技能不能被覆蓋或刪除（sec-9 PR 1a）。

技能名稱會接成 ``config/skills/<名稱>`` 資料夾；快照匯出會寫 ``snapshots/<代理人>.md``。
名稱只接受單一段的技能名稱（``is_skill_segment``），也不能只和已有的技能資料夾差大小寫：
不分大小寫的檔案系統上那會指到同一個資料夾。系統技能從磁碟上的 ``SKILL.md`` 判斷，更新、安裝、
刪除都擋。快照的代理人名稱要是單一安全段，而且和代理人清單的名稱逐字相同。

修補前跑紅燈時，名稱會真的被接成路徑：技能測試的寫入、刪除、搬移、打包都經
``_writes_stay_in_sandbox`` 檢查，目標不在 tmp_path 或沙盒資料根底下就直接失敗、不執行。
只用 tmp_path、沙盒與 mock，不碰 data/。
"""

from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.skills import SkillModel
from src.services import resource_manager as resource_manager_module
from src.services import skill_parser
from src.services.agent_service import agent_service
from src.services.creation.models import SkillSpec
from src.services.creation.skill_kind import SkillCreationKind
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager
from src.services.skill_service import skill_service
from src.tools.builtin.system import activate_skill

from tests.support.zone_roots import point_zone_roots

BAD_NAMES = ["..", ".", "../outside", "a/b", "a\\b", "/abs", "C:", "name\n", "", " name", "a.b"]
ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True)
SOUL_OWNER = AgentIdentity.for_role(agent_id="MySoul", role="Owner")
# 寫入類方法 → 哪幾個位置參數是目標路徑
_WRITE_TARGETS = {
    "write_file_text": (0,),
    "write_file_binary": (0,),
    "write_json": (0,),
    "make_dir": (0,),
    "remove_dir": (0,),
    "move_file": (0, 1),
    "create_archive": (1,),
}


@pytest.fixture
def _writes_stay_in_sandbox(monkeypatch, tmp_path):
    allowed = (tmp_path.resolve(), Path(resource_manager.get_base_path()).resolve())
    for name, positions in _WRITE_TARGETS.items():
        real = getattr(resource_manager, name)

        def guarded(*args, _real=real, _name=name, _positions=positions, **kwargs):
            for index in _positions:
                target = Path(args[index]).resolve()
                if not any(target.is_relative_to(root) for root in allowed):
                    pytest.fail(f"{_name} outside the sandbox: {args[index]}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(resource_manager, name, guarded)


@pytest.fixture
def svc(tmp_path, _writes_stay_in_sandbox):
    """技能資料夾放在 tmp 的 config/skills；旁邊放一個不該被動到的檔。"""
    skills_dir = tmp_path / "config" / "skills"
    resource_manager.make_dir(skills_dir)
    resource_manager.write_file_text(tmp_path / "config" / "keep.json", "{}")
    skill_service._skills_dir = skills_dir
    skill_service.config_path = str(skills_dir)
    skill_service.load_config()
    return skill_service


def _write_skill(svc, sid, *, is_system=False):
    fm = skill_parser.build_frontmatter(
        name=sid, description="d", display_name=sid, category="General", author="",
        is_system=is_system,
    )
    resource_manager.write_file_text(
        svc._skills_dir / sid / "SKILL.md",
        skill_parser.serialize_skill_md(fm, "body"),
        make_parents=True,
    )
    svc.load_config()


def _write_outside_skill(tmp_path):
    resource_manager.write_file_text(
        tmp_path / "config" / "elsewhere" / "SKILL.md",
        "---\nname: x\n---\nSECRET",
        make_parents=True,
    )


def _skill_archive(tmp_path, root, *, is_system=False):
    src = tmp_path / "pkg" / root
    system = "metadata:\n  is_system: true\n" if is_system else ""
    resource_manager.write_file_text(
        src / "SKILL.md", f"---\nname: {root}\ndescription: d\n{system}---\nnew", make_parents=True
    )
    archive = tmp_path / "pkg.skill"
    resource_manager.create_archive(src, archive, arc_base=src.parent)
    return resource_manager.read_file_binary(archive)


def _mock_httpx(content):
    resp = MagicMock(content=content)
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _url_install(content):
    return patch("src.services.skill_service.httpx.AsyncClient", return_value=_mock_httpx(content))


# --- 技能名稱 ---


@pytest.mark.parametrize("name", BAD_NAMES)
def test_every_skill_operation_refuses_a_path_name(svc, tmp_path, name):
    assert svc.get_skill(name) is None
    assert svc.get_skill_resource(name, "SKILL.md") is None
    assert svc.list_skill_resources(name) == []
    assert svc.is_usable_name(name) is False
    for operation in (
        lambda: svc.update_skill(name, {"system_prompt": "x"}),
        lambda: svc.delete_skill(name),
        lambda: svc.package_skill(name),
    ):
        with pytest.raises(ToukaException):
            operation()
    assert resource_manager.exists(tmp_path / "config" / "keep.json")


def test_delete_dotdot_keeps_the_config_folder(svc, tmp_path):
    with pytest.raises(ToukaException) as exc:
        svc.delete_skill("..")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert resource_manager.exists(tmp_path / "config" / "keep.json")


def test_update_cannot_write_outside_the_skills_folder(svc, tmp_path):
    with pytest.raises(ToukaException) as exc:
        svc.update_skill("../outside", {"system_prompt": "x", "mcp_servers": ["m"]})
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert not resource_manager.exists(tmp_path / "config" / "outside")


def test_skill_md_outside_the_skills_folder_is_not_read(svc, tmp_path):
    _write_outside_skill(tmp_path)
    assert svc.get_skill("../elsewhere") is None
    assert svc.get_skill_resource("../elsewhere", "SKILL.md") is None


def test_case_alias_does_not_reach_an_existing_skill(svc):
    """分大小寫的檔案系統上，兩個讀取斷言修補前後都成立；update、package 在任何系統都分得出來。"""
    _write_skill(svc, "real-skill")
    assert svc.get_skill("Real-Skill") is None
    assert svc.list_skill_resources("REAL-SKILL") == []
    with pytest.raises(ToukaException):
        svc.update_skill("Real-Skill", {"system_prompt": "overwrite"})
    with pytest.raises(ToukaException):
        svc.package_skill("Real-Skill")
    assert svc.get_skill("real-skill")["system_prompt"] == "body"


def test_valid_names_keep_working(svc):
    svc.update_skill("my_Skill-2", {"system_prompt": "hello"})
    assert svc.get_skill("my_Skill-2")["system_prompt"] == "hello"
    assert svc.package_skill("my_Skill-2").name == "my_Skill-2.skill"
    svc.delete_skill("my_Skill-2")
    assert svc.get_skill("my_Skill-2") is None
    svc.delete_skill("never-existed")  # 不存在的照舊不報錯


def test_folders_with_invalid_names_are_not_listed(svc):
    _write_skill(svc, "good-skill")
    resource_manager.write_file_text(
        svc._skills_dir / "good-skill.bak" / "SKILL.md", "---\nname: x\n---\n", make_parents=True
    )
    svc.load_config()
    assert {s["id"] for s in svc.list_skills()} == {"good-skill"}


def test_listing_failure_refuses_names_that_are_not_loaded(svc, monkeypatch):
    """目錄列不完整時不能當成「沒有同名資料夾」放行。"""

    def broken_walk(_resolved, report, **_options):
        report.errors.append(("", errno.EIO))
        yield from ()

    monkeypatch.setattr(resource_manager_module, "_walk_plain", broken_walk)
    with pytest.raises(OSError, match="Cannot list"):
        resource_manager.list_child_names(svc._skills_dir)
    assert svc.is_usable_name("new-skill") is False
    with pytest.raises(ToukaException):
        svc.update_skill("new-skill", {"system_prompt": "x"})


def test_list_child_names_lists_only_the_first_level(svc, tmp_path):
    root = tmp_path / "listing"
    resource_manager.write_file_text(root / "a-dir" / "inner" / "deep.txt", "x", make_parents=True)
    resource_manager.write_file_text(root / "top.txt", "x")
    assert sorted(resource_manager.list_child_names(root)) == ["a-dir", "top.txt"]
    assert resource_manager.list_child_names(root, dirs_only=True) == ["a-dir"]
    assert resource_manager.list_child_names(root / "missing") == []
    assert resource_manager.list_child_names(root / "top.txt") == []


async def test_activate_skill_refuses_a_path_name(svc, tmp_path):
    _write_outside_skill(tmp_path)
    with patch("src.tools.builtin.system.session_service") as session:
        session.read_persistence_snapshot = AsyncMock()
        session.conditional_save_session = AsyncMock()
        result = await activate_skill("../elsewhere", session_id="s1", identity=SOUL_OWNER)
    assert "Skill '../elsewhere' not found" in result
    session.read_persistence_snapshot.assert_not_awaited()
    session.conditional_save_session.assert_not_awaited()


# --- 系統技能 ---


def test_system_skill_cannot_be_overwritten_or_deleted(svc):
    _write_skill(svc, "sys-skill", is_system=True)
    with pytest.raises(ToukaException) as exc:
        svc.update_skill("sys-skill", {"system_prompt": "injected"})
    assert exc.value.code == ErrorCode.AUTH_PERMISSION_DENIED
    with pytest.raises(ToukaException) as exc:
        svc.delete_skill("sys-skill")
    assert exc.value.code == ErrorCode.AUTH_PERMISSION_DENIED
    assert svc.get_skill("sys-skill")["system_prompt"] == "body"


def test_system_skill_is_protected_even_when_not_loaded(svc):
    """載入的清單缺了它（例如沒掃到），仍從磁碟上的 SKILL.md 判斷。"""
    _write_skill(svc, "sys-skill", is_system=True)
    svc._metadata = {}
    with pytest.raises(ToukaException):
        svc.update_skill("sys-skill", {"system_prompt": "injected"})
    with pytest.raises(ToukaException):
        svc.delete_skill("sys-skill")
    assert resource_manager.exists(svc._skills_dir / "sys-skill" / "SKILL.md")


def test_case_alias_cannot_delete_a_system_skill(svc):
    _write_skill(svc, "system-skill-dispatcher", is_system=True)
    with pytest.raises(ToukaException) as exc:
        svc.delete_skill("System-Skill-Dispatcher")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert resource_manager.exists(svc._skills_dir / "system-skill-dispatcher" / "SKILL.md")


def test_scan_loads_every_skill_folder_however_many_files(svc):
    """掃描不能受遞迴搜尋的筆數上限影響：排在前面的技能有很多巢狀 SKILL.md 也一樣。"""
    _write_skill(svc, "0-first")
    for n in range(60):
        resource_manager.write_file_text(
            svc._skills_dir / "0-first" / "refs" / f"{n:02d}" / "SKILL.md", "x", make_parents=True
        )
    _write_skill(svc, "zz-system", is_system=True)
    svc.load_config()
    assert {s["id"] for s in svc.list_skills()} == {"0-first", "zz-system"}


# --- 新增與安裝 ---


@pytest.mark.parametrize("name", ["../x", "a/b", "name\n", "a.b"])
def test_skill_model_refuses_a_path_name(name):
    with pytest.raises(ValidationError):
        SkillModel(id=name, name="n", description="d", system_prompt="p")


def test_skill_model_schema_is_unchanged():
    assert SkillModel.model_json_schema()["properties"]["id"] == {"title": "Id", "type": "string"}
    assert SkillModel(id="ok-name", name="n", description="d", system_prompt="p").id == "ok-name"


@pytest.mark.parametrize("spec_id", ["name\n", "con", "real-skill"])
def test_creation_refuses_names_the_service_would_refuse(svc, spec_id):
    """結尾換行、保留名稱、和既有技能（``Real-Skill``）只差大小寫：驗證階段就擋，不等寫入。"""
    _write_skill(svc, "Real-Skill")
    report = SkillCreationKind(svc).validate(SkillSpec(id=spec_id, description="d"))
    assert report.ok is False


async def test_install_from_url_requires_https(svc):
    with (
        patch("src.services.skill_service.httpx.AsyncClient") as client,
        pytest.raises(ToukaException) as exc,
    ):
        await svc.install_skill_from_url("http://github.com/x/y.skill")
    assert exc.value.code == ErrorCode.AUTH_PERMISSION_DENIED
    client.assert_not_called()


@pytest.mark.parametrize("root", ["Real-Skill", "bad.name"])
async def test_install_from_url_refuses_a_bad_folder_name(svc, tmp_path, root):
    _write_skill(svc, "real-skill")
    with _url_install(_skill_archive(tmp_path, root)), pytest.raises(ToukaException) as exc:
        await svc.install_skill_from_url("https://github.com/x/y.skill")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert svc.get_skill("real-skill")["system_prompt"] == "body"
    assert root not in resource_manager.list_child_names(svc._skills_dir)


async def test_install_cannot_replace_a_system_skill(svc, tmp_path):
    _write_skill(svc, "sys-skill", is_system=True)
    with (
        _url_install(_skill_archive(tmp_path, "sys-skill")),
        pytest.raises(ToukaException) as exc,
    ):
        await svc.install_skill_from_url("https://github.com/x/y.skill")
    assert exc.value.code == ErrorCode.AUTH_PERMISSION_DENIED
    assert svc.get_skill("sys-skill")["system_prompt"] == "body"


async def test_install_package_cannot_declare_a_system_skill(svc, tmp_path):
    with (
        _url_install(_skill_archive(tmp_path, "new-skill", is_system=True)),
        pytest.raises(ToukaException) as exc,
    ):
        await svc.install_skill_from_url("https://github.com/x/y.skill")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert svc.get_skill("new-skill") is None


def _catalog_entry(catalog_id):
    """格式合格的條目（只有 id 有問題），名稱檢查拿掉時才會走到下載。"""
    return {
        "id": catalog_id, "name": "n", "category": "c", "description": "d",
        "repo": "owner/repo", "commit": "0" * 40, "base_path": "skills/x",
        "files": [{"path": "SKILL.md", "sha256": "0" * 64}],
    }


@pytest.mark.parametrize("catalog_id", ["../x", "Real-Skill"])
async def test_catalog_install_refuses_a_bad_id_before_downloading(svc, catalog_id):
    _write_skill(svc, "real-skill")
    with (
        patch("src.services.skill_service.SkillCatalogService") as catalog,
        patch("src.services.skill_service.httpx.AsyncClient") as client,
        pytest.raises(ToukaException) as exc,
    ):
        catalog.return_value.get_catalog.return_value = [_catalog_entry(catalog_id)]
        await svc.install_skill_from_catalog(catalog_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    client.assert_not_called()


# --- 內容快照 ---


@pytest.fixture
def snapshot_env(monkeypatch, tmp_path):
    point_zone_roots(monkeypatch, tmp_path, tmp_path / "zone_state")
    reached = AsyncMock(return_value={"blocks": []})
    monkeypatch.setattr("src.services.context_snapshot_service.get_context_snapshot", reached)
    monkeypatch.setattr(
        "src.services.context_snapshot_service.render_snapshot_markdown", lambda _s: "MD"
    )
    monkeypatch.setattr(agent_service, "get_all_agents", lambda: [{"id": "Alice"}])
    # 模擬不分大小寫的磁碟：去掉舊前綴、只差大小寫的寫法也「找得到」Alice 的資料夾
    monkeypatch.setattr(
        agent_service,
        "identify_agent",
        lambda agent_id: (
            {"agent_id": "Alice"}
            if agent_id.removeprefix("ToukaSoul").casefold() == "alice"
            else None
        ),
    )
    writes: list[str] = []

    def call(identity, path):
        app.dependency_overrides[get_verified_identity] = lambda: identity
        try:
            with TestClient(app) as client:
                monkeypatch.setattr(
                    resource_manager, "write_file_text", lambda p, *_a, **_k: writes.append(str(p))
                )
                return client.get(path)
        finally:
            app.dependency_overrides.clear()

    return call, writes, reached


@pytest.mark.parametrize("agent_id", ["../../evil", "..", "a/b", "a\\b"])
def test_snapshot_export_refuses_a_path_name(snapshot_env, agent_id):
    call, writes, reached = snapshot_env
    response = call(ADMIN, f"/v1/debug/context-snapshot/export?agent_id={quote(agent_id, safe='')}")
    assert response.status_code == 422
    assert not any("snapshots" in w for w in writes)
    reached.assert_not_awaited()


def test_snapshot_read_refuses_a_path_name(snapshot_env):
    call, _writes, reached = snapshot_env
    response = call(ADMIN, "/v1/debug/context-snapshot?agent_id=..%2F..%2Fevil")
    assert response.status_code == 422
    reached.assert_not_awaited()


SNAPSHOT_PATHS = ["/v1/debug/context-snapshot", "/v1/debug/context-snapshot/export"]


@pytest.mark.parametrize("path", SNAPSHOT_PATHS)
@pytest.mark.parametrize("caller", [ADMIN, SOUL_OWNER], ids=["admin", "soul-owner"])
@pytest.mark.parametrize("agent_id", ["GhostAgent", "alice", "ALICE", "ToukaSoulAlice"])
def test_snapshot_refuses_names_that_are_not_exactly_known(snapshot_env, path, caller, agent_id):
    call, writes, reached = snapshot_env
    response = call(caller, f"{path}?agent_id={agent_id}")
    assert response.status_code == 403
    assert not any("snapshots" in w for w in writes)
    reached.assert_not_awaited()


def test_snapshot_allows_a_known_agent_for_an_admin(snapshot_env):
    call, writes, reached = snapshot_env
    response = call(ADMIN, "/v1/debug/context-snapshot/export?agent_id=Alice")
    assert response.status_code == 200
    assert any(w.endswith("Alice.md") for w in writes)
    reached.assert_awaited_once()
