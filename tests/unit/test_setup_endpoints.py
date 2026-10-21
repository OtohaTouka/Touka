"""TDD: Setup status and apply endpoints (Phase 2)."""

import secrets

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services import embedding_service as es
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.providers import registry
from src.services.setup_service import SetupService
from src.services.user_service import UserService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_admin_identity() -> AgentIdentity:
    return AgentIdentity.for_role(
        agent_id="DebugAgent",
        role="admin",
        capabilities=[],
        enabled_tools=[],
    )


def _make_guest_identity() -> AgentIdentity:
    # ADR-0245：Guest 退役，低權限樣本改用 Base。
    return AgentIdentity.for_role(
        agent_id="LowAgent",
        role="Base",
        capabilities=[],
        enabled_tools=[],
    )


# ---------------------------------------------------------------------------
# 1. GET /v1/admin/setup/status — auth enforcement
# ---------------------------------------------------------------------------


def test_setup_status_requires_admin():
    """Non-admin request must be rejected with 403."""
    app.dependency_overrides[get_verified_identity] = lambda: _make_guest_identity()
    try:
        with TestClient(app) as client:
            response = client.get("/v1/admin/setup/status")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def test_setup_status_accessible_by_admin():
    """Admin request must return 200 with status fields."""
    app.dependency_overrides[get_verified_identity] = lambda: _make_admin_identity()
    try:
        with TestClient(app) as client:
            response = client.get("/v1/admin/setup/status")
        assert response.status_code == 200
        data = response.json()
        assert "dispatch_token_is_default" in data
        assert "auth_secret_persisted" in data
        assert "api_keys_configured" in data
        assert "is_admin_temp_password" in data
        assert "data_path_is_default" in data
        assert "workspace_root_is_default" in data
        assert "embedding_model" in data
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


# ---------------------------------------------------------------------------
# 2. SetupService unit tests — dispatch_token detection
# ---------------------------------------------------------------------------


def test_setup_service_detects_default_dispatch_token(monkeypatch):
    """dispatch_token_is_default=True when token is the known-weak default."""
    monkeypatch.setenv("DISPATCH_TOKEN", "touka_dispatch_secret_2026")
    svc = SetupService()
    status = svc.get_status()
    assert status.dispatch_token_is_default is True


def test_setup_service_detects_empty_dispatch_token(monkeypatch):
    """dispatch_token_is_default=True when token is empty."""
    monkeypatch.setenv("DISPATCH_TOKEN", "")
    svc = SetupService()
    status = svc.get_status()
    assert status.dispatch_token_is_default is True


def test_setup_service_strong_dispatch_token_is_ok(monkeypatch):
    """dispatch_token_is_default=False when a strong random token is set."""
    monkeypatch.setenv("DISPATCH_TOKEN", secrets.token_urlsafe(32))
    svc = SetupService()
    status = svc.get_status()
    assert status.dispatch_token_is_default is False


# ---------------------------------------------------------------------------
# 3. SetupService unit tests — TOUKA_AUTH_SECRET persistence detection
# ---------------------------------------------------------------------------


def test_setup_service_detects_unpersisted_auth_secret(monkeypatch):
    """auth_secret_persisted=False when TOUKA_AUTH_SECRET is empty in env."""
    monkeypatch.setenv("TOUKA_AUTH_SECRET", "")
    svc = SetupService()
    status = svc.get_status()
    assert status.auth_secret_persisted is False


def test_setup_service_detects_persisted_auth_secret(monkeypatch):
    """auth_secret_persisted=True when TOUKA_AUTH_SECRET is set."""
    monkeypatch.setenv("TOUKA_AUTH_SECRET", secrets.token_hex(32))
    svc = SetupService()
    status = svc.get_status()
    assert status.auth_secret_persisted is True


# ---------------------------------------------------------------------------
# 4. SetupService unit tests — API key detection
# ---------------------------------------------------------------------------


def test_setup_service_detects_api_keys(monkeypatch):
    """api_keys_configured lists providers that have non-empty keys."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROK_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    svc = SetupService()
    status = svc.get_status()
    assert "anthropic" in status.api_keys_configured
    assert "tavily" in status.api_keys_configured
    assert "gemini" not in status.api_keys_configured


def test_setup_service_detects_deepseek_key(monkeypatch):
    """D#15：設定 DEEPSEEK_API_KEY 後狀態須列出 deepseek（先前硬編碼對照表漏掉）。"""
    for var in (
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GROK_API_KEY",
        "OPENAI_API_KEY",
        "TAVILY_API_KEY",
    ):
        monkeypatch.setenv(var, "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    status = SetupService().get_status()
    assert "deepseek" in status.api_keys_configured


def test_setup_service_api_keys_parity_with_registry(monkeypatch):
    """D#15 parity：所有 registry 可發現 provider 都能在狀態中呈現（防對照表再漂移）。"""
    for spec in registry.discoverable_specs():
        monkeypatch.setenv(spec.key_attrs[0], "x-set")
    ids = set(SetupService().get_status().api_keys_configured)
    missing = [spec.id for spec in registry.discoverable_specs() if spec.id not in ids]
    assert not missing, f"provider(s) missing from api_keys_configured: {missing}"


# ---------------------------------------------------------------------------
# 5. SetupService unit tests — is_admin_temp_password
# ---------------------------------------------------------------------------


def test_setup_service_detects_temp_password(tmp_path, monkeypatch):
    """is_admin_temp_password=True when admin has is_temp_password=True."""
    users_file = tmp_path / "users.json"
    auth = AuthService(users_file_path=str(users_file))
    auth.ensure_admin_user([])
    monkeypatch.setenv("TOUKA_CONFIG_DIR", str(tmp_path))

    svc = SetupService(users_file_path=str(users_file))
    status = svc.get_status()
    assert status.is_admin_temp_password is True


def test_setup_service_is_admin_temp_password_returns_true_on_corrupted_users_json(tmp_path):
    """get_status() must not raise when users.json is corrupted — B3 fix."""
    users_file = tmp_path / "users.json"
    users_file.write_text("{invalid json content}")  # intentionally corrupt

    svc = SetupService(users_file_path=str(users_file))
    # Must not raise — should treat corruption as "admin setup required"
    status = svc.get_status()
    assert status.is_admin_temp_password is True


def test_setup_service_no_temp_password_after_change(tmp_path, monkeypatch):
    """is_admin_temp_password=False after admin password is changed."""
    users_file = tmp_path / "users.json"
    auth = AuthService(users_file_path=str(users_file))
    auth.ensure_admin_user([])
    admin = next(u for u in auth.get_all_users() if u.role == "admin")
    UserService(auth_service=auth).admin_update_user(admin.id, {"password": "new_secure_pw!"})

    svc = SetupService(users_file_path=str(users_file))
    status = svc.get_status()
    assert status.is_admin_temp_password is False


# ---------------------------------------------------------------------------
# 6. POST /v1/admin/setup/apply — auth enforcement
# ---------------------------------------------------------------------------


def test_setup_apply_requires_admin():
    """Non-admin request must be rejected with 403."""
    app.dependency_overrides[get_verified_identity] = lambda: _make_guest_identity()
    try:
        with TestClient(app) as client:
            response = client.post("/v1/admin/setup/apply", json={})
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


# ---------------------------------------------------------------------------
# 7. SetupService.apply — writes values to .env
# ---------------------------------------------------------------------------


def test_setup_service_apply_writes_dispatch_token(tmp_path):
    """apply() writes DISPATCH_TOKEN to .env file."""
    env_file = tmp_path / ".env"
    env_file.write_text("DISPATCH_TOKEN=touka_dispatch_secret_2026\n")
    svc = SetupService(env_file_path=str(env_file))
    new_token = secrets.token_urlsafe(32)
    svc.apply(dispatch_token=new_token)
    content = env_file.read_text()
    assert new_token in content


def test_setup_service_apply_rejects_empty_dispatch_token(tmp_path):
    """apply() raises ValueError if dispatch_token is empty or whitespace."""
    env_file = tmp_path / ".env"
    env_file.write_text("")
    svc = SetupService(env_file_path=str(env_file))
    with pytest.raises(ValueError, match="dispatch_token"):
        svc.apply(dispatch_token="  ")


def test_setup_service_apply_writes_auth_secret(tmp_path):
    """apply() writes TOUKA_AUTH_SECRET to .env file."""
    env_file = tmp_path / ".env"
    env_file.write_text("")
    svc = SetupService(env_file_path=str(env_file))
    new_secret = secrets.token_hex(32)
    svc.apply(auth_secret=new_secret)
    content = env_file.read_text()
    assert new_secret in content


def test_setup_service_apply_no_op_without_values(tmp_path):
    """apply() with no arguments writes nothing and does not raise."""
    env_file = tmp_path / ".env"
    env_file.write_text("DISPATCH_TOKEN=existing\n")
    original = env_file.read_text()
    svc = SetupService(env_file_path=str(env_file))
    svc.apply()  # no args
    assert env_file.read_text() == original


# ---------------------------------------------------------------------------
# 8. POST /v1/admin/setup/apply — B1 fix: blank fields must return 422, not 500
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["dispatch_token", "auth_secret", "data_path", "embedding_model"])
def test_setup_apply_empty_string_returns_422(field):
    """Empty string for any apply field must return 422 (not 500) — B1 fix."""
    app.dependency_overrides[get_verified_identity] = lambda: _make_admin_identity()
    try:
        with TestClient(app) as client:
            response = client.post("/v1/admin/setup/apply", json={field: ""})
        assert response.status_code == 422, (
            f"Expected 422 for empty {field!r}, got {response.status_code}. "
            "Router must convert ValueError to 422 via Pydantic validation."
        )
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


@pytest.mark.parametrize("field", ["dispatch_token", "auth_secret", "data_path", "embedding_model"])
def test_setup_apply_whitespace_only_returns_422(field):
    """Whitespace-only string for any apply field must return 422 — B1 fix."""
    app.dependency_overrides[get_verified_identity] = lambda: _make_admin_identity()
    try:
        with TestClient(app) as client:
            response = client.post("/v1/admin/setup/apply", json={field: "   "})
        assert response.status_code == 422, (
            f"Expected 422 for whitespace {field!r}, got {response.status_code}."
        )
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def test_setup_apply_null_fields_returns_200():
    """All-None payload (empty JSON object) must still return 200 — regression guard."""
    app.dependency_overrides[get_verified_identity] = lambda: _make_admin_identity()
    try:
        with TestClient(app) as client:
            response = client.post("/v1/admin/setup/apply", json={})
        assert response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


# ---------------------------------------------------------------------------
# 9. SetupService.apply — B2 fix: no partial write on validation failure
# ---------------------------------------------------------------------------


def test_setup_apply_no_partial_write_on_second_field_failure(tmp_path):
    """B2: First field must NOT be written if a later field fails validation."""
    env_file = tmp_path / ".env"
    env_file.write_text("DISPATCH_TOKEN=original_marker\n")
    svc = SetupService(env_file_path=str(env_file))

    with pytest.raises(ValueError):
        svc.apply(dispatch_token=secrets.token_urlsafe(32), auth_secret="  ")

    assert "original_marker" in env_file.read_text()


# ---------------------------------------------------------------------------
# 10. SetupService.apply — B4 fix: data_path directory validation
# ---------------------------------------------------------------------------


@pytest.fixture
def native_setup_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host-directory cases must not inherit the runner's Docker detection."""
    monkeypatch.setattr("src.services.setup_service._is_running_in_docker", lambda: False)


@pytest.mark.usefixtures("native_setup_mode")
def test_setup_apply_rejects_nonexistent_data_path(tmp_path):
    """B4: apply() raises ValueError when data_path does not exist."""
    env_file = tmp_path / ".env"
    env_file.write_text("")
    svc = SetupService(env_file_path=str(env_file))
    with pytest.raises(ValueError, match="data_path"):
        svc.apply(data_path=str(tmp_path / "does_not_exist"))


@pytest.mark.usefixtures("native_setup_mode")
def test_setup_apply_rejects_file_as_data_path(tmp_path):
    """B4: apply() raises ValueError when data_path points to a file, not a directory."""
    env_file = tmp_path / ".env"
    env_file.write_text("")
    a_file = tmp_path / "not_a_dir.txt"
    a_file.write_text("content")
    svc = SetupService(env_file_path=str(env_file))
    with pytest.raises(ValueError, match="data_path"):
        svc.apply(data_path=str(a_file))


@pytest.mark.usefixtures("native_setup_mode")
def test_setup_apply_accepts_existing_directory_as_data_path(tmp_path):
    """B4: apply() writes data_path when the directory exists — regression guard."""
    env_file = tmp_path / ".env"
    env_file.write_text("")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    svc = SetupService(env_file_path=str(env_file))
    svc.apply(data_path=str(data_dir))
    assert str(data_dir) in env_file.read_text()


# ---------------------------------------------------------------------------
# 11. WORKSPACE_ROOT configurability + Docker-aware validation + clean 422
#     (bug② fix：宿主↔容器路徑；精靈可設 SANCTUARY_DATA_PATH 與 WORKSPACE_ROOT)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("native_setup_mode")
def test_setup_service_apply_writes_workspace_root(tmp_path):
    """apply() writes WORKSPACE_ROOT to .env when the directory exists (native mode)."""
    env_file = tmp_path / ".env"
    env_file.write_text("")
    ws = tmp_path / "workspace"
    ws.mkdir()
    svc = SetupService(env_file_path=str(env_file))
    svc.apply(workspace_root=str(ws))
    content = env_file.read_text()
    assert "WORKSPACE_ROOT" in content
    assert str(ws) in content


@pytest.mark.usefixtures("native_setup_mode")
def test_setup_service_apply_rejects_nonexistent_workspace_root_native(tmp_path):
    """Native mode: apply() raises ValueError when workspace_root does not exist."""
    env_file = tmp_path / ".env"
    env_file.write_text("")
    svc = SetupService(env_file_path=str(env_file))
    with pytest.raises(ValueError, match="workspace_root"):
        svc.apply(workspace_root=str(tmp_path / "nope"))


def test_setup_service_apply_docker_skips_dir_existence(tmp_path, monkeypatch):
    """Docker 模式：宿主路徑對容器不可見 → 跳過 is_dir，仍寫入（由 compose 掛載時把關）。"""
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr("src.services.setup_service._is_running_in_docker", lambda: True)
    svc = SetupService(env_file_path=str(env_file))
    # 一個在（容器）檔案系統中不存在的宿主路徑。
    svc.apply(data_path="/host/only/data", workspace_root="/host/only/work")
    content = env_file.read_text()
    assert "SANCTUARY_DATA_PATH" in content
    assert "WORKSPACE_ROOT" in content


@pytest.mark.parametrize("docker", [False, True])
@pytest.mark.parametrize("field", ["data_path", "workspace_root"])
def test_setup_directory_blank_rejected_in_both_modes(tmp_path, monkeypatch, docker, field):
    """Docker's host-path exception never admits blank input or writes partial settings."""
    env_file = tmp_path / ".env"
    env_file.write_text("ORIGINAL=preserved\n")
    monkeypatch.setattr("src.services.setup_service._is_running_in_docker", lambda: docker)
    svc = SetupService(env_file_path=str(env_file))
    with pytest.raises(ValueError, match=field):
        svc.apply(**{field: "  "})
    assert env_file.read_text() == "ORIGINAL=preserved\n"


def test_setup_status_workspace_root_default_true_when_unset(monkeypatch):
    """workspace_root_is_default=True 當 WORKSPACE_ROOT 未設定。"""
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    svc = SetupService()
    assert svc.get_status().workspace_root_is_default is True


def test_setup_status_workspace_root_default_false_when_set(monkeypatch):
    """workspace_root_is_default=False 當 WORKSPACE_ROOT 已設自訂值。"""
    monkeypatch.setenv("WORKSPACE_ROOT", "/some/host/path")
    svc = SetupService()
    assert svc.get_status().workspace_root_is_default is False


@pytest.mark.usefixtures("native_setup_mode")
def test_setup_apply_endpoint_invalid_data_path_returns_422(tmp_path):
    """端點：原生模式下不存在的 data_path → 乾淨 422（非 500 醜英文）。"""
    app.dependency_overrides[get_verified_identity] = lambda: _make_admin_identity()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/admin/setup/apply",
                json={"data_path": str(tmp_path / "does_not_exist_xyz")},
            )
        assert response.status_code == 422, (
            f"Expected 422 for invalid data_path, got {response.status_code}"
        )
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


# ---------------------------------------------------------------------------
# 12. 嵌入模型：下載/狀態/切換端點（bug①）
# ---------------------------------------------------------------------------


def test_embedding_download_requires_admin():
    """下載端點須 admin。"""
    app.dependency_overrides[get_verified_identity] = lambda: _make_guest_identity()
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/admin/setup/embedding/download", json={"model": "all-MiniLM-L6-v2"}
            )
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def test_embedding_download_returns_state(monkeypatch):
    """下載端點回傳目前狀態（begin_download 決定是否背景執行）。"""
    monkeypatch.setattr(es, "begin_download", lambda m: ({"status": "downloading", "pct": 0}, True))
    monkeypatch.setattr(es, "perform_download", lambda m: None)
    app.dependency_overrides[get_verified_identity] = lambda: _make_admin_identity()
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/admin/setup/embedding/download", json={"model": "all-MiniLM-L6-v2"}
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "downloading"
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def test_embedding_download_status_returns_progress(monkeypatch):
    """狀態端點回傳輪詢進度。"""
    monkeypatch.setattr(es, "get_download_state", lambda m: {"status": "done", "pct": 100})
    app.dependency_overrides[get_verified_identity] = lambda: _make_admin_identity()
    try:
        with TestClient(app) as client:
            resp = client.get(
                "/v1/admin/setup/embedding/download-status",
                params={"model": "all-MiniLM-L6-v2"},
            )
        assert resp.status_code == 200
        assert resp.json()["pct"] == 100
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def test_embedding_switch_requires_confirmation_with_memories(monkeypatch):
    """既有記憶 + 換模型 + 未 force → confirmation_required，且不寫入（防呆③）。"""
    monkeypatch.setattr(
        es,
        "evaluate_switch",
        lambda m: {
            "current_model": "all-MiniLM-L6-v2",
            "target_model": m,
            "has_memories": True,
            "requires_confirmation": True,
        },
    )
    applied = {"n": 0}
    monkeypatch.setattr(es, "apply_switch", lambda m: applied.__setitem__("n", applied["n"] + 1))
    app.dependency_overrides[get_verified_identity] = lambda: _make_admin_identity()
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/admin/setup/embedding/switch",
                json={"model": "paraphrase-multilingual-MiniLM-L12-v2"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmation_required"
        assert applied["n"] == 0  # 未 force → 絕不寫入
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def test_embedding_switch_force_applies(monkeypatch):
    """force=True（已確認）→ 寫入切換。"""
    monkeypatch.setattr(
        es,
        "evaluate_switch",
        lambda m: {
            "current_model": "all-MiniLM-L6-v2",
            "target_model": m,
            "has_memories": True,
            "requires_confirmation": True,
        },
    )
    applied = {"n": 0}
    monkeypatch.setattr(es, "apply_switch", lambda m: applied.__setitem__("n", applied["n"] + 1))
    app.dependency_overrides[get_verified_identity] = lambda: _make_admin_identity()
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/admin/setup/embedding/switch",
                json={"model": "paraphrase-multilingual-MiniLM-L12-v2", "force": True},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert applied["n"] == 1
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def test_embedding_switch_no_memories_applies_without_force(monkeypatch):
    """無既有記憶 → 直接切換、免確認。"""
    monkeypatch.setattr(
        es,
        "evaluate_switch",
        lambda m: {
            "current_model": "all-MiniLM-L6-v2",
            "target_model": m,
            "has_memories": False,
            "requires_confirmation": False,
        },
    )
    applied = {"n": 0}
    monkeypatch.setattr(es, "apply_switch", lambda m: applied.__setitem__("n", applied["n"] + 1))
    app.dependency_overrides[get_verified_identity] = lambda: _make_admin_identity()
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/admin/setup/embedding/switch", json={"model": "x"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert applied["n"] == 1
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)
