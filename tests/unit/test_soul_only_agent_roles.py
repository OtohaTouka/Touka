"""ADR-0245（U2a-3）：只有靈魂、DebugAgent、DispatchAgent 能有代理角色與能力。

- ``get_role_key``／``get_capabilities_by_id`` 的 override、tag 規則與 default_role 只對
  上述三類生效；其他 ID 一律 Base、無能力，每個被略過的設定項只記一次警告。
- tracked live 設定不改檔：讀到舊覆寫時略過；本檔只用 tmp／沙箱合成檔，不讀 live 設定。
- X-Agent-ID 只能切進靈魂；``POST /identity`` 拒絕非允許 ID，``GET /identity`` 與
  ``/identity/agents`` 只列允許 ID，存檔時保留檔內被略過的舊項不動。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from memory_server import config_utils
from src.core.exceptions import ToukaException
from src.main import app
from src.models.identity import ASSIGNABLE_AGENT_ROLES, AgentIdentity
from src.services.agent_service import agent_service
from src.services.identity_router import get_verified_identity
from src.services.identity_service import identity_service
from src.services.permission_service import PermissionService, permission_service

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLE_MAP = _REPO / "ToukaProxy" / "config" / "identity_map.json.example"
_LOGGER = "src.services.permission_service"
_CAPS = ["heartbeat_awakening", "can_supervise_dispatch"]

# 舊設定：自訂模型被覆寫成 Owner、is_custom 規則給 Owner——代表尚未手動清理的 live 檔。
_LEGACY_MAP = {
    "tag_rules": {
        "is_toukasoul": {"role": "Owner", "capabilities": _CAPS},
        "is_custom": {"role": "Owner", "capabilities": ["heartbeat_awakening"]},
    },
    "agent_overrides": {
        "CustomBot": {"role": "Owner", "capabilities": _CAPS},
        "Sumi": {"role": "Debugger", "capabilities": ["heartbeat_awakening"]},
        "DebugAgent": {"role": "Debugger", "capabilities": _CAPS},
        "DispatchAgent": {"role": "Base", "capabilities": ["can_supervise_dispatch"]},
    },
    "default_role": "Base",
}


def _ignored_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and "ADR-0245" in r.getMessage() and "ignored" in r.getMessage()
    ]


@pytest.fixture
def build(tmp_path):
    """以 tmp 合成 identity_map 建立真載入的 PermissionService（自動重載關閉）。"""

    def _build(identity: dict) -> tuple[PermissionService, Path]:
        PermissionService.reset_singleton()
        ident = tmp_path / "identity_map.json"
        ident.write_text(json.dumps(identity), encoding="utf-8")
        svc = PermissionService(
            config_path=str(tmp_path / "permissions.json"), identity_path=str(ident)
        )
        svc.disable_auto_reload()
        return svc, ident

    yield _build
    PermissionService.reset_singleton()


# ── get_role_key／get_capabilities_by_id：只有三類套規則 ───────────────────────


def test_non_soul_override_is_ignored_once_and_file_unchanged(build, caplog):
    svc, path = build(_LEGACY_MAP)
    before = path.read_bytes()
    with caplog.at_level("WARNING", logger=_LOGGER):
        assert svc.get_role_key("CustomBot") == "Base"
        assert svc.get_role_key("CustomBot", is_custom=True) == "Base"
        assert svc.get_capabilities_by_id("CustomBot") == []
        assert svc.get_capabilities_by_id("CustomBot", is_custom=True) == []
    warnings = _ignored_warnings(caplog)
    assert len(warnings) == 1
    assert "agent_overrides.CustomBot" in warnings[0]
    assert path.read_bytes() == before


def test_custom_tag_rule_is_ignored(build, caplog):
    svc, _ = build(_LEGACY_MAP)
    with caplog.at_level("WARNING", logger=_LOGGER):
        assert svc.get_role_key("SomeModel", is_custom=True) == "Base"
        assert svc.get_capabilities_by_id("SomeModel", is_custom=True) == []
        assert svc.get_role_key("OtherModel", is_custom=True) == "Base"
    warnings = _ignored_warnings(caplog)
    assert len(warnings) == 1
    assert "tag_rules.is_custom" in warnings[0]


def test_non_base_default_role_does_not_reach_other_ids(build, caplog):
    svc, _ = build({"tag_rules": {}, "agent_overrides": {}, "default_role": "Owner"})
    with caplog.at_level("WARNING", logger=_LOGGER):
        assert svc.get_role_key("RandomModel") == "Base"
        assert svc.get_role_key("AnotherModel") == "Base"
    warnings = _ignored_warnings(caplog)
    assert len(warnings) == 1
    assert "default_role" in warnings[0]


def test_souls_and_system_agents_keep_their_rules(build, caplog):
    svc, _ = build(_LEGACY_MAP)
    for soul in ("Sumi", "NewSoul", "Tagged"):
        _make_soul_dir(soul)  # R-2a：靈魂旗標要經原始 ID 確認
    with caplog.at_level("WARNING", logger=_LOGGER):
        assert svc.get_role_key("Sumi", is_toukasoul=True) == "Debugger"
        assert svc.get_role_key("NewSoul", is_toukasoul=True) == "Owner"
        assert svc.get_capabilities_by_id("NewSoul", is_toukasoul=True) == _CAPS
        tagged = AgentIdentity(agent_id="Tagged", capabilities=["is_toukasoul"])
        assert svc.get_role_key(tagged) == "Owner"
        assert svc.get_role_key("DebugAgent") == "Debugger"
        assert svc.get_capabilities_by_id("DebugAgent") == _CAPS
        assert svc.get_role_key("DispatchAgent") == "Base"
        assert svc.get_capabilities_by_id("DispatchAgent") == ["can_supervise_dispatch"]
    assert _ignored_warnings(caplog) == []


def test_soul_override_needs_the_soul_flag(build):
    """覆寫以 ID 為鍵，但沒帶靈魂旗標（呼叫端沒判定成靈魂）就不套用。"""
    svc, _ = build(_LEGACY_MAP)
    _make_soul_dir("Sumi")  # 真的靈魂沒帶旗標也一樣（LDH 等無旗標路徑不變寬）
    assert svc.get_role_key("Sumi") == "Base"
    assert svc.get_capabilities_by_id("Sumi") == []


# ── X-Agent-ID：只能切進靈魂 ─────────────────────────────────────────────────


def _make_soul_dir(agent_id: str) -> None:
    soul_dir = Path(config_utils.get_app_data_dir()) / "soul" / agent_id
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "soul.md").write_text(f"I am {agent_id}", encoding="utf-8")
    (soul_dir / "config.json").write_text(json.dumps({"is_toukasoul": True}), encoding="utf-8")


async def _assume(driver: AgentIdentity, target: str) -> AgentIdentity:
    creds = MagicMock()
    creds.credentials = "human_token"
    req = MagicMock()
    req.headers = {"X-Agent-ID": target}
    req.client.host = "127.0.0.1"
    req.state = MagicMock()
    with patch("src.services.identity_service.identity_service.verify_token", return_value=driver):
        return await get_verified_identity(req, creds)


ADMIN_DRIVER = AgentIdentity.for_role("AdminU", "admin", is_human=True, owned_agents=[])
OWNER_DRIVER = AgentIdentity.for_role(
    "OwnerU", "normal", is_human=True, owned_agents=["MyBot", "AssumeSoul"]
)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["CustomBot", "ToukaGroup", "DebugAgent", "DispatchAgent"])
async def test_admin_cannot_assume_non_soul(target):
    with pytest.raises(ToukaException) as exc:
        await _assume(ADMIN_DRIVER, target)
    assert exc.value.status_code == 403
    assert exc.value.code.value == "AUTH_PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_owner_cannot_assume_owned_non_soul():
    with pytest.raises(ToukaException) as exc:
        await _assume(OWNER_DRIVER, "MyBot")
    assert exc.value.status_code == 403
    assert exc.value.code.value == "AUTH_PERMISSION_DENIED"


@pytest.fixture
def example_map():
    """沙箱 identity_map 換成出貨範本（靈魂→Owner）；測後還原。"""
    target = permission_service.identity_path
    original = target.read_bytes() if target.exists() else None
    target.write_text(_EXAMPLE_MAP.read_text(encoding="utf-8"), encoding="utf-8")
    permission_service.reload_all()
    yield
    if original is None:
        target.unlink(missing_ok=True)
    else:
        target.write_bytes(original)
    permission_service.reload_all()


def test_legacy_prefixed_soul_id_is_base_everywhere(example_map):
    """R-2a：``ToukaSoul<靈魂>`` 會被 identify_agent 糾偏成靈魂，但角色依原始 ID 判定。"""
    _make_soul_dir("PfxSoul")
    legacy = "ToukaSoulPfxSoul"
    assert agent_service.resolve_role_and_caps(legacy) == ("Base", [])
    by_id = identity_service.get_identity(legacy)
    assert (by_id.agent_role, by_id.capabilities) == ("Base", [])
    assert permission_service.can_awaken(by_id) is False
    token = identity_service.issue_token(AgentIdentity(agent_id=legacy))
    verified = identity_service.verify_token(token)
    assert verified is not None
    assert (verified.agent_id, verified.agent_role, verified.capabilities) == (legacy, "Base", [])
    assert permission_service.get_role_key(legacy, is_toukasoul=True) == "Base"
    assert permission_service.get_capabilities_by_id(legacy, is_toukasoul=True) == []


def test_real_soul_id_keeps_owner_and_capabilities(example_map):
    _make_soul_dir("PfxSoul")
    assert agent_service.resolve_role_and_caps("PfxSoul") == ("Owner", _CAPS)
    by_id = identity_service.get_identity("PfxSoul")
    assert (by_id.agent_role, by_id.capabilities) == ("Owner", _CAPS)
    assert permission_service.can_awaken(by_id) is True
    verified = identity_service.verify_token(
        identity_service.issue_token(AgentIdentity(agent_id="PfxSoul"))
    )
    assert verified is not None
    assert (verified.agent_role, verified.capabilities) == ("Owner", _CAPS)


@pytest.mark.asyncio
async def test_owner_and_admin_can_still_assume_a_soul(example_map):
    _make_soul_dir("AssumeSoul")
    owner_view = await _assume(OWNER_DRIVER, "AssumeSoul")
    assert (owner_view.agent_id, owner_view.agent_role, owner_view.human_role) == (
        "AssumeSoul",
        "Owner",
        None,
    )
    admin_view = await _assume(ADMIN_DRIVER, "AssumeSoul")
    assert (admin_view.agent_id, admin_view.agent_role, admin_view.human_role) == (
        "AssumeSoul",
        None,
        "admin",
    )


# ── /identity 路由：只管允許的 ID ───────────────────────────────────────────────

ADMIN = AgentIdentity.for_role("AdminUser", "admin", is_human=True)


@pytest.fixture
def admin_client():
    """沙箱 identity_map 換成合成的舊設定；測後還原。"""
    target = permission_service.identity_path
    original = target.read_bytes() if target.exists() else None
    target.write_text(json.dumps(_LEGACY_MAP), encoding="utf-8")
    permission_service.reload_all()
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    try:
        with TestClient(app) as client:
            yield client, target
    finally:
        app.dependency_overrides.clear()
        if original is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(original)
        permission_service.reload_all()


def test_post_identity_rejects_non_role_holders(admin_client):
    client, target = admin_client
    before = target.read_bytes()
    body = {
        "tag_rules": {},
        "agent_overrides": {
            "DebugAgent": {"role": "Debugger", "capabilities": []},
            "CustomBot": {"role": "Owner", "capabilities": []},
            "ToukaGroup": {"role": "Base", "capabilities": ["heartbeat_awakening"]},
        },
        "default_role": "Base",
    }
    resp = client.post("/v1/identity", json=body)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "VALIDATION_ERROR"
    assert detail["rejected_agent_ids"] == ["CustomBot", "ToukaGroup"]
    assert target.read_bytes() == before


def test_get_hides_and_post_keeps_ignored_overrides(admin_client):
    client, target = admin_client
    _make_soul_dir("Sumi")
    current = client.get("/v1/identity").json()
    assert sorted(current["agent_overrides"]) == ["DebugAgent", "DispatchAgent", "Sumi"]

    current["agent_overrides"]["Sumi"] = {"role": "Owner", "capabilities": _CAPS}
    del current["agent_overrides"]["DispatchAgent"]
    resp = client.post("/v1/identity", json=current)
    assert resp.status_code == 200, resp.text

    saved = json.loads(target.read_text(encoding="utf-8"))["agent_overrides"]
    assert saved == {
        "CustomBot": {"role": "Owner", "capabilities": _CAPS},
        "Sumi": {"role": "Owner", "capabilities": _CAPS},
        "DebugAgent": {"role": "Debugger", "capabilities": _CAPS},
    }


def test_identity_agents_lists_only_role_holders(admin_client):
    client, _ = admin_client
    _make_soul_dir("ListedSoul")
    agents = [
        {"id": "ListedSoul", "type": "soul"},
        {"id": "DebugAgent", "type": "registered"},
        {"id": "DispatchAgent", "type": "registered"},
        {"id": "CustomBot", "type": "custom"},
        {"id": "ToukaGroup", "type": "group"},
        {"id": "PlaceholderSoul", "type": "soul_placeholder"},
    ]
    with patch("src.routers.identity.agent_service.get_all_agents", return_value=agents):
        resp = client.get("/v1/identity/agents")
    assert resp.status_code == 200, resp.text
    assert [a["id"] for a in resp.json()] == ["ListedSoul", "DebugAgent", "DispatchAgent"]


# ── 前端角色下拉選單與後端可指派角色一致（漂移防護）─────────────────────────────


def test_frontend_assignable_roles_match_backend():
    source = (
        _REPO / "ToukaChat" / "src" / "components" / "settings" / "tabs" / "security"
        / "IdentityMapping.tsx"
    ).read_text(encoding="utf-8")
    match = re.search(r"ASSIGNABLE_AGENT_ROLES\s*=\s*\[([^\]]*)\]", source)
    assert match, "IdentityMapping.tsx must declare ASSIGNABLE_AGENT_ROLES"
    frontend = set(re.findall(r"['\"](\w+)['\"]", match.group(1)))
    assert frontend == {"Owner", "Base", "Debugger"}
    assert frontend == set(ASSIGNABLE_AGENT_ROLES)
