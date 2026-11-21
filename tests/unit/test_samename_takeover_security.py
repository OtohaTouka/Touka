"""刪掉的靈魂留下的排程、身分設定、統計不給同名的新靈魂（ADR-0326；禁令 20 對應安全測試）.

修補前：刪除靈魂不刪它的心跳排程與統計，身分設定寫不進去也不會被發現；之後再建同名的新靈魂，
舊排程照原本的提示與權限跑起來、舊的角色設定套上去、舊的 token 用量與自主次數算在新靈魂頭上。
用編輯建立靈魂也少了「和自訂模型同名」「和人類帳號同名」兩道檢查。

修補後：
- 刪除靈魂：鎖內刪統計；取消註冊那步刪身分設定（兩種寫法），寫不進去照實回報；放開鎖之後刪排程
  （先確認名字仍然沒人用，最多 15 秒，清不掉只記警告）。
- 建立靈魂（含用編輯建立新資料夾）：所有拒絕檢查之後、名字不屬於任何代理人時才清。先清排程
  （心跳服務設定了卻連不上就不建立），再清身分設定與統計。用編輯建立時，名字還在擁有清單或
  模型清單裡算「修復」，什麼都不清。

排程用真的 ToukaHeartbeat app（ASGI，不連網路）；data 根與設定都在 conftest 測試沙盒。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.testclient import TestClient
from memory_server.services.mission_manager import MissionManager, mission_board_path
from memory_server.utils.zone_paths import ZoneRootsUnavailableError
from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.middleware.layers.monitor import MonitorLayer
from src.models.identity import AgentIdentity
from src.routers import souls as souls_router
from src.services import agent_leftovers, destructive_ops
from src.services.auth_service import AuthService
from src.services.heartbeat_schedule_bridge import DELETE_RETRY_ALLOWANCE
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.utils import async_db
from src.utils.agent_spellings import legacy_spellings

from ToukaHeartbeat import awaken
from ToukaHeartbeat.proxy_client import ProxyClient
from ToukaHeartbeat.storage_service import StorageService
from ToukaHeartbeat.token_provider import StorageTokenProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from src.models.user import User

ADMIN = AgentIdentity.for_role("takeover-admin", "admin", is_human=True)
HB_SECRET = "synthetic-takeover-heartbeat-secret"  # noqa: S105
HB_ADDRESS = "http://heartbeat-takeover.invalid:8082/trigger"
SOULS = (
    "OldSoul",
    "EditOld",
    "ConvOld",
    "BusySoul",
    "BadModel",
    "RegOld",
    "OwnedOld",
    "ModelOld",
    "SpareOld",
    "LockOld",
    "OfflineOld",
    "NoAddr",
    "OvrOld",
    "OvrFail",
    "StatOld",
    "EditNew",
    "HumanName",
    "ModelName",
    "ConvModel",
)


# --------------------------------------------------------------------------- #
# 共用
# --------------------------------------------------------------------------- #


@pytest.fixture
async def sandbox() -> AsyncIterator[Path]:
    """靈魂路由要 conftest 沙盒的對話資料庫；本檔用到的名字在結束時清掉看板、統計與資料夾."""
    data = Path(resource_manager.get_base_path())
    await async_db.init_db_async()
    yield data
    for agent_id in SOULS:
        mission_board_path(agent_id).unlink(missing_ok=True)
        for base in ("personal_drive", "legacy", "stats", "custom"):
            shutil.rmtree(data / base / agent_id, ignore_errors=True)


@pytest.fixture(autouse=True)
def registry(sandbox: Path) -> Iterator[Path]:
    """models.json、agents_registry.json 每個測試從空的開始，結束時還原."""
    _ = sandbox
    config = Path(get_config_dir())
    files = [config / "models.json", config / "agents_registry.json"]
    original = {f: (f.read_text(encoding="utf-8") if f.exists() else None) for f in files}
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}", encoding="utf-8")
    model_manager.load_initial_config()
    yield config / "agents_registry.json"
    for f, content in original.items():
        if content is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(content, encoding="utf-8")
    model_manager.load_initial_config()


@pytest.fixture
def admin() -> Iterator[tuple[AgentIdentity, str]]:
    """真的 admin 帳號（刪除與用編輯建立靈魂要重新輸入密碼）；結束時還原 users.json."""
    pw = "TakeoverPass123!"
    auth = AuthService()
    users = auth.users_file_path
    before = users.read_bytes() if users.exists() else None
    user = auth.create_user("takeover-admin@test.com", pw, role="admin")
    yield AgentIdentity.for_role(user.id, "admin", is_human=True), pw
    if before is None:
        users.unlink(missing_ok=True)
    else:
        users.write_bytes(before)


@dataclass
class Heartbeat:
    """真的 ToukaHeartbeat app（ASGI）與它的設定檔；``controls`` 讓請求失敗或在送出前做事."""

    storage: StorageService
    requests: list[httpx.Request] = field(default_factory=list)
    controls: dict[str, Any] = field(default_factory=dict)

    def put(self, *schedules: tuple[str, str]) -> None:
        self.storage.save_config(
            {
                "heartbeat_token": HB_SECRET,
                "schedules": [{"id": sid, "agent_id": agent} for sid, agent in schedules],
            }
        )

    def ids(self) -> list[str]:
        return [item["id"] for item in self.storage.load_config().get("schedules", [])]


@pytest.fixture
def heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Heartbeat:
    monkeypatch.setattr(agent_leftovers, "HEARTBEAT_EXTERNAL_URL", HB_ADDRESS)
    monkeypatch.setattr(agent_leftovers, "HEARTBEAT_TOKEN", HB_SECRET)
    storage = StorageService(tmp_path / "heartbeat.json", env_token=HB_SECRET)
    monkeypatch.setattr(awaken, "storage", storage)
    provider = StorageTokenProvider(storage)
    monkeypatch.setattr(awaken, "token_provider", provider)
    monkeypatch.setattr(awaken, "proxy_client", ProxyClient(provider))
    monkeypatch.setattr(awaken, "scheduler", AsyncIOScheduler(timezone=awaken.TAIPEI_TZ))
    hb = Heartbeat(storage)
    hb.put()
    real_client = httpx.AsyncClient

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            hb.requests.append(request)
            if hb.controls.get("status"):
                return httpx.Response(hb.controls["status"], request=request)
            if hb.controls.get("fake_delete_ok") and request.method == "DELETE":
                return httpx.Response(200, json={"status": "deleted"}, request=request)
            hook = hb.controls.get("before")
            if hook:
                hook(request)
            return await httpx.ASGITransport(app=awaken.app).handle_async_request(request)

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_client(*args, **kwargs, transport=Transport())

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return hb


def _send(method: str, url: str, body: dict[str, Any] | None, who: AgentIdentity = ADMIN) -> Any:
    app.dependency_overrides[get_verified_identity] = lambda: who
    try:
        return TestClient(app, raise_server_exceptions=False).request(method, url, json=body)
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def _code(response: Any) -> str:
    return response.json()["detail"]["code"]


def _create(agent_id: str, **config: Any) -> Any:
    return _send("POST", "/v1/souls", {"agent_id": agent_id, "config": config})


def _edit_create(agent_id: str, who: tuple[AgentIdentity, str], **config: Any) -> Any:
    body = {"config": {"is_toukasoul": True, **config}, "admin_password": who[1]}
    return _send("POST", f"/v1/souls/{agent_id}", body, who[0])


def _delete(agent_id: str, who: tuple[AgentIdentity, str]) -> Any:
    return _send("DELETE", f"/v1/souls/{agent_id}", {"admin_password": who[1]}, who[0])


def _stats_dir(agent_id: str) -> Path:
    return Path(resource_manager.get_stats_path(AgentIdentity(agent_id=agent_id)))


def _put_stats(agent_id: str) -> None:
    folder = _stats_dir(agent_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "token_usage.json").write_text('{"total_tokens": 999}', encoding="utf-8")


def _put_overrides(*agent_ids: str, extra: dict[str, Any] | None = None) -> None:
    data = {
        "tag_rules": {},
        "agent_overrides": {agent: {"role": "Owner", "capabilities": []} for agent in agent_ids},
        "default_role": "Base",
        **(extra or {}),
    }
    permission_service.identity_path.write_text(json.dumps(data), encoding="utf-8")
    permission_service.reload_all()


def _disk_overrides() -> dict[str, Any]:
    if not permission_service.identity_path.exists():
        return {}
    return json.loads(permission_service.identity_path.read_text(encoding="utf-8"))[
        "agent_overrides"
    ]


def _register(registry: Path, agent_id: str) -> None:
    data = json.loads(registry.read_text(encoding="utf-8"))
    data[agent_id] = [agent_id]
    registry.write_text(json.dumps(data), encoding="utf-8")


def _leftovers(agent_id: str) -> None:
    """名字底下放齊三種舊東西（排程另外放）."""
    _put_stats(agent_id)
    _put_overrides(agent_id, f"ToukaSoul{agent_id}", "Keep")


async def _put_board(agent_id: str, goal: str) -> None:
    await MissionManager().initialize_mission(AgentIdentity.for_role(agent_id, "Base"), goal)


def _goal(agent_id: str) -> str | None:
    path = mission_board_path(agent_id)
    return json.loads(path.read_text(encoding="utf-8"))["goal"] if path.exists() else None


# --------------------------------------------------------------------------- #
# 名字的兩種寫法
# --------------------------------------------------------------------------- #


async def test_legacy_spellings_always_has_two_values() -> None:
    assert legacy_spellings("Alice") == ("Alice", "ToukaSoulAlice")
    # 對話資料庫的 SQL 寫死 IN (?, ?)（禁令 24）：DebugAgent 只有一種寫法，回兩次
    assert legacy_spellings("DebugAgent") == ("DebugAgent", "DebugAgent")


async def test_debug_agent_sessions_still_use_two_values() -> None:
    assert async_db.soul_session_agents(AgentIdentity(agent_id="DebugAgent")) == (
        "DebugAgent",
        "DebugAgent",
    )
    assert async_db.soul_session_agents(AgentIdentity(agent_id="Alice")) == (
        "Alice",
        "ToukaSoulAlice",
    )


# --------------------------------------------------------------------------- #
# 刪除
# --------------------------------------------------------------------------- #


async def test_deleting_a_soul_removes_all_its_schedules_only(
    heartbeat: Heartbeat, admin: tuple[AgentIdentity, str]
) -> None:
    assert _create("OldSoul").status_code == 200
    heartbeat.put(
        ("a", "OldSoul"),
        ("b", "ToukaSoulOldSoul"),
        ("c", "OldSoul"),
        ("d", "Bob"),
        ("e", "oldsoul"),
    )
    response = _delete("OldSoul", admin)
    assert response.status_code == 200, response.text
    # 只差大小寫的是另一個代理人（ADR-0303、0325），不刪
    assert heartbeat.ids() == ["d", "e"]


async def test_schedules_are_removed_after_the_destructive_lock(
    heartbeat: Heartbeat, admin: tuple[AgentIdentity, str]
) -> None:
    assert _create("LockOld").status_code == 200
    heartbeat.put(("a", "LockOld"))
    held: list[bool] = []
    heartbeat.controls["before"] = lambda _request: held.append(
        destructive_ops.is_held(AgentIdentity(agent_id="LockOld"))
    )
    assert _delete("LockOld", admin).status_code == 200
    assert held
    assert not any(held)
    assert heartbeat.ids() == []


async def test_delete_still_works_when_schedules_cannot_be_reached(
    heartbeat: Heartbeat, admin: tuple[AgentIdentity, str], caplog: pytest.LogCaptureFixture
) -> None:
    assert _create("OfflineOld").status_code == 200
    heartbeat.put(("a", "OfflineOld"))
    heartbeat.controls["status"] = 503
    response = _delete("OfflineOld", admin)
    assert response.status_code == 200, response.text
    assert not (Path(resource_manager.get_base_path()) / "soul" / "OfflineOld").exists()
    assert "schedules of OfflineOld were not removed" in caplog.text
    # PR 2 起排程清不掉也列進 skipped（回應看得出來）
    assert "schedules" in response.json()["skipped"]


async def test_deleting_a_soul_removes_its_stats(
    sandbox: Path, admin: tuple[AgentIdentity, str]
) -> None:
    _ = sandbox
    assert _create("StatOld").status_code == 200
    _put_stats("StatOld")
    assert _delete("StatOld", admin).status_code == 200
    assert not _stats_dir("StatOld").exists()


async def test_stats_failure_on_delete_is_listed_in_skipped(
    sandbox: Path, admin: tuple[AgentIdentity, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """刪不掉統計（含意料外的例外）不擋刪除，回應的 skipped 列出 stats."""
    _ = sandbox
    assert _create("StatOld").status_code == 200
    _put_stats("StatOld")
    real_remove_dir = resource_manager.remove_dir
    stats_dir = _stats_dir("StatOld")

    def _remove_dir(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == stats_dir:
            raise ZoneRootsUnavailableError
        return real_remove_dir(path, *args, **kwargs)

    monkeypatch.setattr(resource_manager, "remove_dir", _remove_dir)
    response = _delete("StatOld", admin)
    assert response.status_code == 200, response.text
    assert "stats" in response.json()["skipped"]
    assert not (Path(resource_manager.get_base_path()) / "soul" / "StatOld").exists()


async def test_unexpected_schedule_error_after_delete_is_only_logged(
    sandbox: Path,
    admin: tuple[AgentIdentity, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """刪除已經做完：鎖外清排程遇到意料外的例外也只記 log，刪除照樣回成功."""
    _ = sandbox
    assert _create("OfflineOld").status_code == 200

    async def _broken(_identity: AgentIdentity) -> int:
        raise RuntimeError

    monkeypatch.setattr(agent_leftovers, "_remove_schedules", _broken)
    response = _delete("OfflineOld", admin)
    assert response.status_code == 200, response.text
    assert "schedules of OfflineOld were not removed" in caplog.text


async def test_identity_override_is_removed_on_delete(
    sandbox: Path, admin: tuple[AgentIdentity, str]
) -> None:
    _ = sandbox
    assert _create("OvrOld").status_code == 200
    _put_overrides("OvrOld", "ToukaSoulOvrOld", "Keep")
    assert _delete("OvrOld", admin).status_code == 200
    assert set(_disk_overrides()) == {"Keep"}
    assert set(permission_service.identity_map.agent_overrides) == {"Keep"}


async def test_identity_override_write_failure_is_reported_on_delete(
    sandbox: Path, admin: tuple[AgentIdentity, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = sandbox
    assert _create("OvrFail").status_code == 200
    _put_overrides("OvrFail")

    def _no_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError

    monkeypatch.setattr(resource_manager, "write_file_text", _no_write)
    response = _delete("OvrFail", admin)
    assert response.status_code == 500, response.text
    assert _code(response) == ErrorCode.SOUL_UNREGISTER_INCOMPLETE.value
    assert "identity_override" in response.json()["detail"]["failed_steps"]


async def test_refused_delete_touches_nothing(
    heartbeat: Heartbeat, admin: tuple[AgentIdentity, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _create("BusySoul").status_code == 200
    heartbeat.put(("a", "BusySoul"))
    _leftovers("BusySoul")

    async def _has_memory(*_args: Any, **_kwargs: Any) -> None:
        raise ToukaException(ErrorCode.SOUL_MEMORY_NOT_RESET)

    monkeypatch.setattr(souls_router, "assert_memory_cleared", _has_memory)
    response = _delete("BusySoul", admin)
    assert response.status_code == 409, response.text
    assert heartbeat.ids() == ["a"]
    assert _stats_dir("BusySoul").exists()
    assert "BusySoul" in _disk_overrides()


async def test_schedules_after_delete_spare_a_claimed_name(
    heartbeat: Heartbeat, sandbox: Path
) -> None:
    """放開鎖之後名字還有人用（刪除沒做完）：排程不刪，回報沒清成."""
    (sandbox / "soul" / "SpareOld").mkdir(parents=True)
    heartbeat.put(("a", "SpareOld"))
    failed = await agent_leftovers.clean_up_after_delete(AgentIdentity(agent_id="SpareOld"))
    assert failed == ["schedules"]
    assert heartbeat.ids() == ["a"]
    assert heartbeat.requests == []


async def test_schedule_cleanup_after_delete_has_a_deadline(
    heartbeat: Heartbeat, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _stall(_request: httpx.Request) -> None:
        await asyncio.Event().wait()

    heartbeat.put(("a", "Gone"))
    monkeypatch.setattr(agent_leftovers, "SCHEDULE_CLEANUP_SECONDS", 0.05)
    monkeypatch.setattr(
        agent_leftovers.HeartbeatScheduleBridge,
        "delete_for_agent",
        lambda _self, _spellings: _stall(None),  # type: ignore[arg-type]
    )
    failed = await asyncio.wait_for(
        agent_leftovers.clean_up_after_delete(AgentIdentity(agent_id="Gone")), 2
    )
    assert failed == ["schedules"]


# --------------------------------------------------------------------------- #
# 建立
# --------------------------------------------------------------------------- #


async def test_create_soul_clears_old_schedules(heartbeat: Heartbeat) -> None:
    heartbeat.put(("a", "OldSoul"), ("b", "ToukaSoulOldSoul"), ("c", "Bob"))
    assert _create("OldSoul").status_code == 200
    assert heartbeat.ids() == ["c"]


async def test_edit_create_clears_old_schedules(
    heartbeat: Heartbeat, admin: tuple[AgentIdentity, str]
) -> None:
    heartbeat.put(("a", "EditOld"), ("c", "Bob"))
    response = _edit_create("EditOld", admin)
    assert response.status_code == 200, response.text
    assert heartbeat.ids() == ["c"]


async def test_create_refuses_when_schedules_cannot_be_checked(
    heartbeat: Heartbeat, sandbox: Path, registry: Path
) -> None:
    heartbeat.put(("a", "OldSoul"))
    _leftovers("OldSoul")
    await _put_board("OldSoul", "leftover")
    await async_db.init_db_async()
    async with async_db.get_async_db() as db:
        await db.execute(
            "INSERT INTO user_sessions (id, title, messages, updatedAt, agent_id, auth_level, "
            "active_mcp_servers, active_skills, last_activity_at) "
            "VALUES ('takeover-old-chat', 't', '{}', 1, 'OldSoul', 'Manual', '[]', '[]', 1)"
        )
        await db.commit()
    try:
        heartbeat.controls["status"] = 503
        response = _create("OldSoul")
        assert response.status_code == 502, response.text
        assert _code(response) == ErrorCode.HEARTBEAT_UPSTREAM_FAILED.value
        assert not (sandbox / "soul" / "OldSoul").exists()
        assert "OldSoul" not in json.loads(registry.read_text(encoding="utf-8"))
        assert not any("OldSoul" in u.owned_agents for u in AuthService().get_all_users())
        # 排程是第一步：拒絕時看板、身分設定、統計、對話（ADR-0326 PR 3a）都還沒動
        assert _goal("OldSoul") == "leftover"
        assert _stats_dir("OldSoul").exists()
        assert "OldSoul" in _disk_overrides()
        async with async_db.get_async_db() as db:
            rows = await db.execute_fetchall(
                "SELECT agent_id FROM user_sessions WHERE id = 'takeover-old-chat'"
            )
        assert [row[0] for row in rows] == ["OldSoul"]
    finally:
        async with async_db.get_async_db() as db:
            await db.execute("DELETE FROM user_sessions WHERE id = 'takeover-old-chat'")
            await db.commit()


async def test_no_heartbeat_address_skips_the_schedule_check(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _ = sandbox
    monkeypatch.setattr(agent_leftovers, "HEARTBEAT_EXTERNAL_URL", None)
    assert _create("NoAddr").status_code == 200
    assert "no Heartbeat address" in caplog.text


async def test_identity_override_is_cleared_on_create(sandbox: Path) -> None:
    _ = sandbox
    _put_overrides("OvrOld", "ToukaSoulOvrOld", "Keep")
    assert _create("OvrOld").status_code == 200
    assert set(_disk_overrides()) == {"Keep"}


async def test_identity_override_write_failure_refuses_create(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_overrides("OvrFail")

    def _no_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError

    monkeypatch.setattr(resource_manager, "write_file_text", _no_write)
    response = _create("OvrFail")
    assert response.status_code == 500, response.text
    assert not (sandbox / "soul" / "OvrFail").exists()


async def test_creating_a_soul_clears_old_stats(sandbox: Path) -> None:
    _ = sandbox
    _put_stats("StatOld")
    assert _create("StatOld").status_code == 200
    assert not _stats_dir("StatOld").exists()


async def test_refused_create_touches_nothing(heartbeat: Heartbeat) -> None:
    heartbeat.put(("a", "BadModel"))
    _leftovers("BadModel")
    response = _create("BadModel", base_model="no-such-model")
    assert response.status_code == 404, response.text
    assert heartbeat.ids() == ["a"]
    assert heartbeat.requests == []
    assert _stats_dir("BadModel").exists()
    assert "BadModel" in _disk_overrides()


async def test_refused_edit_create_touches_nothing(
    heartbeat: Heartbeat, sandbox: Path, admin: tuple[AgentIdentity, str]
) -> None:
    heartbeat.put(("a", "BadModel"))
    _leftovers("BadModel")
    await _put_board("BadModel", "leftover")
    response = _edit_create("BadModel", admin, base_model="no-such-model")
    assert response.status_code == 404, response.text
    assert heartbeat.ids() == ["a"]
    assert heartbeat.requests == []
    assert _stats_dir("BadModel").exists()
    assert "BadModel" in _disk_overrides()
    assert _goal("BadModel") == "leftover"
    assert not (sandbox / "soul" / "BadModel").exists()


async def test_converting_a_folder_clears_nothing(
    heartbeat: Heartbeat, sandbox: Path, admin: tuple[AgentIdentity, str]
) -> None:
    (sandbox / "soul" / "ConvOld").mkdir(parents=True)
    (sandbox / "soul" / "ConvOld" / "config.json").write_text(
        json.dumps({"is_toukasoul": False}), encoding="utf-8"
    )
    heartbeat.put(("a", "ConvOld"))
    _leftovers("ConvOld")
    response = _edit_create("ConvOld", admin)
    assert response.status_code == 200, response.text
    assert heartbeat.ids() == ["a"]
    assert _stats_dir("ConvOld").exists()
    assert "ConvOld" in _disk_overrides()


def _claim_in_registry(registry: Path, agent_id: str) -> None:
    _register(registry, agent_id)


def _claim_in_owned_list(_registry: Path, agent_id: str) -> None:
    auth = AuthService()

    def _own(users: list[User]) -> None:
        users[0].owned_agents.append(agent_id)

    auth._atomic_users_write(_own)  # noqa: SLF001


def _claim_in_models(_registry: Path, agent_id: str) -> None:
    model_manager.register_soul_model(agent_id)


def _put_models(*models: dict[str, Any]) -> None:
    (Path(get_config_dir()) / "models.json").write_text(
        json.dumps({"models": list(models)}), encoding="utf-8"
    )
    model_manager.load_initial_config()


def _claim_in_models_disabled(_registry: Path, agent_id: str) -> None:
    """管理員把靈魂模型關掉了：還是它的名字."""
    _put_models({"id": f"ToukaSoul{agent_id}", "owned_by": "touka", "enabled": False})


def _claim_in_models_offline(_registry: Path, agent_id: str) -> None:
    """不帶前綴的舊寫法，而且很久沒見到（離線）：還是它的名字."""
    _put_models({"id": agent_id, "owned_by": "touka", "last_seen": 0})


@pytest.mark.parametrize(
    ("path", "claim"),
    [
        ("create", _claim_in_registry),
        ("edit", _claim_in_registry),
        # 擁有清單、模型清單只在用編輯建立時算：create_soul 會用認領清掉擁有清單的殘留
        ("edit", _claim_in_owned_list),
        ("edit", _claim_in_models),
        ("edit", _claim_in_models_disabled),
        ("edit", _claim_in_models_offline),
    ],
    ids=[
        "create-registry",
        "edit-registry",
        "edit-owned",
        "edit-models",
        "edit-models-disabled",
        "edit-models-offline",
    ],
)
async def test_claimed_names_are_not_cleared(
    heartbeat: Heartbeat,
    registry: Path,
    admin: tuple[AgentIdentity, str],
    path: str,
    claim: Any,
) -> None:
    heartbeat.put(("a", "RegOld"))
    _leftovers("RegOld")
    await _put_board("RegOld", "kept")
    claim(registry, "RegOld")
    response = _create("RegOld") if path == "create" else _edit_create("RegOld", admin)
    assert response.status_code == 200, response.text
    assert heartbeat.ids() == ["a"]
    assert _stats_dir("RegOld").exists()
    assert "RegOld" in _disk_overrides()
    assert _goal("RegOld") == "kept"


# --------------------------------------------------------------------------- #
# 用編輯建立少的兩道撞名檢查
# --------------------------------------------------------------------------- #


async def test_edit_create_refuses_a_custom_model_name(
    sandbox: Path, admin: tuple[AgentIdentity, str]
) -> None:
    (sandbox / "custom" / "ModelName").mkdir(parents=True)
    (sandbox / "custom" / "ModelName" / "config.json").write_text(
        json.dumps({"id": "ModelName"}), encoding="utf-8"
    )
    response = _edit_create("ModelName", admin)
    assert response.status_code == 409, response.text
    assert _code(response) == ErrorCode.MODEL_ALREADY_EXISTS.value
    assert not (sandbox / "soul" / "ModelName").exists()


async def test_converting_a_folder_refuses_a_custom_model_name(
    sandbox: Path, admin: tuple[AgentIdentity, str]
) -> None:
    (sandbox / "soul" / "ConvModel").mkdir(parents=True)
    (sandbox / "soul" / "ConvModel" / "config.json").write_text(
        json.dumps({"is_toukasoul": False}), encoding="utf-8"
    )
    (sandbox / "custom" / "ConvModel").mkdir(parents=True)
    (sandbox / "custom" / "ConvModel" / "config.json").write_text(
        json.dumps({"id": "ConvModel"}), encoding="utf-8"
    )
    response = _edit_create("ConvModel", admin)
    assert response.status_code == 409, response.text
    stored = json.loads((sandbox / "soul" / "ConvModel" / "config.json").read_text("utf-8"))
    assert stored["is_toukasoul"] is False


async def test_edit_create_refuses_a_human_id(
    sandbox: Path, admin: tuple[AgentIdentity, str]
) -> None:
    human = AuthService().create_user("same-name@test.com", "SameNamePass123!", role="normal")
    response = _edit_create(human.id, admin)
    assert response.status_code == 422, response.text
    assert _code(response) == ErrorCode.VALIDATION_ERROR.value
    assert not (sandbox / "soul" / human.id).exists()


async def test_converting_a_folder_refuses_a_human_id(
    sandbox: Path, admin: tuple[AgentIdentity, str]
) -> None:
    human = AuthService().create_user("same-folder@test.com", "SameFolderPass123!", role="normal")
    folder = sandbox / "soul" / human.id
    folder.mkdir(parents=True)
    try:
        (folder / "config.json").write_text(json.dumps({"is_toukasoul": False}), encoding="utf-8")
        response = _edit_create(human.id, admin)
        assert response.status_code == 422, response.text
        assert _code(response) == ErrorCode.VALIDATION_ERROR.value
        stored = json.loads((folder / "config.json").read_text("utf-8"))
        assert stored["is_toukasoul"] is False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 身分設定的寫入：鎖內從磁碟重讀、立刻生效、存檔不寫回
# --------------------------------------------------------------------------- #


async def test_override_removal_rereads_the_file() -> None:
    _put_overrides("Gone", "Keep")
    # 磁碟上的檔被別人改過（快取還是舊的）：清除以磁碟為準，別的欄位原樣保留
    data = json.loads(permission_service.identity_path.read_text(encoding="utf-8"))
    data["agent_overrides"]["Later"] = {"role": "Owner", "capabilities": []}
    data["note"] = "kept as written"
    permission_service.identity_path.write_text(json.dumps(data), encoding="utf-8")
    assert permission_service.remove_agent_overrides(("Gone", "ToukaSoulGone"))
    stored = json.loads(permission_service.identity_path.read_text(encoding="utf-8"))
    assert set(stored["agent_overrides"]) == {"Keep", "Later"}
    assert stored["note"] == "kept as written"


async def test_override_removal_refuses_an_unreadable_file() -> None:
    permission_service.identity_path.write_text("{not json", encoding="utf-8")
    assert permission_service.remove_agent_overrides(("Gone", "ToukaSoulGone")) is False
    assert permission_service.identity_path.read_text(encoding="utf-8") == "{not json"


def _no_hot_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    """關掉 2 秒一次的熱重載（測試結束還原；conftest 不會重設它）."""
    monkeypatch.setattr(permission_service, "_check_interval", 999999.0)
    monkeypatch.setattr(permission_service, "_last_reload_check", 10.0**12)


async def test_override_removal_takes_effect_now(monkeypatch: pytest.MonkeyPatch) -> None:
    _put_overrides("Gone", "Keep")
    _no_hot_reload(monkeypatch)
    assert permission_service.remove_agent_overrides(("Gone", "ToukaSoulGone"))
    assert set(permission_service.identity_map.agent_overrides) == {"Keep"}


async def test_identity_save_does_not_restore_a_removed_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快取裡還有舊覆寫（不是靈魂、畫面看不到），磁碟上已經清掉：存檔不把它寫回去."""
    _put_overrides("Gone", "Keep")
    _no_hot_reload(monkeypatch)
    data = json.loads(permission_service.identity_path.read_text(encoding="utf-8"))
    del data["agent_overrides"]["Gone"]
    permission_service.identity_path.write_text(json.dumps(data), encoding="utf-8")
    body = {"tag_rules": {}, "agent_overrides": {}, "default_role": "Base"}
    admin_identity = AgentIdentity.for_role("takeover-admin", "admin", is_human=True)
    response = _send("POST", "/v1/identity", body, admin_identity)
    assert response.status_code == 200, response.text
    assert "Gone" not in _disk_overrides()
    assert "Keep" in _disk_overrides()  # 不是靈魂的舊覆寫照舊保留


# --------------------------------------------------------------------------- #
# 前提：自訂模型平常不寫自己的統計
# --------------------------------------------------------------------------- #


async def test_custom_model_chat_keeps_the_human_identity() -> None:
    """跟自訂模型聊天時身分仍是人類（只有靈魂會換身分），統計記在人類名下."""
    human = AgentIdentity.for_role("chat-human", "normal", is_human=True)
    ctx: dict[str, Any] = {"identity": human}
    MonitorLayer()._maybe_assume_soul_identity(ctx, "SomeModel", is_toukasoul=False)  # noqa: SLF001
    assert ctx["identity"] is human


# --------------------------------------------------------------------------- #
# 排程逐筆刪除：版本衝突重讀、刪了卻還在時不會一直轉
# --------------------------------------------------------------------------- #


async def test_schedule_deletion_rereads_after_a_revision_conflict(heartbeat: Heartbeat) -> None:
    heartbeat.put(("a", "Gone"), ("b", "ToukaSoulGone"), ("c", "Bob"))
    changed: list[bool] = []

    def _someone_edits(request: httpx.Request) -> None:
        if request.method == "DELETE" and not changed:
            changed.append(True)  # 第一次刪除之前，別人改了排程：版本號變了
            heartbeat.put(("a", "Gone"), ("b", "ToukaSoulGone"), ("c", "Bob"), ("d", "Carol"))

    heartbeat.controls["before"] = _someone_edits
    bridge = agent_leftovers._system_bridge()  # noqa: SLF001
    assert bridge is not None
    assert await bridge.delete_for_agent(legacy_spellings("Gone")) == 2
    assert heartbeat.ids() == ["c", "d"]


async def test_schedule_deletion_gives_up_when_deletes_do_not_stick(heartbeat: Heartbeat) -> None:
    heartbeat.put(("a", "Gone"), ("c", "Bob"))
    heartbeat.controls["fake_delete_ok"] = True  # 刪除回成功，排程卻還在
    bridge = agent_leftovers._system_bridge()  # noqa: SLF001
    assert bridge is not None
    with pytest.raises(ToukaException) as error:
        await bridge.delete_for_agent(legacy_spellings("Gone"))
    assert error.value.code == ErrorCode.HEARTBEAT_OUTCOME_UNKNOWN
    deletes = [r for r in heartbeat.requests if r.method == "DELETE"]
    assert len(deletes) == 1 + DELETE_RETRY_ALLOWANCE


# --------------------------------------------------------------------------- #
# 身分設定的鎖：兩個寫入交錯時不會互相蓋掉
# --------------------------------------------------------------------------- #


async def test_override_writes_do_not_lose_each_other() -> None:
    """A 讀完、還沒寫時 B 想寫：有鎖時 B 等 A 寫完再從磁碟讀；沒有鎖時 A 會把 B 的刪除蓋回去."""
    _put_overrides("FromA", "FromB", "Keep")
    a_has_read = threading.Event()
    b_finished = threading.Event()

    def _slow_a(raw: dict[str, Any] | None) -> dict[str, Any] | None:
        assert raw is not None
        a_has_read.set()
        b_finished.wait(timeout=0.5)  # 沒有鎖時 B 會在這段時間寫完
        overrides = {k: v for k, v in raw["agent_overrides"].items() if k != "FromA"}
        return {**raw, "agent_overrides": overrides}

    def _b() -> None:
        a_has_read.wait(timeout=5)
        permission_service.remove_agent_overrides(("FromB",))
        b_finished.set()

    writer_b = threading.Thread(target=_b)
    writer_b.start()
    assert permission_service.edit_identity_map(_slow_a)
    writer_b.join(timeout=5)
    assert set(_disk_overrides()) == {"Keep"}
