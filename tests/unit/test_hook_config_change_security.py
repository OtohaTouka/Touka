"""ConfigChange 的接線與防漏（ADR-0292 決定 17）。

- 每個會改狀態的路由要嘛在確認存檔成功後呼叫 ``notify_config_change``，要嘛列在下面的「不是設定」
  並寫理由；新端點沒分類就紅。
- 只通知：hook 回擋下時設定照樣存好，存檔失敗時 hook 不會收到事件。

全部在 sandbox，設定檔在 tmp_path，hook 走假的 http runner。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, get_args
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.routing import APIRoute
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.autonomy import AutonomyConfigModel
from src.models.hooks import HooksConfig, HooksSaveRequest
from src.models.identity import AgentIdentity
from src.models.sliding_window import SlidingWindowConfig
from src.routers import autonomy as autonomy_router
from src.routers import creation as creation_router
from src.routers import hooks as hooks_router
from src.routers import mcp as mcp_router
from src.routers import system as system_router
from src.routers import ui_sessions as ui_sessions_router
from src.services.hooks.lifecycle import ConfigSection
from src.utils import async_db

from tests.support.hook_wiring import group, install_hooks, until

if TYPE_CHECKING:
    from tests.support.hook_wiring import HookSetup

_ADMIN = AgentIdentity.for_role(agent_id="zz-config-admin", role="admin", is_human=True)

# 會改狀態、但不是系統設定的路由；分類和理由都要明寫，新端點沒分類就紅。
_NOT_SETTINGS: dict[str, set[str]] = {
    "帳號與登入（不含帳號是刻意的，ADR-0292 差別表）": {
        "admin.create_user", "admin.update_user", "admin.delete_user", "auth.register",
        "auth.login", "auth.refresh", "auth.logout", "auth.change_password", "auth.update_me",
        "auth.upload_avatar", "auth.remove_avatar", "auth.clear_my_data", "auth.export_my_data",
    },
    "聊天、對話與專案資料": {
        "proxy.proxy_anthropic_messages", "proxy.proxy_chat_completions",
        "chat_upload.upload_chat_file", "ui_sessions.set_home_session",
        "ui_sessions.save_session", "ui_sessions.stop_session_tasks",
        "ui_sessions.delete_session", "ui_sessions.export_legacy", "ui_sessions.save_project",
        "ui_sessions.delete_project", "ui_interjections.add_interjection",
        "ui_interjections.retract_interjection", "feedback.submit_feedback",
        "stats.trigger_legacy_sync",
    },
    "審批、回答與派工回呼（一次性的動作）": {
        "ask_user.submit_answer", "terminal.approve_command", "terminal.reject_command",
        "tools_approval.approve_tool_call", "tools_approval.reject_tool_call",
        "tools_approval.cancel_tool_interaction", "dispatch_callback.receive_dispatch_callback",
    },
    "靈魂的資料與排程，不是系統設定": {
        "souls.create_soul", "souls.update_soul", "souls.delete_soul",
        "admin.reset_agent", "heartbeat.upsert_human_heartbeat_schedule",
        "heartbeat.delete_human_heartbeat_schedule", "heartbeat.awaken_agent",
        "heartbeat.retrigger_heartbeat",
    },
    "備份檔案本身與唯讀匯出": {
        "system.run_backup", "system.delete_backup", "system.download_backup",
        "system.export_settings",
    },
    "讀取或重新整理，不改設定": {
        "mcp.restart_mcp_server", "mcp.read_server_resource", "mcp.get_server_prompt",
        "mcp.sync_mcp_hub", "models.refresh_models", "models.check_model_health",
        "setup.download_embedding_model",
    },
    "除錯工具": {"debug.debug_execute_tool", "debug.debug_reset", "debug.scorecard_run"},
    "本機小幫手的配對與通道（配對是連線憑證，不是設定值，ADR-0312）": {
        "local_bridge.create_pairing", "local_bridge.delete_pairing", "local_bridge.poll",
        "local_bridge.confirm_job", "local_bridge.submit_result",
    },
    "MCP OAuth 的連線與憑證（有自己的稽核紀錄，這批不接，ADR-0292 差別表）": {
        "mcp_oauth.start_mcp_oauth", "mcp_oauth.disconnect_mcp_oauth",
        "mcp_oauth.complete_mcp_oauth_form_post", "mcp_oauth.put_mcp_oauth_profile",
        "mcp_oauth.delete_mcp_oauth_profile", "mcp_oauth.start_mcp_oauth_key_rotation",
        "mcp_oauth_workload.put_workload_profile", "mcp_oauth_workload.delete_workload_profile",
        "mcp_oauth_workload.set_workload_credential",
        "mcp_oauth_workload.revoke_workload_credential",
        "mcp_oauth_workload.refresh_workload_metadata",
        "mcp_oauth_workload.retry_workload_recovery",
        "mcp_oauth_workload.reconcile_workload_transition",
        "mcp_oauth_workload.start_workload_key_rotation",
        "mcp_oauth_workload.put_owner_delegation", "mcp_oauth_workload.delete_owner_delegation",
    },
}  # fmt: skip
_NOT_SETTING_NAMES = set().union(*_NOT_SETTINGS.values())
_CALL = re.compile(r"""notify_config_change\(\s*"([a-z_]+)"\s*\)""")


def _state_changing_routes() -> dict[str, Any]:
    routes: dict[str, Any] = {}
    for route in app.routes:
        if isinstance(route, APIRoute) and route.methods & {"POST", "PUT", "PATCH", "DELETE"}:
            module = route.endpoint.__module__.removeprefix("src.routers.")
            routes[f"{module}.{route.endpoint.__name__}"] = route.endpoint
    return routes


def test_every_state_changing_route_is_classified():
    routes = _state_changing_routes()
    assert len(routes) > 90  # 列舉真的抓到路由
    assert set(routes) >= _NOT_SETTING_NAMES, _NOT_SETTING_NAMES - set(routes)
    for name, endpoint in routes.items():
        source = inspect.getsource(endpoint)
        if name in _NOT_SETTING_NAMES:
            assert "notify_config_change" not in source, name
        else:
            assert _CALL.search(source), f"{name} 沒分類：存檔後通知，或列進不是設定並寫理由"


def test_every_notifying_route_can_start_a_background_hook():
    """背景入口要有事件迴圈：同步的路由跑在執行緒裡，通知會默默消失。"""
    for name, endpoint in _state_changing_routes().items():
        if name not in _NOT_SETTING_NAMES:
            assert inspect.iscoroutinefunction(endpoint), name


def test_sections_are_the_declared_ones_and_all_are_used():
    used = {
        section
        for name, endpoint in _state_changing_routes().items()
        if name not in _NOT_SETTING_NAMES
        for section in _CALL.findall(inspect.getsource(endpoint))
    }
    assert used == set(get_args(ConfigSection))


@pytest.fixture
def hooks(tmp_path, monkeypatch) -> HookSetup:
    setup = install_hooks(tmp_path, monkeypatch)
    setup.configure({"ConfigChange": [group()]})
    return setup


def _autonomy_payload() -> AutonomyConfigModel:
    return AutonomyConfigModel.model_validate(autonomy_router.autonomy_service.config)


async def test_a_failed_save_fires_nothing(hooks, monkeypatch):
    monkeypatch.setattr(autonomy_router.autonomy_service, "update_config", lambda _c: False)
    with pytest.raises(ToukaException):
        await autonomy_router.update_autonomy_config(_autonomy_payload(), _ADMIN)
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []


async def test_a_save_fires_after_it_succeeded(hooks, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        autonomy_router.autonomy_service,
        "update_config",
        lambda _c: order.append(f"saved:{len(hooks.http.calls)}") or True,
    )
    await autonomy_router.update_autonomy_config(_autonomy_payload(), _ADMIN)
    await until(lambda: hooks.http.calls)
    assert order == ["saved:0"]
    assert hooks.http.calls[0]["source"] == "autonomy"


async def test_a_hook_that_answers_block_changes_nothing(hooks, monkeypatch):
    saved: list[Any] = []
    hooks.http.bodies["ConfigChange"] = '{"decision": "block", "reason": "no"}'
    monkeypatch.setattr(
        autonomy_router.autonomy_service, "update_config", lambda c: saved.append(c) or True
    )
    result = await autonomy_router.update_autonomy_config(_autonomy_payload(), _ADMIN)
    await until(lambda: hooks.http.calls)
    assert result["status"] == "success"
    assert saved


async def test_a_stuck_hook_does_not_hold_the_response(hooks, monkeypatch):
    gate = asyncio.Event()
    hooks.http.gates["ConfigChange"] = gate
    monkeypatch.setattr(autonomy_router.autonomy_service, "update_config", lambda _c: True)
    try:
        await asyncio.wait_for(
            autonomy_router.update_autonomy_config(_autonomy_payload(), _ADMIN), 5
        )
    finally:
        gate.set()


async def _save_hooks(hooks: HookSetup, monkeypatch, *, enabled: bool) -> None:
    monkeypatch.setattr(hooks_router, "hook_service", hooks.service)
    monkeypatch.setattr(hooks_router, "verify_origin", lambda _request: None)
    config = HooksConfig.model_validate({"enabled": enabled, "hooks": {"ConfigChange": [group()]}})
    payload = HooksSaveRequest(
        config=config, revision=hashlib.sha256(hooks.path.read_bytes()).hexdigest()
    )
    await hooks_router.save_hooks_config(payload, MagicMock(), _ADMIN)


async def test_saving_the_hooks_page_stays_saved_when_the_hook_answers_block(hooks, monkeypatch):
    hooks.http.bodies["ConfigChange"] = '{"decision": "block", "reason": "no"}'
    before = hooks.path.read_bytes()
    await _save_hooks(hooks, monkeypatch, enabled=True)
    await until(lambda: hooks.http.calls)
    assert hooks.http.calls[0]["source"] == "hooks"
    assert hooks.path.read_bytes() != before
    assert hooks.service.has_hooks("ConfigChange")


async def test_turning_hooks_off_works_and_fires_nothing(hooks, monkeypatch):
    """關閉 hooks 是人要停掉壞掉的 hook 的路，不能被 hook 卡住，也不再通知。"""
    hooks.http.gates["ConfigChange"] = asyncio.Event()
    await asyncio.wait_for(_save_hooks(hooks, monkeypatch, enabled=False), 5)
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []
    assert not hooks.service.has_hooks("ConfigChange")


async def test_sliding_window_failure_fires_nothing(hooks, monkeypatch):
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError

    monkeypatch.setattr(system_router.resource_manager, "write_json", boom)
    with pytest.raises(ToukaException):
        await system_router.update_sliding_window_config(SlidingWindowConfig(enabled=True), _ADMIN)
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []


async def test_a_settings_import_preview_fires_nothing(hooks, monkeypatch):
    monkeypatch.setattr(system_router, "verify_origin", lambda _request: None)
    preview = MagicMock(return_value={"applied": ["autonomy"]})
    monkeypatch.setattr(system_router, "apply_snapshot", preview)
    request = MagicMock(dry_run=True, snapshot={}, mode="merge", sections=None)
    await system_router.import_settings(request, MagicMock(), MagicMock(role="admin"))
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []


async def test_a_hub_entry_that_could_not_be_installed_fires_nothing(hooks, monkeypatch):
    hub = MagicMock(install=AsyncMock(return_value={"status": "invalid_entry", "hub_id": "x"}))
    monkeypatch.setattr(mcp_router, "MCPHubService", lambda **_kwargs: hub)
    response = await mcp_router.install_mcp_from_hub(
        MagicMock(hub_id="x", env_vars={}), MagicMock(is_owner_or_admin=lambda: True)
    )
    assert response.status_code == 422
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []


async def test_a_workspace_setting_that_was_not_written_fires_nothing(hooks, monkeypatch):
    monkeypatch.setattr(ui_sessions_router, "set_system_setting", AsyncMock(return_value=False))
    payload = ui_sessions_router.WorkspaceSettings(workspace_root="x")
    with pytest.raises(ToukaException) as caught:
        await ui_sessions_router.update_workspace_settings(payload, MagicMock())
    assert caught.value.code is ErrorCode.CONFIG_SAVE_FAILED
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []


async def test_a_system_setting_that_can_not_be_written_says_so(monkeypatch):
    monkeypatch.setattr(async_db, "get_async_db", MagicMock(side_effect=OSError))
    assert await async_db.set_system_setting("k", "v") is False


@pytest.mark.parametrize("committed", [True, False])
async def test_creating_from_the_settings_page_fires_only_when_committed(
    hooks, monkeypatch, committed
):
    result = MagicMock(committed=committed)
    monkeypatch.setattr(
        creation_router.creation_api, "create_skill", AsyncMock(return_value=result)
    )
    monkeypatch.setattr(creation_router, "_to_response", lambda res: res)
    request = SimpleNamespace(
        id="s1", name="s1", description="d", category="general", author="a", is_system=False,
        system_prompt="p", required_tools=[], mcp_servers=[], run_smoke=False, run_eval=False,
        eval_scoring=None,
    )  # fmt: skip
    await creation_router.create_skill_endpoint(request, _ADMIN)
    await asyncio.sleep(0.05)
    assert bool(hooks.http.calls) is committed
