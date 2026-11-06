"""技能名稱與內容快照的代理人名稱不能被當成路徑（sec-9 PR 1a）。

技能名稱會接成 ``config/skills/<名稱>`` 資料夾；快照匯出會寫 ``snapshots/<代理人>.md``。
名稱只接受單一段的技能名稱（``is_skill_segment``），也不能只和已有的技能資料夾差大小寫：
不分大小寫的檔案系統上那會指到同一個資料夾、繞過「系統技能不可刪」。快照的代理人名稱要是
單一安全段，而且是系統認得的代理人。只用 tmp_path、沙盒與 mock，不碰 data/。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.skills import SkillModel
from src.services import skill_parser
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


@pytest.fixture
def svc(tmp_path):
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


def _skill_archive(tmp_path, root):
    src = tmp_path / "pkg" / root
    resource_manager.write_file_text(
        src / "SKILL.md", f"---\nname: {root}\ndescription: d\n---\nnew", make_parents=True
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


# --- 技能服務 ---


@pytest.mark.parametrize("name", BAD_NAMES)
def test_every_skill_operation_refuses_a_path_name(svc, tmp_path, name):
    assert svc.get_skill(name) is None
    assert svc.get_skill_resource(name, "SKILL.md") is None
    assert svc.list_skill_resources(name) == []
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


def test_case_alias_cannot_delete_a_system_skill(svc):
    _write_skill(svc, "system-skill-dispatcher", is_system=True)
    with pytest.raises(ToukaException) as exc:
        svc.delete_skill("System-Skill-Dispatcher")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert resource_manager.exists(svc._skills_dir / "system-skill-dispatcher" / "SKILL.md")


def test_case_alias_does_not_reach_an_existing_skill(svc):
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


async def test_activate_skill_refuses_a_path_name(svc, tmp_path):
    _write_outside_skill(tmp_path)
    with patch("src.tools.builtin.system.session_service") as session:
        session.conditional_save_session = AsyncMock()
        result = await activate_skill("../elsewhere", session_id="s1", identity=SOUL_OWNER)
    assert "not found" in result
    session.conditional_save_session.assert_not_awaited()


# --- 新增技能的入口 ---


@pytest.mark.parametrize("name", ["../x", "a/b", "name\n", "a.b"])
def test_skill_model_refuses_a_path_name(name):
    with pytest.raises(ValidationError):
        SkillModel(id=name, name="n", description="d", system_prompt="p")


def test_skill_model_schema_is_unchanged():
    assert SkillModel.model_json_schema()["properties"]["id"] == {"title": "Id", "type": "string"}
    assert SkillModel(id="ok-name", name="n", description="d", system_prompt="p").id == "ok-name"


def test_creation_refuses_an_id_with_a_trailing_newline(svc):
    report = SkillCreationKind(svc).validate(SkillSpec(id="name\n", description="d"))
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
    content = _skill_archive(tmp_path, root)
    with (
        patch("src.services.skill_service.httpx.AsyncClient", return_value=_mock_httpx(content)),
        pytest.raises(ToukaException) as exc,
    ):
        await svc.install_skill_from_url("https://github.com/x/y.skill")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert svc.get_skill("real-skill")["system_prompt"] == "body"
    assert root not in resource_manager.list_child_names(svc._skills_dir)


# --- 內容快照 ---


@pytest.fixture
def snapshot_env(monkeypatch, tmp_path):
    point_zone_roots(monkeypatch, tmp_path, tmp_path / "zone_state")
    reached = AsyncMock(return_value={"blocks": []})
    monkeypatch.setattr("src.services.context_snapshot_service.get_context_snapshot", reached)
    monkeypatch.setattr(
        "src.services.context_snapshot_service.render_snapshot_markdown", lambda _s: "MD"
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


def test_snapshot_export_refuses_an_unknown_agent(snapshot_env):
    call, writes, reached = snapshot_env
    response = call(SOUL_OWNER, "/v1/debug/context-snapshot/export?agent_id=GhostAgent")
    assert response.status_code == 403
    assert not any("snapshots" in w for w in writes)
    reached.assert_not_awaited()
