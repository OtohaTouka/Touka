"""C1 — 靈魂 CRUD 真實落盤測試（soul-crud-repair-and-admin-stepup 提案 C1；修復前＝紅）。

診斷（2026-07-10）：create_soul/update_soul 經 resource_manager.write_file_text/write_json
寫入 <app_data>/soul/<id>/，被 Soul Protection 路徑監獄（is_protected_path 對 soul/legacy
下一切寫刪**無條件** raise PermissionError）擋下 → 未捕捉 500；且 :45 make_dir 先成功
→ 留空孤兒目錄、同 id 重試撞 SOUL_ALREADY_EXISTS。先前 souls 測試把 write_* monkeypatch
成 no-op ＝ 真實落盤路徑零涵蓋——本檔補上（全部走 conftest 會話沙箱、每測試自動清
soul/，絕不觸碰真實 data/）。

修復（F2，D1）＝寫入改走受認可通道 soul_io（_safe_target 監獄＋原子寫＋自動 .bak）；
保護原語一行不動——本檔含**防退化錨**（直呼 resource_manager 寫/刪 soul 路徑必須照擋）。
另鎖 D6 回滾（失敗不留孤兒、不誤刪既存資料）與 agent_id/soul_id 路徑逃逸拒絕。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from src.config import get_config_dir
from src.core.exceptions import ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.models.souls import SoulUpdateRequest
from src.routers import souls as souls_mod
from src.routers.souls import update_soul
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager

ADMIN = AgentIdentity.for_role(agent_id="AdminH", role="admin", is_human=True)


def _app_data() -> Path:
    return Path(resource_manager.get_base_path())


def _config_dir() -> Path:
    return Path(get_config_dir())


def _create_payload(agent_id: str, base_model: str | None = "claude-test-brain") -> dict:
    return {
        "agent_id": agent_id,
        "name": agent_id,
        "persona": f"我是 {agent_id} 的測試人格",
        "config": {"base_model": base_model},
    }


@pytest.fixture(autouse=True)
def registry_snapshot():
    """models.json／agents_registry.json 逐測試快照還原（會話沙箱跨測試共享，
    本檔的註冊寫入不得洩漏污染其他測試）；**無條件**回到空 dict＝全新安裝形態。

    無條件佈 {}（非「缺檔才佈」）＋同步 model_manager 快取：本檔斷言的是「fresh-install
    註冊」形態，而 model_manager._models_cache 是會話共享單例——前置測試遺留其中的
    touka/proxy 模型，會被 TestClient 啟動的 check_models_health 背景任務以 _save_config
    整包回寫、覆蓋掉 create 剛註冊的 ToukaSoul<id>（隨機次序 flake 根因）。歸零快取使
    背景健檢對空快取即早退、無從覆蓋（生產面根因另由 model_manager SSOT 化修復，ADR-0145）。
    """
    cfg = _config_dir()
    files = [cfg / "models.json", cfg / "agents_registry.json"]
    original = {f: (f.read_text(encoding="utf-8") if f.exists() else None) for f in files}
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}", encoding="utf-8")
    model_manager.load_initial_config()  # 快取歸零，與剛佈的空 models.json 一致
    yield
    for f, content in original.items():
        if content is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def selectable_test_brain(registry_snapshot):
    """建立靈魂會先驗主要模型（H9）：前置資料用的 claude-test-brain 種成清單裡可選的一般模型."""
    model_manager.set_models(
        [*model_manager._models_cache, {"id": "claude-test-brain", "owned_by": "anthropic"}]
    )


@pytest.fixture
def admin_client():
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


class _InjectedRegistrationFailure(RuntimeError):
    """測試注入的註冊步失敗（模擬 create 中途炸掉以驗證 D6 回滾）。"""


def _boom(*_a, **_k):
    raise _InjectedRegistrationFailure


# ── create：真實落盤＋全 SSOT 註冊 ─────────────────────────────────────────────


def test_create_soul_persists_files_and_registrations(admin_client):
    """POST /v1/souls → 200＋soul.md/config.json 實體落盤＋models/registry/brain 全註冊。

    現況紅：write_file_text 於 souls.py:53 被 Soul Protection 擋 → 500、全部缺席。
    註冊面同時鎖「全新安裝」形態：沙箱 registry 以空 {} 佈置——舊碼 `if not registry:
    return` 會沉默跳過（fresh install 永遠註冊不進去），F2 一併修為只跳過 None。
    """
    r = admin_client.post("/v1/souls", json=_create_payload("CrudSoulA"))
    assert r.status_code == 200, r.text

    soul_dir = _app_data() / "soul" / "CrudSoulA"
    assert (soul_dir / "soul.md").read_text(encoding="utf-8") == "我是 CrudSoulA 的測試人格"
    config = json.loads((soul_dir / "config.json").read_text(encoding="utf-8"))
    assert config["is_toukasoul"] is True  # 辨識鏈（soul_service.is_toukasoul）所需旗標
    assert config["base_model"] == "claude-test-brain"

    models = json.loads((_config_dir() / "models.json").read_text(encoding="utf-8"))
    assert any(m.get("id") == "ToukaSoulCrudSoulA" for m in models.get("models", []))
    registry = json.loads((_config_dir() / "agents_registry.json").read_text(encoding="utf-8"))
    assert registry.get("CrudSoulA") == ["CrudSoulA"]
    brains = json.loads(
        (_app_data() / "vault" / "brain_settings.json").read_text(encoding="utf-8")
    )
    assert brains["CrudSoulA"]["active_brain"] == "claude-test-brain"


def test_create_soul_does_not_write_identity_map_override(admin_client):
    """G18（守衛涵蓋缺口／ADR-0158）：create_soul 刻意**不得**寫 identity_map override。

    角色面由 tag_rules is_toukasoul→Owner（ADR-0147）涵蓋；建立時寫 override 會製造
    「override 蓋 tag_rules」的降級風險——正是 Sumi 降級事故成因。因寫入的 override 值
    通常＝Owner、不會讓其他 create 測試變紅，故需專門鎖住此**負向**不變式。
    """
    r = admin_client.post("/v1/souls", json=_create_payload("G18NoOverride"))
    assert r.status_code == 200, r.text

    # 磁碟面：create 若寫 override（經 update_identity 或直寫）identity_map.json 會出現該條目
    idmap = _config_dir() / "identity_map.json"
    if idmap.exists():
        overrides = json.loads(idmap.read_text(encoding="utf-8")).get("agent_overrides", {})
        assert "G18NoOverride" not in overrides, (
            "create_soul 在磁碟 identity_map 寫了 override（Sumi footgun 回歸；ADR-0158/G18）"
        )
    # 記憶體單例：update_identity 寫入會 reload 快取，故經該通道的寫入亦會反映於此
    assert "G18NoOverride" not in permission_service.identity_map.agent_overrides, (
        "create_soul 寫了 identity_map override＝角色降級風險（應由 tag_rules 涵蓋，ADR-0158/G18）"
    )


def test_registry_snapshot_fixture_zeroes_cache_unconditionally():
    """G21（守衛涵蓋缺口／ADR-0145）：本檔 registry_snapshot autouse fixture 必須無條件把
    models.json/agents_registry.json 佈為 {} 並呼叫 load_initial_config() 歸零會話共享單例
    快取（defense-in-depth，阻背景 check_models_health 跨測試回寫覆蓋、消隨機次序 flake）。

    分歧的姊妹 fixture（test_soul_admin_stepup 的條件寫、無 load_initial_config 呼叫）正證明
    此不變式先前無執法——本 meta-test 以源碼守衛鎖本檔 fixture 的無條件歸零行為。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    start = src.index("def registry_snapshot(")
    region = src[start:src.index("def admin_client(", start)]  # fixture 定義於 admin_client 之前
    assert 'write_text("{}"' in region, (
        "registry_snapshot 未無條件把 models/registry 佈為 {}（快取歸零 defense-in-depth 破壞）"
    )
    assert "load_initial_config()" in region, (
        "registry_snapshot 未呼叫 model_manager.load_initial_config() 歸零單例快取"
        "（背景健檢會以過時快取回寫覆蓋＝隨機次序 flake 回歸）"
    )


# ── update：真實落盤＋soul_io 自動備份 ─────────────────────────────────────────


def test_update_soul_persists_and_creates_backup():
    """POST /v1/souls/{id} → 200＋soul.md 更新＋.bak 保留舊內容（soul_io 自動備份）。

    現況紅：souls.py:175 write_file_text 被 Soul Protection 擋 → 500。
    C3 後人類編輯須步升密碼（ADR-0142 D3），故用真實 admin 使用者＋密碼。
    """
    auth = AuthService()
    pw = "CrudUpdatePass123!"
    user = auth.create_user("crud-update@test.com", pw, role="admin")
    identity = AgentIdentity.for_role(user.id, "admin", is_human=True)
    app.dependency_overrides[get_verified_identity] = lambda: identity

    soul_dir = _app_data() / "soul" / "CrudSoulB"
    soul_dir.mkdir(parents=True)
    (soul_dir / "soul.md").write_text("舊人格", encoding="utf-8")
    (soul_dir / "config.json").write_text(
        json.dumps({"is_toukasoul": True}), encoding="utf-8"
    )

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/v1/souls/CrudSoulB", json={"persona": "新人格", "admin_password": pw})
    app.dependency_overrides.clear()
    assert r.status_code == 200, r.text
    assert (soul_dir / "soul.md").read_text(encoding="utf-8") == "新人格"
    assert (soul_dir / "soul.md.bak").read_text(encoding="utf-8") == "舊人格"


# ── D6 回滾：失敗不留孤兒、同 id 可重試、不誤刪既存資料 ────────────────────────


def test_create_failure_rolls_back_dirs_and_id_reusable(admin_client, monkeypatch):
    """建立中途失敗 → 三目錄回滾（不留孤兒）＋同 id 重試成功。

    現況紅：500 發生在寫入步（更早），孤兒 soul/ 目錄殘留 → 重試撞 SOUL_ALREADY_EXISTS
    ——即使用者「建立失敗後連清都清不掉」的直接根源。
    """
    monkeypatch.setattr(souls_mod, "_update_models_config", _boom)
    r1 = admin_client.post("/v1/souls", json=_create_payload("CrudSoulC"))
    assert r1.status_code >= 400
    assert not (_app_data() / "soul" / "CrudSoulC").exists(), "失敗不得留下孤兒目錄"
    for base in ("personal_drive", "legacy"):
        assert not (_app_data() / base / "CrudSoulC").exists()

    monkeypatch.undo()
    r2 = admin_client.post("/v1/souls", json=_create_payload("CrudSoulC"))
    assert r2.status_code == 200, f"孤兒清除後同 id 必須可重建: {r2.text}"


def test_create_failure_rollback_spares_preexisting_dirs(admin_client, monkeypatch):
    """回滾只撤銷「本次新建」的目錄——先於建立即存在的 personal_drive 資料不得被誤刪。"""
    pd = _app_data() / "personal_drive" / "CrudSoulD"
    pd.mkdir(parents=True)
    (pd / "keep.txt").write_text("珍貴資料", encoding="utf-8")

    monkeypatch.setattr(souls_mod, "_update_models_config", _boom)
    r = admin_client.post("/v1/souls", json=_create_payload("CrudSoulD"))
    assert r.status_code >= 400
    assert (pd / "keep.txt").read_text(encoding="utf-8") == "珍貴資料"
    assert not (_app_data() / "soul" / "CrudSoulD").exists()


# ── fail-loud：soul_io 寫入失敗 → 結構化 SOUL_WRITE_FAILED（非裸 500）──────────


def test_create_write_failure_fails_loud_with_error_code(admin_client, monkeypatch):
    """soul_io 寫入回 False → SOUL_WRITE_FAILED（errors.json SSOT）＋回滾不留孤兒。"""
    monkeypatch.setattr("src.utils.soul_io.write_soul_file", lambda *_a, **_k: False)
    r = admin_client.post("/v1/souls", json=_create_payload("CrudSoulE"))
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "SOUL_WRITE_FAILED"
    assert not (_app_data() / "soul" / "CrudSoulE").exists()


# ── 路徑逃逸：agent_id／soul_id 含 ../ 必拒（先於任何建立動作）────────────────


def test_create_soul_rejects_path_escape_agent_id(admin_client):
    """agent_id="../escapee" 會把目錄建到 soul/ 之外（且繞過只認 soul/legacy 的
    Soul Protection）——必須 4xx/5xx 拒絕且 soul/ 外不得出現任何東西。現況紅：200 逃逸。"""
    r = admin_client.post("/v1/souls", json=_create_payload("../escapee"))
    assert r.status_code >= 400
    assert not (_app_data() / "escapee").exists(), "逃逸的 agent_id 不得在 soul/ 外建立任何東西"


@pytest.mark.asyncio
async def test_update_soul_rejects_path_escape_soul_id():
    """update 面同款監獄：soul_id=".." 解析到 soul/ 之外 → 必拒。現況紅：寫到 app_data 根。"""
    with pytest.raises(ToukaException):
        await update_soul("..", SoulUpdateRequest(persona="x"), ADMIN)


# ── 自動歸屬（ADR-0158）：建立者即擁有者、建立/刪除/回滾三面對稱 ────────────────


def _real_admin(email: str, pw: str) -> tuple[AuthService, AgentIdentity]:
    """建立真實 admin 使用者並回傳 (auth, identity)——歸屬斷言需要 users.json 實體。"""
    auth = AuthService()
    user = auth.create_user(email, pw, role="admin")
    return auth, AgentIdentity.for_role(user.id, "admin", is_human=True)


def test_create_soul_attributes_creator_owned_agents():
    """POST /v1/souls 成功 → 建立者 user.owned_agents 必含新靈魂。

    現況紅：create_soul 寫四個 SSOT 面但漏 owned_agents（delete_soul 反而會清＝
    建立/刪除不對稱）；孤兒靈魂的心跳喚醒被 HEARTBEAT_ORPHAN_AGENT 直接拒絕、
    前景代打/SafeAuto/審批解算等 owned_agents 授權面全部過不了。"""
    auth, identity = _real_admin("crud-owner@test.com", "CrudOwnerPass123!")
    app.dependency_overrides[get_verified_identity] = lambda: identity
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post("/v1/souls", json=_create_payload("CrudSoulF"))
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200, r.text
    assert "CrudSoulF" in auth.get_user_by_id(identity.agent_id).owned_agents


def test_delete_soul_revokes_attribution_symmetry():
    """建立→歸屬、刪除→撤銷（round-trip 對稱錨；刪除側本就會清、建立側自此必寫）。"""
    pw = "CrudSymmetryPass123!"
    auth, identity = _real_admin("crud-symmetry@test.com", pw)
    app.dependency_overrides[get_verified_identity] = lambda: identity
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r1 = c.post("/v1/souls", json=_create_payload("CrudSoulG"))
            assert r1.status_code == 200, r1.text
            assert "CrudSoulG" in auth.get_user_by_id(identity.agent_id).owned_agents
            r2 = c.request("DELETE", "/v1/souls/CrudSoulG", json={"admin_password": pw})
    finally:
        app.dependency_overrides.clear()
    assert r2.status_code == 200, r2.text
    assert "CrudSoulG" not in auth.get_user_by_id(identity.agent_id).owned_agents


def test_create_failure_rollback_revokes_attribution(monkeypatch):
    """D6 回滾涵蓋歸屬：歸屬之後的步驟失敗 → owned_agents 不得殘留該 id。"""
    auth, identity = _real_admin("crud-rollback@test.com", "CrudRollbackPass123!")
    app.dependency_overrides[get_verified_identity] = lambda: identity
    monkeypatch.setattr(souls_mod, "set_active_brain", _boom)  # 歸屬後的下一步
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post("/v1/souls", json=_create_payload("CrudSoulI"))
    finally:
        app.dependency_overrides.clear()
    assert r.status_code >= 400
    assert "CrudSoulI" not in auth.get_user_by_id(identity.agent_id).owned_agents


# ── 防退化錨：Soul Protection 原語一行未弱化（修復前後皆須綠）─────────────────


class TestProtectionPrimitiveUnweakened:
    """D1 的安全立場：修復走 soul_io 受認可通道，resource_manager 的保護原語照擋
    一切其他呼叫者（agent/工具面）。此錨若紅＝有人弱化了原語＝立即回退。"""

    def test_direct_write_to_soul_path_still_denied(self):
        with pytest.raises(PermissionError):
            resource_manager.write_file_text(_app_data() / "soul" / "X" / "soul.md", "x")

    def test_direct_write_json_to_soul_path_still_denied(self):
        with pytest.raises(PermissionError):
            resource_manager.write_json(_app_data() / "soul" / "X" / "config.json", {})

    def test_direct_remove_of_soul_dir_still_denied(self):
        with pytest.raises(PermissionError):
            resource_manager.remove_dir(_app_data() / "soul" / "X")

    def test_direct_write_to_legacy_path_still_denied(self):
        with pytest.raises(PermissionError):
            resource_manager.write_file_text(_app_data() / "legacy" / "X" / "a.md", "x")
