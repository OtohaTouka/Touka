"""ADR-0112 — 設定快照（Config Snapshot）匯入／匯出（TDD）。

涵蓋：
  - build_snapshot：遮罩 MCP env／連線 apiKey、剔除 agent_mappings／last_seen／last_run_epoch、
    sha256 完整性指紋。
  - validate_snapshot：拒絕非快照／未來版本／竄改／過大；接受合法。
  - apply_snapshot：dict 深合併、list 依 id upsert（only_existing 略過未知＋警告）、replace 模式、
    遮罩機密跳過不覆寫、dry_run 不寫入、限定 sections。
  - 端點安全閘：admin gate、套用時密碼重輸、dry_run 免密碼。
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import system as sysrouter
from src.routers.system import SettingsExportRequest, SettingsImportRequest
from src.services import settings_snapshot_service as svc
from src.services import system_backup
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.providers import registry as provider_registry
from src.utils import config_utils
from src.utils.secrets import MASK


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """隔離的設定目錄；停用真實服務熱載入。"""
    monkeypatch.setattr(config_utils, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(svc, "_RELOADERS", {})
    return tmp_path


def _seed(**files: dict) -> None:
    for name, data in files.items():
        config_utils.save_config_json(name + ".json", data)


def _reseal(snap: dict) -> None:
    """測試修改 sections 後重算完整性指紋，使 validate 通過。"""
    snap["manifest"]["sha256"] = svc._canonical_sha256(snap["sections"])


# ── build_snapshot：遮罩與脫敏 ────────────────────────────────────────────────

def test_build_masks_secrets_and_strips_runtime(cfg):
    _seed(
        server_config={"mcpServers": {"fetch": {
            "command": "x", "args": [], "env": {"API_KEY": "sk-real"}, "enabled": True}}},
        connections={"connections": [{"id": "c1", "apiKey": "real-key", "provider": "x"}]},
        permissions={"system_config": {}, "roles": {}, "agent_mappings": {"a1": "Owner"}},
        models={"models": [
            {"id": "m1", "enabled": True, "visibility": "visible", "temperature": 0.5,
             "last_seen": 123.4, "status": "online"}]},
        backup_schedule={"enabled": True, "frequency": "daily", "last_run_epoch": 999.0},
    )
    snap = svc.build_snapshot(exported_by_user_id="admin1")
    sec = snap["sections"]

    assert sec["mcp_servers"]["data"]["mcpServers"]["fetch"]["env"]["API_KEY"] == MASK
    assert sec["connections"]["data"]["connections"][0]["apiKey"] is None
    assert "agent_mappings" not in sec["permissions"]["data"]
    model = sec["models"]["data"]["models"][0]
    assert "last_seen" not in model and "status" not in model
    assert model["temperature"] == 0.5
    assert "last_run_epoch" not in sec["backup_schedule"]["data"]

    assert snap["export_kind"] == "settings_snapshot"
    assert snap["manifest"]["sha256"] == svc._canonical_sha256(snap["sections"])


# ── validate_snapshot ─────────────────────────────────────────────────────────

def test_validate_rejects_non_snapshot(cfg):
    with pytest.raises(ToukaException):
        svc.validate_snapshot({"export_kind": "backup", "sections": {}})


def test_validate_rejects_future_version(cfg):
    _seed(autonomy={"quota": {"daily_max": 1}})
    snap = svc.build_snapshot()
    snap["schema_version"] = svc.SCHEMA_VERSION + 1
    with pytest.raises(ToukaException):
        svc.validate_snapshot(snap)


def test_validate_rejects_tampered_sha(cfg):
    _seed(autonomy={"quota": {"daily_max": 1}})
    snap = svc.build_snapshot()
    snap["sections"]["autonomy"]["data"]["quota"]["daily_max"] = 999  # 竄改但 sha 未更新
    with pytest.raises(ToukaException):
        svc.validate_snapshot(snap)


def test_validate_accepts_valid(cfg):
    _seed(autonomy={"quota": {"daily_max": 1}})
    snap = svc.build_snapshot()
    validated = svc.validate_snapshot(snap)
    assert "autonomy" in validated


def test_validate_rejects_non_list_wrapper(cfg):
    """複審：list 策略區的包裹鍵非 list → 拒絕（防 replace 寫損毀／merge 逐字元無聲略過）。"""
    _seed(models={"models": [{"id": "m1", "enabled": True}]})
    snap = svc.build_snapshot()
    snap["sections"]["models"]["data"]["models"] = "not-a-list"
    _reseal(snap)
    with pytest.raises(ToukaException):
        svc.validate_snapshot(snap)


def test_export_strips_url_credentials(cfg):
    """複審：連線 url 內嵌帳密於匯出時去除（apiKey 亦 null）。"""
    _seed(connections={"connections": [
        {"id": "c1", "url": "https://user:pass@api.example.com/v1", "apiKey": "k"}]})
    snap = svc.build_snapshot()
    conn = snap["sections"]["connections"]["data"]["connections"][0]
    assert conn["url"] == "https://api.example.com/v1"
    assert conn["apiKey"] is None


# ── apply_snapshot：合併語意 ──────────────────────────────────────────────────

def test_apply_merge_deep_merges_dict(cfg):
    _seed(autonomy={"global_config": {"max_pulses_per_session": 5}, "quota": {"daily_max": 50}})
    snap = svc.build_snapshot()
    snap["sections"]["autonomy"]["data"]["quota"]["daily_max"] = 99  # 改一鍵
    _reseal(snap)
    svc.apply_snapshot(snap, mode="merge")
    after = config_utils.read_config_json("autonomy.json")
    assert after["quota"]["daily_max"] == 99           # 已更新
    assert after["global_config"]["max_pulses_per_session"] == 5  # 未提及者保留


def test_apply_models_only_existing_warns_unknown(cfg):
    _seed(models={"models": [{"id": "m1", "enabled": True, "visibility": "visible"}]})
    snap = svc.build_snapshot()
    data = snap["sections"]["models"]["data"]
    data["models"][0]["enabled"] = False                  # 更新既有
    data["models"].append({"id": "ghost", "enabled": True})  # 未知 id
    _reseal(snap)
    result = svc.apply_snapshot(snap, mode="merge")
    after = config_utils.read_config_json("models.json")
    assert after["models"][0]["enabled"] is False
    assert all(m["id"] != "ghost" for m in after["models"])  # 未新增
    assert any("ghost" in w for w in result["warnings"])


def test_apply_backfills_masked_secret_merge(cfg):
    """既有真值的 MCP env 被遮罩匯入 → 回填保留；非「待補填」故不列入 skipped_secrets。"""
    _seed(server_config={"mcpServers": {"fetch": {
        "command": "x", "args": [], "env": {"API_KEY": "sk-real"}, "enabled": True}}})
    snap = svc.build_snapshot()  # env 已遮罩為 MASK
    result = svc.apply_snapshot(snap, mode="merge")
    after = config_utils.read_config_json("server_config.json")
    assert after["mcpServers"]["fetch"]["env"]["API_KEY"] == "sk-real"  # 真值未被遮罩覆寫
    assert result["skipped_secrets"] == []  # 已回填既有值，無待補填


def test_apply_replace_preserves_real_secret(cfg):
    """複審 High：replace 模式也須回填既有真值，不得以遮罩假值清掉真金鑰。"""
    _seed(server_config={"mcpServers": {"fetch": {
        "command": "x", "args": [], "env": {"API_KEY": "sk-real"}, "enabled": True}}})
    snap = svc.build_snapshot()
    svc.apply_snapshot(snap, mode="replace")
    after = config_utils.read_config_json("server_config.json")
    assert after["mcpServers"]["fetch"]["env"]["API_KEY"] == "sk-real"


def test_apply_env_null_not_clobbered(cfg):
    """複審 High：incoming env=null（惡意/損毀）不得覆寫目標既有 env。"""
    _seed(server_config={"mcpServers": {"fetch": {
        "command": "x", "args": [], "env": {"API_KEY": "sk-real"}, "enabled": True}}})
    snap = svc.build_snapshot()
    snap["sections"]["mcp_servers"]["data"]["mcpServers"]["fetch"]["env"] = None
    _reseal(snap)
    svc.apply_snapshot(snap, mode="merge")
    after = config_utils.read_config_json("server_config.json")
    assert after["mcpServers"]["fetch"]["env"]["API_KEY"] == "sk-real"


def test_apply_new_server_masked_secret_reported(cfg):
    """目標無既有值可回填的遮罩機密 → 丟棄並列入 skipped_secrets（提示 admin 補填）。"""
    _seed(server_config={"mcpServers": {"newsrv": {
        "command": "x", "args": [], "env": {"API_KEY": "sk-x"}, "enabled": True}}})
    snap = svc.build_snapshot()
    _seed(server_config={"mcpServers": {}})  # 目標無此伺服器
    result = svc.apply_snapshot(snap, mode="merge")
    after = config_utils.read_config_json("server_config.json")
    assert "API_KEY" not in after["mcpServers"]["newsrv"].get("env", {})
    assert any("newsrv:API_KEY" in s for s in result["skipped_secrets"])


def test_apply_replace_mode_overwrites_whole_section(cfg):
    _seed(models={"models": [
        {"id": "m1", "enabled": True}, {"id": "m2", "enabled": True}]})
    snap = svc.build_snapshot()
    snap["sections"]["models"]["data"] = {"models": [{"id": "m1", "enabled": False}]}
    _reseal(snap)
    svc.apply_snapshot(snap, mode="replace")
    after = config_utils.read_config_json("models.json")
    assert after == {"models": [{"id": "m1", "enabled": False}]}  # m2 已移除


def test_apply_dry_run_writes_nothing(cfg):
    _seed(autonomy={"quota": {"daily_max": 50}})
    snap = svc.build_snapshot()
    snap["sections"]["autonomy"]["data"]["quota"]["daily_max"] = 1
    _reseal(snap)
    result = svc.apply_snapshot(snap, mode="merge", dry_run=True)
    assert result["dry_run"] is True
    assert "autonomy" in result["applied"]
    assert config_utils.read_config_json("autonomy.json")["quota"]["daily_max"] == 50  # 未變


def test_apply_subset_only_listed_sections(cfg):
    _seed(autonomy={"quota": {"daily_max": 50}}, sliding_window={"enabled": True})
    snap = svc.build_snapshot()
    snap["sections"]["autonomy"]["data"]["quota"]["daily_max"] = 1
    snap["sections"]["sliding_window"]["data"]["enabled"] = False
    _reseal(snap)
    svc.apply_snapshot(snap, mode="merge", sections=["autonomy"])
    assert config_utils.read_config_json("autonomy.json")["quota"]["daily_max"] == 1
    assert config_utils.read_config_json("sliding_window.json")["enabled"] is True  # 未動


# ── 端點安全閘 ────────────────────────────────────────────────────────────────

def _admin() -> SimpleNamespace:
    return SimpleNamespace(role="admin", hashed_password="h", id="u1", agent_id="admin1")


async def test_export_returns_snapshot(cfg, monkeypatch):
    monkeypatch.setattr(sysrouter, "verify_origin", lambda r: None)
    _seed(autonomy={"quota": {"daily_max": 1}})
    out = await sysrouter.export_settings(SettingsExportRequest(encrypt=False), None, _admin())
    assert out["export_kind"] == "settings_snapshot"


async def test_import_requires_admin(cfg, monkeypatch):
    monkeypatch.setattr(sysrouter, "verify_origin", lambda r: None)
    _seed(autonomy={"quota": {"daily_max": 1}})
    snap = svc.build_snapshot()
    non_admin = SimpleNamespace(role="normal", hashed_password="h", id="u2")
    req = SettingsImportRequest(snapshot=snap, dry_run=True)
    with pytest.raises(ToukaException):
        await sysrouter.import_settings(req, None, non_admin, Mock())


async def test_import_apply_requires_password(cfg, monkeypatch):
    monkeypatch.setattr(sysrouter, "verify_origin", lambda r: None)
    _seed(autonomy={"quota": {"daily_max": 1}})
    snap = svc.build_snapshot()
    auth = Mock()
    auth.verify_password.return_value = False
    req = SettingsImportRequest(snapshot=snap, dry_run=False, current_password="wrong")
    with pytest.raises(ToukaException) as exc:
        await sysrouter.import_settings(req, None, _admin(), auth)
    assert (exc.value.code, exc.value.status_code) == (ErrorCode.AUTH_REAUTH_FAILED, 403)


async def test_import_dry_run_skips_password_and_backup(cfg, monkeypatch):
    monkeypatch.setattr(sysrouter, "verify_origin", lambda r: None)
    called = {"backup": False}
    monkeypatch.setattr(sysrouter, "create_backup", lambda: called.__setitem__("backup", True))
    _seed(autonomy={"quota": {"daily_max": 1}})
    snap = svc.build_snapshot()
    auth = Mock()
    req = SettingsImportRequest(snapshot=snap, dry_run=True)
    result = await sysrouter.import_settings(req, None, _admin(), auth)
    assert result["dry_run"] is True
    auth.verify_password.assert_not_called()
    assert called["backup"] is False
    assert result["pre_import_backup"] is None


# ── E64（ADR-0112）：匯入成功路徑——備份先於套用、全程持鎖、真的落盤 ─────────────


async def test_import_apply_success_backs_up_before_apply_under_locks(cfg, monkeypatch):
    """正確密碼成功路徑：create_backup 先於 apply、雙鎖序列化、回傳還原點、落盤生效。

    變異「去備份」（pre_import_backup = {}）或「去鎖」（拆掉巢狀 async with）→ 紅。
    """
    monkeypatch.setattr(sysrouter, "verify_origin", lambda r: None)
    order: list[str] = []
    lock_seen: dict[str, bool] = {}

    def _fake_backup():
        order.append("backup")
        lock_seen["backup_under_import_lock"] = sysrouter._settings_import_lock.locked()
        lock_seen["backup_under_exec_lock"] = system_backup.backup_execution_lock.locked()
        return {"success": True, "message": "restore-point"}

    real_apply = svc.apply_snapshot

    def _spy_apply(*args, **kwargs):
        order.append("apply")
        lock_seen["apply_under_import_lock"] = sysrouter._settings_import_lock.locked()
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(sysrouter, "create_backup", _fake_backup)
    monkeypatch.setattr(sysrouter, "apply_snapshot", _spy_apply)

    _seed(autonomy={"quota": {"daily_max": 50}})
    snap = svc.build_snapshot()
    snap["sections"]["autonomy"]["data"]["quota"]["daily_max"] = 99
    _reseal(snap)
    auth = Mock()
    auth.verify_password.return_value = True
    req = SettingsImportRequest(snapshot=snap, dry_run=False, current_password="correct")
    result = await sysrouter.import_settings(req, None, _admin(), auth)

    auth.verify_password.assert_called_once_with("correct", "h")  # 密碼重輸真的走過
    assert order == ["backup", "apply"]  # 還原點先於破壞性套用
    assert lock_seen == {
        "backup_under_import_lock": True,  # 備份在匯入鎖內（TOCTOU 序列化）
        "backup_under_exec_lock": True,    # 且持備份執行鎖（不與排程備份重疊）
        "apply_under_import_lock": True,   # 套用仍在匯入鎖內
    }
    assert result["pre_import_backup"] == {"success": True, "message": "restore-point"}
    assert result["dry_run"] is False
    assert config_utils.read_config_json("autonomy.json")["quota"]["daily_max"] == 99  # 落盤


# ── E63（ADR-0112）：套用後熱載入 _RELOADERS 接線（既有 cfg fixture 會清空表） ──────


@pytest.fixture
def cfg_live_reload(tmp_path, monkeypatch):
    """隔離設定目錄但**保留**熱載入表——E63 專用（cfg 會把 _RELOADERS 清空）。"""
    monkeypatch.setattr(config_utils, "CONFIG_DIR", tmp_path)
    return tmp_path


def test_reloaders_table_binds_every_declared_reload_key():
    """SECTIONS 宣告的每個 reload_key 都有對應熱載入器（防清表／打錯字＝匯入後 stale）。"""
    declared = {s.reload_key for s in svc.SECTIONS if s.reload_key}
    assert declared == {"autonomy", "permissions", "models", "sliding_window"}
    assert set(svc._RELOADERS) == declared
    assert all(callable(fn) for fn in svc._RELOADERS.values())


def test_apply_runs_hot_reloader_per_applied_section(cfg_live_reload, monkeypatch):
    """套用四個熱載入區 → 各自的 reloader 恰被呼叫一次（admin 匯入後即時生效不需重啟）。

    變異 _run_reloaders 改 pass、或 SectionSpec reload_key 打錯字 → 對應鍵未被呼叫 → 紅。
    """
    calls: list[str] = []
    for key in list(svc._RELOADERS):
        monkeypatch.setitem(svc._RELOADERS, key, lambda k=key: calls.append(k))
    _seed(
        autonomy={"quota": {"daily_max": 50}},
        permissions={"system_config": {}, "roles": {}},
        models={"models": [{"id": "m1", "enabled": True}]},
        sliding_window={"enabled": True},
    )
    snap = svc.build_snapshot()
    svc.apply_snapshot(snap, mode="merge")
    assert sorted(calls) == ["autonomy", "models", "permissions", "sliding_window"]


def test_apply_dry_run_never_hot_reloads(cfg_live_reload, monkeypatch):
    """dry_run 只預覽：零熱載入呼叫（與零寫入同權重的唯讀保證）。"""
    calls: list[str] = []
    for key in list(svc._RELOADERS):
        monkeypatch.setitem(svc._RELOADERS, key, lambda k=key: calls.append(k))
    _seed(autonomy={"quota": {"daily_max": 50}})
    snap = svc.build_snapshot()
    svc.apply_snapshot(snap, mode="merge", dry_run=True)
    assert calls == []


def test_connections_after_save_runs_only_after_a_real_write(cfg, monkeypatch):
    """U2e3：連線區寫進檔案之後才呼叫 after_save（撤舊路由、封鎖埠跟上）；dry_run 不呼叫。"""
    seen: list[object] = []
    spec = dataclasses.replace(
        svc._BY_KEY["connections"],
        after_save=lambda: seen.append(config_utils.read_config_json("connections.json")),
    )
    monkeypatch.setitem(svc._BY_KEY, "connections", spec)
    _seed(connections={"connections": []})
    snap = svc.build_snapshot()
    gateway = {"id": "gw", "url": "http://localhost:4100/v1"}
    snap["sections"]["connections"]["data"] = {"connections": [gateway]}
    _reseal(snap)
    svc.apply_snapshot(snap, mode="merge", dry_run=True)
    assert seen == []
    svc.apply_snapshot(snap, mode="merge")
    assert seen == [{"connections": [gateway]}]  # 呼叫時新內容已落盤


def test_import_closes_custom_routes_until_models_refresh(cfg, monkeypatch):
    """U2e4：匯入含 connections 區的快照（不是 dry_run）後，自訂模型的路由先關閉，
    要重新掃描（``/models/refresh``）或重啟才恢復；和連線頁存檔同一套。
    """
    monkeypatch.setattr(provider_registry, "get_key", lambda _spec: "")  # 官方供應商不探測
    discovered = [{"id": "gw-model", "object": "model", "owned_by": "custom"}]
    monkeypatch.setattr(
        "src.services.models.manager.fetch_openai_compatible_models",
        AsyncMock(return_value=discovered),
    )
    gateway = {"id": "gw", "url": "http://localhost:4100/v1", "provider": "custom", "enabled": True}
    _seed(connections={"connections": [gateway]})
    admin = AgentIdentity.for_role(agent_id="Admin", role="admin", is_human=True)
    app.dependency_overrides[get_verified_identity] = lambda: admin
    client = TestClient(app)
    assert client.post("/v1/models/refresh").status_code == 200
    assert model_manager.get_custom_route("gw-model") is not None
    svc.apply_snapshot(svc.build_snapshot(), mode="merge")
    assert model_manager.get_custom_route("gw-model") is None  # 匯入後先關
    assert client.post("/v1/models/refresh").status_code == 200
    assert model_manager.get_custom_route("gw-model") is not None  # 重新掃描後恢復
