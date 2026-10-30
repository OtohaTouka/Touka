"""TDD: mission_tools 新 action 測試（ADR-0051 雙向回呼通道）.

驗證：
  1. initialize + notify_on_complete=True 時，回傳含 callback_token 欄位的訊息
  2. await_dispatch_result action 在 token 被 deliver 後解除阻擋並回傳結果
  3. notify_on_complete=False（預設）時，不在 callback store 中建立 token
"""

from __future__ import annotations

import asyncio

import pytest
from memory_server import config_utils
from src.models.identity import AgentIdentity
from src.services.dispatch_callback_store import _reset_callback_store, get_callback_store
from src.tools.mission_tools import mission_tools


def _make_identity(name: str = "Alice", role: str = "Owner") -> AgentIdentity:
    """建立測試用身分實體（SSOT）。"""
    return AgentIdentity.for_role(
        agent_id=name,
        role=role,
        capabilities=["heartbeat_awakening", "can_supervise_dispatch"],
    )


def _reset():
    """重置相關快取（data 根由 ``zone_data_root`` 換，ADR-0293 T3）。"""
    config_utils._reset_resource_proxy_cache()
    _reset_callback_store()


class TestMissionToolsNotify:
    """mission_tools 新增 notify_on_complete / await_dispatch_result 行為驗證。"""

    @pytest.mark.asyncio
    async def test_initialize_with_notify_returns_callback_token(self, zone_data_root, monkeypatch):
        """notify_on_complete=True 時，回傳訊息應包含各 task 的 callback_token。"""
        _reset()
        identity = _make_identity()

        result = await mission_tools(
            action="initialize",
            identity=identity,
            target_agent_id="DispatchAgent",
            goal="Test dispatch mission",
            tasks=[
                {"id": "t1", "description": "Task one"},
                {"id": "t2", "description": "Task two"},
            ],
            notify_on_complete=True,
        )

        # 回傳字串必須包含 callback_token 提示
        assert "callback_token" in result.lower() or "callback" in result.lower(), (
            f"notify_on_complete=True 應回傳含 callback token 的訊息，實際：{result}"
        )
        # 同時 store 裡應有已注冊的 token
        store = get_callback_store()
        assert any(
            store.is_registered(tok)
            for tok in result.split()
            if len(tok) >= 30  # token 至少 30 字元
        ) or store._pending, "callback store 應有已注冊的 pending token"

    @pytest.mark.asyncio
    async def test_await_dispatch_result_action_returns_result(self, zone_data_root, monkeypatch):
        """await_dispatch_result action 在 deliver_result 後應解除阻擋並回傳結果。"""
        _reset()
        identity = _make_identity()

        store = get_callback_store()
        # 手動建立 token（模擬 initialize 的 notify 流程）
        token = store.generate_token()
        store.register(token)

        # 非同步交付結果
        async def deliver():
            await asyncio.sleep(0.05)
            store.deliver_result(
                token,
                {"status": "DONE", "task_id": "t1", "result_summary": "All tests passed"},
            )

        asyncio.create_task(deliver())

        result = await mission_tools(
            action="await_dispatch_result",
            identity=identity,
            callback_token=token,
            timeout=2.0,
        )

        assert "DONE" in result, f"應包含 DONE 狀態，實際：{result}"
        assert "t1" in result, f"應包含 task_id，實際：{result}"

    @pytest.mark.asyncio
    async def test_notify_false_does_not_register_token(self, zone_data_root, monkeypatch):
        """notify_on_complete=False（預設）時，callback store 不應有任何新 token。"""
        _reset()
        identity = _make_identity()

        store = get_callback_store()
        before_count = len(store._pending)

        await mission_tools(
            action="initialize",
            identity=identity,
            target_agent_id="DispatchAgent",
            goal="Simple mission without notify",
            tasks=[{"id": "t1", "description": "Task one"}],
            notify_on_complete=False,
        )

        after_count = len(store._pending)
        assert after_count == before_count, (
            f"notify_on_complete=False 不應在 callback store 中新增 token；"
            f"before={before_count}, after={after_count}"
        )
