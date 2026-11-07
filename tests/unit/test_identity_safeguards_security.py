"""身分與防呆（ADR-0304）。

- 首裝卡（LDH）帶記號，系統認領後失效；重送時的角色和發卡時一樣（不帶類型旗標），
  永遠不是人類。人類分支與 ``get_current_user`` 只收 ``type == "access"``。
- 檔案工具的專案只來自系統（ctx 或 session 綁定的專案），不收模型在參數裡自己填的編號。
- 還原中不能刪靈魂、不能刪除或改名自訂模型（身分與擁有權先檢查）。
- ``/heartbeat/test``、``GET /``、``/mcp/hub/sync`` 不把例外原文回給呼叫者。

全部在 conftest 測試沙盒；本檔每個測試前清空沙盒的 users.json（LDH 依賴「未認領」），
測後放回原本的使用者；建立的靈魂與自訂模型資料夾在測後刪掉。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from memory_server import config_utils
from src import main
from src.core.exceptions import ToukaException
from src.core.security import AuthSecretManager
from src.main import app
from src.models.identity import AgentIdentity, IdentityMapConfig
from src.models.policy import PolicyDecision
from src.models.system_restore import RestoreStatus
from src.models.user import User
from src.routers import custom_models as custom_models_router
from src.routers import heartbeat as heartbeat_router
from src.routers import mcp as mcp_router
from src.routers import models as models_router
from src.routers import system as system_router
from src.routers.auth import get_current_user
from src.services import identity_service as identity_service_module
from src.services.agent_service import agent_service
from src.services.auth_service import AuthService
from src.services.authorization_gateway import require_privileged
from src.services.identity_router import get_optional_identity, get_verified_identity
from src.services.identity_service import identity_service
from src.services.isolation.status import LevelDecision, isolation_monitor
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.services.tool_executor import ToolExecutor
from src.utils.async_db import session_service
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from collections.abc import Iterator

REAL_PW = "RealPassword123!"
SOUL_ID = "pr2_replay_soul"
MARKER = "PR2-EXCEPTION-TEXT-7f3a"
_OWNER_CAPS = ["heartbeat_awakening", "can_supervise_dispatch"]
_CONTROLLED_MAP = {
    "tag_rules": {"is_toukasoul": {"role": "Owner", "capabilities": _OWNER_CAPS}},
    "agent_overrides": {"DebugAgent": {"role": "Debugger", "capabilities": _OWNER_CAPS}},
    "default_role": "Base",
}
_ADMIN = AgentIdentity.for_role("pr2-sec-admin", "admin", is_human=True)
_MODEL_BODY = {"name": "x", "description": "", "systemPrompt": "PROMPT", "config": {}}
_hosts = itertools.count(10)


# ── 共用 ─────────────────────────────────────────────────────────────────────────


def _swap_users(new: list[User]) -> list[User]:
    """把沙盒 users.json 換成 ``new``，回傳原本的清單。"""

    def _mutate(users: list[User]) -> list[User]:
        old = list(users)
        users[:] = new
        return old

    return AuthService()._atomic_users_write(_mutate)


@pytest.fixture(autouse=True)
def _users_hygiene() -> Iterator[None]:
    saved = _swap_users([])
    yield
    _swap_users(saved)
    app.dependency_overrides.pop(get_verified_identity, None)


def _request(headers: dict[str, str] | None = None) -> MagicMock:
    req = MagicMock(spec=Request)
    req.client.host = f"127.0.1.{next(_hosts) % 250}"  # LDH 限流以來源為單位，每次換一個
    req.headers = headers or {}
    req.state = SimpleNamespace()
    return req


async def _ldh_card(agent_id: str | None = None) -> str:
    req = _request({"X-Agent-ID": agent_id} if agent_id else None)
    assert await get_optional_identity(req, None) is not None
    return req.state.new_auth_token


def _claim() -> User:
    return AuthService().create_user("pr2-claimed@test.com", REAL_PW, role="admin")


def _seed_temp_admin() -> User:
    auth = AuthService()
    user = User(
        id="pr2-temp-admin-id",
        email="pr2-bootstrap@test.com",
        role="admin",
        hashed_password=auth.hash_password("TempBootstrap123!"),
        is_temp_password=True,
    )
    auth._atomic_users_write(lambda users: users.append(user))
    return user


def _token(payload: dict[str, Any]) -> str:
    now = int(get_current_epoch())
    body = {"iat": now, "exp": now + 3600, "iss": "ToukaProxy", **payload}
    return jwt.encode(body, AuthSecretManager.get_secret(), algorithm="HS256")


@pytest.fixture
def controlled_map(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """identity_map 釘成 is_toukasoul→Owner、DebugAgent→Debugger，並建一個真的靈魂資料夾。"""
    controlled = IdentityMapConfig.model_validate(_CONTROLLED_MAP)
    monkeypatch.setattr(permission_service, "identity_map", controlled)
    monkeypatch.setattr(permission_service, "_auto_reload_check", lambda: None)
    soul_dir = Path(config_utils.get_app_data_dir()) / "soul" / SOUL_ID
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "config.json").write_text(json.dumps({"is_toukasoul": True}), encoding="utf-8")
    yield
    shutil.rmtree(soul_dir, ignore_errors=True)


# ── 首裝卡 ───────────────────────────────────────────────────────────────────────


async def test_ldh_card_carries_the_first_install_mark():
    payload = jwt.decode(await _ldh_card(), AuthSecretManager.get_secret(), algorithms=["HS256"])
    assert identity_service_module.LDH_CARD_SOURCE == "ldh"
    assert payload.get("src") == identity_service_module.LDH_CARD_SOURCE


async def test_ldh_card_stops_working_once_the_system_is_claimed():
    card = await _ldh_card()
    assert identity_service.verify_token(card) is not None
    _claim()
    assert identity_service.verify_token(card) is None
    bearer = HTTPAuthorizationCredentials(scheme="Bearer", credentials=card)
    with pytest.raises(ToukaException) as exc:
        await get_optional_identity(_request(), bearer)
    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "AUTH_INVALID_TOKEN"


async def test_ldh_card_still_works_before_the_first_admin_sets_a_password(controlled_map):
    _seed_temp_admin()  # 只有暫時密碼的 admin＝還沒認領
    identity = identity_service.verify_token(await _ldh_card())
    assert identity is not None
    assert identity.agent_role == "Debugger"
    assert require_privileged(identity) is identity


async def test_replayed_ldh_card_keeps_the_role_it_was_issued_with(controlled_map):
    role, _caps = agent_service.resolve_role_and_caps(SOUL_ID)
    assert role == "Owner"  # 前提：帶類型旗標的解析會給 Owner
    identity = identity_service.verify_token(await _ldh_card(SOUL_ID))
    assert identity is not None
    assert identity.agent_role == "Base"
    assert identity.is_human is False


@pytest.mark.parametrize("marked", [False, True])
async def test_untyped_card_naming_a_user_is_never_a_human(marked: bool):
    user = _seed_temp_admin()
    claims: dict[str, Any] = {"sub": user.id}
    if marked:
        claims["src"] = "ldh"
    card = _token(claims)
    assert identity_service.verify_token(card) is None
    with pytest.raises(ToukaException) as exc:
        get_current_user(token=card, auth_service=AuthService())
    assert exc.value.status_code == 401


async def test_access_token_of_a_user_still_works():
    user = _seed_temp_admin()
    access = _token({"sub": user.id, "type": "access"})
    identity = identity_service.verify_token(access)
    assert identity is not None and identity.is_human
    assert get_current_user(token=access, auth_service=AuthService()).id == user.id


async def test_ldh_issues_no_card_for_a_user_id():
    user = _seed_temp_admin()
    req = _request({"X-Agent-ID": user.id})
    assert await get_optional_identity(req, None) is None
    assert getattr(req.state, "new_auth_token", None) is None


async def test_ldh_card_is_refused_when_the_user_list_cannot_be_read(monkeypatch):
    card = await _ldh_card()

    def _broken(_self: AuthService) -> bool:
        raise OSError(MARKER)

    monkeypatch.setattr(AuthService, "any_claimed_user", _broken)
    assert identity_service.verify_token(card) is None


async def test_plain_agent_card_without_the_mark_keeps_working_after_claim():
    card = identity_service.issue_token(AgentIdentity.for_role("DebugAgent", "Debugger"))
    _claim()
    assert identity_service.verify_token(card) is not None


# ── 專案編號只來自系統 ───────────────────────────────────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return json.dumps({"ok": True})


@pytest.fixture
def executor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _session(session_id: str) -> dict[str, Any] | None:
        if session_id != "s-bound":
            return None
        return {"id": session_id, "project_id": "bound-project"}

    async def _level_for(_identity: Any, _project_root: Any) -> LevelDecision:
        return LevelDecision("none", "native_platform")

    monkeypatch.setattr(session_service, "get_session_by_id", _session)
    monkeypatch.setattr(isolation_monitor, "level_for", _level_for)
    allow = PolicyDecision(effect="ALLOW", reason="ok")
    monkeypatch.setattr(permission_service, "evaluate_policy", lambda _c: allow)
    monkeypatch.setattr(
        permission_service,
        "resolve_grants",
        lambda _role: SimpleNamespace(tools=SimpleNamespace(allows=lambda _name: True)),
    )


async def _run_tool(
    name: str, args: dict[str, Any], session_id: str, ctx_project: str | None = None
) -> list[dict[str, Any]]:
    recorder = _Recorder()
    executor = ToolExecutor(
        protocol_manager=None,
        tool_registry={name: recorder},
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )
    actor = AgentIdentity.for_role(agent_id="ProjectProbe", role="Owner", is_human=False)
    ctx: dict[str, Any] = {"identity": actor, "auth_level": "YOLO", "session_id": session_id}
    if ctx_project:
        ctx["project_id"] = ctx_project
    queue: asyncio.Queue = asyncio.Queue()
    async with asyncio.timeout(5):
        await executor.execute_to_queue(name, dict(args), "req-pr2", ctx, queue, "call-pr2")
    return recorder.calls


# 第二個名稱是隨便取的工具：專案編號在執行器裡決定，和是哪個工具無關
@pytest.mark.parametrize("tool", ["project_tools", "pr2_probe_tool"])
async def test_tool_gets_the_session_project_not_the_model_argument(executor_env, tool: str):
    calls = await _run_tool(tool, {"action": "list", "project_id": "other-project"}, "s-bound")
    assert [c["project_id"] for c in calls] == ["bound-project"]


@pytest.mark.parametrize("tool", ["project_tools", "pr2_probe_tool"])
async def test_tool_gets_no_project_when_the_session_has_none(executor_env, tool: str):
    calls = await _run_tool(tool, {"action": "list", "project_id": "other-project"}, "s-unbound")
    assert [c["project_id"] for c in calls] == [None]


# 系統在 ctx 帶的專案優先於 session 綁定（目前產品程式沒有地方設，先釘住順序）
@pytest.mark.parametrize("session_id", ["s-bound", "s-unbound"])
async def test_project_in_the_pipeline_context_comes_before_the_session(executor_env, session_id):
    args = {"action": "list", "project_id": "other-project"}
    calls = await _run_tool("pr2_probe_tool", args, session_id, ctx_project="ctx-project")
    assert [c["project_id"] for c in calls] == ["ctx-project"]


# ── 還原中 ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def restore_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_router, "_last_restore", RestoreStatus(state="running"))


def _client(who: AgentIdentity) -> TestClient:
    app.dependency_overrides[get_verified_identity] = lambda: who
    return TestClient(app, raise_server_exceptions=False)


def test_soul_delete_is_refused_during_restore_before_the_password_check(
    monkeypatch, restore_running
):
    user = _claim()
    verify = MagicMock(return_value=True)
    monkeypatch.setattr(AuthService, "verify_password", verify)
    admin = AgentIdentity.for_role(user.id, "admin", is_human=True, owned_agents=[])
    r = _client(admin).request("DELETE", "/v1/souls/PR2Soul", json={"admin_password": "x"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SYSTEM_RESTORE_IN_PROGRESS"
    verify.assert_not_called()


def test_soul_delete_by_a_non_admin_is_still_forbidden_during_restore(restore_running):
    normal = AgentIdentity.for_role("pr2-normal", "normal", is_human=True, owned_agents=[])
    r = _client(normal).request("DELETE", "/v1/souls/PR2Soul", json={"admin_password": "x"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "AUTH_ADMIN_REQUIRED"


def _custom_dir(model_id: str) -> Path:
    return Path(resource_manager.get_base_path()) / "custom" / model_id


@pytest.fixture
def custom_model(monkeypatch: pytest.MonkeyPatch) -> Any:
    """建立一個自訂模型（擁有清單換成 MagicMock）；結束時清掉本檔建立的資料夾。"""
    monkeypatch.setattr(custom_models_router, "user_service", MagicMock())
    created = "PR2RestoreModel"
    r = _client(_ADMIN).post(f"/v1/ui/custom_models/{created}", json=_MODEL_BODY)
    assert r.status_code == 200, r.text
    yield created
    for name in (created, "PR2RestoreRenamed"):
        shutil.rmtree(_custom_dir(name), ignore_errors=True)


def test_custom_model_delete_is_refused_during_restore(custom_model, restore_running):
    r = _client(_ADMIN).delete(f"/v1/ui/custom_models/{custom_model}")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SYSTEM_RESTORE_IN_PROGRESS"
    assert _custom_dir(custom_model).is_dir()


def _give_to_another_user(model_id: str) -> None:
    owner = AuthService().create_user("pr2-owner@test.com", REAL_PW, role="normal")

    def _mutate(users: list[User]) -> None:
        for user in users:
            if user.id == owner.id:
                user.owned_agents = [model_id]

    AuthService()._atomic_users_write(_mutate)


def test_custom_model_delete_checks_ownership_before_restore(custom_model, restore_running):
    _give_to_another_user(custom_model)
    stranger = AgentIdentity.for_role("PR2Stranger", "Owner", is_human=False)
    r = _client(stranger).delete(f"/v1/ui/custom_models/{custom_model}")
    assert r.status_code in (403, 404)
    assert _custom_dir(custom_model).is_dir()


def test_model_registry_delete_of_a_custom_model_is_refused_during_restore(
    monkeypatch, custom_model, restore_running
):
    registry_delete = MagicMock(return_value=True)
    monkeypatch.setattr(models_router._model_manager, "delete_model", registry_delete)
    r = _client(_ADMIN).delete(f"/v1/models/{custom_model}")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SYSTEM_RESTORE_IN_PROGRESS"
    registry_delete.assert_not_called()
    assert _custom_dir(custom_model).is_dir()


def test_custom_model_rename_is_refused_during_restore(custom_model, restore_running):
    r = _client(_ADMIN).post(
        f"/v1/ui/custom_models/{custom_model}/rename", json={"new_id": "PR2RestoreRenamed"}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SYSTEM_RESTORE_IN_PROGRESS"
    assert _custom_dir(custom_model).is_dir()
    assert not _custom_dir("PR2RestoreRenamed").exists()


def test_custom_model_rename_is_refused_when_restore_starts_while_it_checks(
    monkeypatch, custom_model
):
    monkeypatch.setattr(system_router, "_last_restore", RestoreStatus(state="idle"))
    original = custom_models_router._assert_collection_free

    async def _restore_starts(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        monkeypatch.setattr(system_router, "_last_restore", RestoreStatus(state="running"))

    monkeypatch.setattr(custom_models_router, "_assert_collection_free", _restore_starts)
    r = _client(_ADMIN).post(
        f"/v1/ui/custom_models/{custom_model}/rename", json={"new_id": "PR2RestoreRenamed"}
    )
    assert r.status_code == 409
    assert _custom_dir(custom_model).is_dir()
    assert not _custom_dir("PR2RestoreRenamed").exists()


# ── 錯誤原文不回給呼叫者 ─────────────────────────────────────────────────────────


class _UnreachableClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _UnreachableClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def post(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(MARKER)


def test_heartbeat_probe_does_not_return_the_exception_text(monkeypatch):
    monkeypatch.setattr(heartbeat_router, "HEARTBEAT_EXTERNAL_URL", "http://heartbeat.invalid/x")
    monkeypatch.setattr(heartbeat_router, "HEARTBEAT_TOKEN", "pr2-token")
    monkeypatch.setattr(heartbeat_router.httpx, "AsyncClient", _UnreachableClient)
    r = TestClient(app, raise_server_exceptions=False).get("/v1/heartbeat/test")
    assert r.status_code == 200
    assert r.json()["status"] == "error"
    assert MARKER not in r.text


@pytest.mark.parametrize(
    ("error", "prefix"),
    [
        (ImportError(f"dependency broke {MARKER}"), "Memory System dependency error"),
        (RuntimeError(MARKER), "Memory System crash"),
    ],
)
def test_degraded_reason_carries_no_exception_text(monkeypatch, error: Exception, prefix: str):
    for name in ("SYSTEM_DEGRADED", "DEGRADED_REASON", "memory_tool_registry"):
        monkeypatch.setattr(main, name, getattr(main, name))
    monkeypatch.setattr(main, "memory_tools_schema", main.memory_tools_schema)

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    with monkeypatch.context() as patched:
        patched.setattr(main.importlib, "import_module", _raise)
        main.load_memory_system()
    assert main.SYSTEM_DEGRADED is True
    assert main.DEGRADED_REASON.startswith(prefix)
    assert MARKER not in main.DEGRADED_REASON
    r = TestClient(app, raise_server_exceptions=False).get("/")
    assert MARKER not in r.text


class _FailingHub:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def sync(self) -> dict[str, Any]:
        raise RuntimeError(MARKER)


def test_mcp_hub_sync_failure_does_not_return_the_exception_text(monkeypatch):
    monkeypatch.setattr(mcp_router, "MCPHubService", _FailingHub)
    r = _client(_ADMIN).post("/v1/mcp/hub/sync")
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "SYSTEM_ERROR"
    assert MARKER not in r.text
