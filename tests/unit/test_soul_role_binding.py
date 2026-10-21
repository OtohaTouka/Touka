"""#4-B（ADR-0147）——靈魂建立即定角色（類型硬綁定）＋id 撞名防禦。

根因（清空重啟後實測）：靈魂→角色的 SSOT ＝ ``identity_map.json`` 的
``tag_rules``/``agent_overrides``（permission_service.get_role_key）。該檔在
fresh checkout／清空重啟後**不存在**→ 退 ``DEFAULT_IDENTITY_MAP(default_role=Base)``
→ 所有靈魂＋DebugAgent 全塌 Base（「Sumi 掉 Owner」＋除錯/計分卡失權）。且
``verify_token`` 等站點呼叫 ``get_role_key(agent_id)`` **不帶類型旗標**，休眠的
``tag_rules["is_toukasoul"]`` 從不觸發。

修法（owner 三拍板）：
1. 類型硬綁定（tag_rules）：is_toukasoul→Owner、native→Base（ADR-0245 起自訂模型
   不再有規則，落到 default Base）；
   種入 identity_map.json.example ＋ 加進 bootstrap ESSENTIAL_CONFIGS 自癒。
2. 接線：agent_service.resolve_role_and_caps 經 identify_agent 帶旗標，套到
   verify_token / get_identity / identity_router 全站，讓 tag_rules 真的觸發。
3. 建立時禁止同名：create_soul 拒 agent_id 撞任何 user.id（Decision 1a）。

conftest 已從 .example 播種 sandbox identity_map（生產自癒同一份）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from memory_server import config_utils
from src.config import get_config_dir
from src.main import app
from src.models.identity import AgentIdentity, IdentityMapConfig
from src.services.agent_service import agent_service
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.identity_service import identity_service
from src.services.models import model_manager
from src.services.permission_service import permission_service
from src.utils import bootstrap
from src.utils.bootstrap import ESSENTIAL_CONFIGS

_EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "ToukaProxy" / "config" / "identity_map.json.example"
)


def _make_soul_dir(agent_id: str, *, is_toukasoul: bool = True) -> None:
    """在沙箱建立最小靈魂目錄（soul_service.is_toukasoul 讀 dir＋config.json 旗標）。"""
    soul_dir = Path(config_utils.get_app_data_dir()) / "soul" / agent_id
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "soul.md").write_text(f"我是 {agent_id}", encoding="utf-8")
    (soul_dir / "config.json").write_text(
        json.dumps({"is_toukasoul": is_toukasoul}), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _seed_identity_map():
    """以 .example 內容 seed 沙箱 identity_map＋重載（覆蓋 setup_test_environment 的
    每測試刪檔回 DEFAULT）。生產自癒種同一份，故測試＝生產基線。寫入
    permission_service.identity_path＝它實際讀取的路徑（免 os.environ）。"""
    target = permission_service.identity_path
    target.write_text(_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    permission_service.reload_all()
    yield
    target.unlink(missing_ok=True)
    permission_service.reload_all()


# ── tag_rules 類型硬綁定：get_role_key 帶旗標 ────────────────────────────────────


def test_tag_rules_map_types_to_roles():
    """播種的 identity_map：is_toukasoul→Owner、is_custom→Base（ADR-0245）、無旗標→Base。"""
    _make_soul_dir("Whoever")  # R-2a：靈魂旗標要經原始 ID 確認
    assert permission_service.get_role_key("Whoever", is_toukasoul=True) == "Owner"
    assert permission_service.get_role_key("Whoever", is_custom=True) == "Base"
    assert permission_service.get_role_key("Whoever") == "Base"


def test_debugagent_override_beats_tag_rules():
    """agent_overrides 優先於 tag_rules：DebugAgent→Debugger（即使被判為靈魂）。"""
    assert permission_service.get_role_key("DebugAgent", is_toukasoul=True) == "Debugger"


# ── resolve_role_and_caps：靈魂目錄→Owner（核心接線）───────────────────────────


def test_resolve_role_and_caps_soul_is_owner():
    """有靈魂目錄的 agent → identify_agent 判 is_toukasoul → Owner（不再全塌 Base）。

    現況紅：resolve_role_and_caps 尚未存在／未帶旗標 → Base。
    caps 須含 heartbeat_awakening（靈魂核心功能＝可被心跳自主喚醒；審查揪出的
    能力面缺漏——修好角色須一併恢復能力）。
    """
    _make_soul_dir("RoleSoulA")
    role, caps = agent_service.resolve_role_and_caps("RoleSoulA")
    assert role == "Owner"
    assert "heartbeat_awakening" in caps


def test_soul_can_be_heartbeat_woken():
    """靈魂經 get_identity 解析後 can_awaken 為 True（心跳喚醒能力隨角色一併恢復）。

    對釘審查發現：若 .example 只給 role 不給 heartbeat_awakening cap，靈魂拿回
    Owner 卻仍無法被心跳喚醒（heartbeat.py:478 can_awaken 檢 SOUL 自身能力）。
    """
    _make_soul_dir("WakeableSoul")
    ident = identity_service.get_identity("WakeableSoul")
    assert ident.agent_role == "Owner"
    assert permission_service.can_awaken(ident) is True


def test_resolve_role_and_caps_unknown_is_base():
    """完全未知的 agent（無靈魂/custom/registry）→ default Base。"""
    role, _caps = agent_service.resolve_role_and_caps("TotallyUnknownAgent")
    assert role == "Base"


def test_get_all_agents_exposes_effective_role_tag_rules_aware():
    """get_all_agents 為每個 agent 附 tag_rules-aware 有效角色/能力（IdentityMapping
    UI 據此顯示真實角色，不再對無 override 的靈魂一律顯示 default_role Base）。"""
    _make_soul_dir("EffSoul")
    agents = agent_service.get_all_agents()
    soul = next(a for a in agents if a["id"] == "EffSoul")
    assert soul["effective_role"] == "Owner"  # tag_rules is_toukasoul→Owner，非 Base
    assert "heartbeat_awakening" in soul["effective_capabilities"]
    assert soul["has_override"] is False  # 純繼承、無 override


def test_resolve_role_and_caps_debugagent_is_debugger():
    """DebugAgent（override）→ Debugger，不論類型判定。"""
    role, _caps = agent_service.resolve_role_and_caps("DebugAgent")
    assert role == "Debugger"


# ── verify_token：靈魂 token → Owner（整合，接線的真實效果）─────────────────────


def test_verify_token_resolves_soul_as_owner():
    """靈魂發的 agent token → agent_role=Owner、is_human=False（Sumi 掉 Owner 對釘）。

    現況紅：verify_token 呼叫 get_role_key(agent_id) 不帶旗標 → Base。
    """
    _make_soul_dir("TokenSoul")
    token = identity_service.issue_token(AgentIdentity(agent_id="TokenSoul"))
    ident = identity_service.verify_token(token)
    assert ident is not None
    assert ident.is_human is False
    assert ident.agent_role == "Owner"


def test_get_identity_resolves_soul_as_owner():
    """get_identity（heartbeat/排程路徑）同樣把靈魂解析為 Owner。"""
    _make_soul_dir("PulseSoul")
    ident = identity_service.get_identity("PulseSoul")
    assert ident.agent_role == "Owner"


# ── identity_router：擁有者代打自己的靈魂 → Owner ──────────────────────────────


@pytest.mark.asyncio
async def test_owner_assumes_owned_soul_becomes_owner():
    """非 admin 人類擁有者代打自己的靈魂 → 該靈魂解析為 Owner（而非 Base）。"""
    _make_soul_dir("MyOwnedSoul")
    driver = AgentIdentity.for_role(
        agent_id="HumanU", role="normal", is_human=True, owned_agents=["MyOwnedSoul"]
    )
    creds = MagicMock()
    creds.credentials = "human_token"
    req = MagicMock()
    req.headers = {"X-Agent-ID": "MyOwnedSoul"}
    req.client.host = "127.0.0.1"
    req.state = MagicMock()

    with patch("src.services.identity_service.identity_service.verify_token", return_value=driver):
        result = await get_verified_identity(req, creds)

    assert result.agent_id == "MyOwnedSoul"
    assert result.is_human is False
    assert result.agent_role == "Owner"


# ── create_soul：建立時禁止 id 撞 user.id（Decision 1a）──────────────────────────


def test_create_soul_rejects_collision_with_user_id():
    """create_soul 的 agent_id 撞任何現存 user.id → 422 拒建（身分二義性防禦）。"""
    auth = AuthService()
    user = auth.create_user("collide@test.com", "CollidePass123!", role="normal")
    admin = AgentIdentity.for_role(agent_id="AdminBoss", role="admin", is_human=True)
    app.dependency_overrides[get_verified_identity] = lambda: admin
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # 撞名：以 user.id 當 soul agent_id
            r = client.post(
                "/v1/souls",
                json={"agent_id": user.id, "config": {"base_model": "claude-test-brain"}},
            )
        assert r.status_code == 422, r.text
        assert "collide" in r.text.lower() or "user id" in r.text.lower()
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def selectable_test_brain():
    """建立靈魂會先驗主要模型（H9）：前置資料用的 claude-test-brain 種成清單裡可選的一般模型.

    建立時會把模型清單寫進 models.json；結束時還原，種入的模型不留給後面的測試。
    """
    models_json = Path(get_config_dir()) / "models.json"
    original = models_json.read_bytes() if models_json.exists() else None
    model_manager.set_models(
        [*model_manager._models_cache, {"id": "claude-test-brain", "owned_by": "anthropic"}]
    )
    yield
    if original is None:
        models_json.unlink(missing_ok=True)
    else:
        models_json.write_bytes(original)


@pytest.mark.usefixtures("selectable_test_brain")
def test_create_soul_allows_non_colliding_id():
    """不撞名的 agent_id → 正常建立（回歸保護，確認守衛不過度攔截）。"""
    AuthService().create_user("noncollide@test.com", "NonCollide123!", role="normal")
    admin = AgentIdentity.for_role(agent_id="AdminBoss2", role="admin", is_human=True)
    app.dependency_overrides[get_verified_identity] = lambda: admin
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post(
                "/v1/souls",
                json={"agent_id": "FreshUniqueSoul", "config": {"base_model": "claude-test-brain"}},
            )
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides.clear()


# ── 自癒：identity_map.json 進 ESSENTIAL_CONFIGS，ensure_configs 從 .example 建 ──


def test_identity_map_in_essential_configs():
    """identity_map.json 必須在 ESSENTIAL_CONFIGS（否則 fresh checkout 全塌 Base）。"""
    assert "identity_map.json" in ESSENTIAL_CONFIGS


def test_ensure_configs_restores_identity_map_from_example(tmp_path, monkeypatch):
    """fresh checkout：ensure_configs 從 .example 還原 identity_map.json（自癒閉環）。

    對釘「清空重啟後 identity_map 缺席 → 全塌 Base」：restore 後的 live 檔須含
    tag_rules（is_toukasoul→Owner），且能通過 IdentityMapConfig 驗證（validate_live）。
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copyfile(_EXAMPLE, cfg / "identity_map.json.example")
    monkeypatch.setattr(bootstrap, "CONFIG_DIR", str(cfg))

    restored = bootstrap.ensure_configs()

    live = cfg / "identity_map.json"
    assert live.exists(), "ensure_configs 未從 .example 還原 identity_map.json"
    assert "identity_map.json" in restored
    cfg_obj = IdentityMapConfig.model_validate(json.loads(live.read_text(encoding="utf-8")))
    assert cfg_obj.tag_rules["is_toukasoul"].role == "Owner"


def test_example_seed_maps_soul_to_owner():
    """identity_map.json.example 種子語意：is_toukasoul→Owner、DebugAgent→Debugger。"""
    example = (
        Path(__file__).resolve().parents[2]
        / "ToukaProxy" / "config" / "identity_map.json.example"
    )
    data = json.loads(example.read_text(encoding="utf-8"))
    cfg = IdentityMapConfig.model_validate(data)  # 型別合法
    assert cfg.tag_rules["is_toukasoul"].role == "Owner"
    assert set(cfg.tag_rules) == {"is_toukasoul"}
    assert cfg.agent_overrides["DebugAgent"].role == "Debugger"
    assert cfg.default_role == "Base"
