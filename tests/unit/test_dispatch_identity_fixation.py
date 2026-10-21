"""E04（早期守衛缺口 backlog）— operation tools 消費身分固定 DispatchAgent 行為測試。

缺口背景（docs/architecture/proposals/early-adr-guard-coverage-gap-backlog.md · E04）：
    ADR-0047 的核心不變式——operation tools（initialize/update/get_board）交給
    MissionManager 做授權與板路徑構造的身分物件，永遠是固定非特權 DispatchAgent、
    絕不由 caller 提供的輸入衍生（Vuln 1 身分偽冒／Vuln 2 路徑遍歷的根治）。
    既有執法皆為間接：AST 參數檢查（無 agent_id 參數）與「能初始化＋能讀板」
    煙囪測試（test_dispatch_server_tools.py），從未捕捉「實際消費的身分物件」
    斷言其欄位——若 identity 改由 caller_info 衍生，全部既有測試照綠。

本檔以 spy MissionManager 攔截消費點，行為級鎖住：
  1. 三個 operation tools 交付的 identity 全為 agent_id="DispatchAgent"、
     非特權（is_privileged()=False）、agent 面 Base、無人類權威、零 capabilities
  2. caller_name 宣稱高權限名（Owner/DebugAgent）不影響消費身分（身分固定）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ToukaDispatch import server as ds

_CAPTURED: list[tuple[str, Any]] = []


class _SpyManager:
    """MissionManager 替身：只捕捉 identity 引數，不碰檔案系統。"""

    async def initialize_mission(
        self, identity: Any = None, goal: str = "", initial_tasks: Any = None, **kw: Any
    ) -> Any:
        _CAPTURED.append(("initialize_mission", identity))
        return SimpleNamespace(mission_id="m-e04", tasks=[])

    async def get_current_mission(self, identity: Any = None, target_agent_id: Any = None) -> Any:
        _CAPTURED.append(("get_current_mission", identity))
        return None

    async def update_task(self, identity: Any = None, **kw: Any) -> Any:
        _CAPTURED.append(("update_task", identity))
        return None


async def _handshake(ds: Any, caller_name: str) -> str:
    result = await ds.dispatch_handshake(
        dispatch_token="dispatch-e04-token", caller_name=caller_name
    )
    assert "Session token:" in result, f"Handshake failed: {result}"
    return result.split("Session token:")[-1].strip()


@pytest.fixture
def dispatch_server(monkeypatch):
    """裝上 spy MissionManager 的 dispatch server 模組（含有效 DISPATCH_TOKEN）。

    DISPATCH_TOKEN 於 _verify_dispatch_token 呼叫時才讀取（非 import 時），故
    fixture 設定 env + patch MissionManager 即足；server 模組於檔頂匯入。
    """
    monkeypatch.setenv("DISPATCH_TOKEN", "dispatch-e04-token")
    monkeypatch.setattr(ds, "MissionManager", _SpyManager)
    _CAPTURED.clear()
    return ds


def _assert_fixed_dispatch_identity(identity: Any, consumer: str) -> None:
    """消費身分不變式：固定 DispatchAgent、非特權、agent 面 Base、零能力。"""
    assert identity is not None, f"{consumer} 未交付 identity"
    assert identity.agent_id == "DispatchAgent", (
        f"{consumer} 消費的身分是 {identity.agent_id!r}——身分不再固定 DispatchAgent"
        "（Vuln 1 身分偽冒重開，見 ADR-0047／E04）"
    )
    assert identity.is_privileged() is False
    assert identity.human_role is None
    assert identity.agent_role == "Base"
    assert "can_supervise_dispatch" not in list(identity.capabilities)


@pytest.mark.asyncio
async def test_all_operation_tools_consume_fixed_dispatch_identity(dispatch_server):
    """三個 operation tools 交給 MissionManager 的身分全為固定 DispatchAgent。"""
    ds = dispatch_server
    session_token = await _handshake(ds, caller_name="TestTool")

    await ds.dispatch_initialize_mission(session_token=session_token, goal="E04 goal")
    await ds.dispatch_update_task(session_token=session_token, task_id="t1", status="DOING")
    await ds.dispatch_get_board(session_token=session_token)

    consumers = [c for c, _ in _CAPTURED]
    assert "initialize_mission" in consumers
    assert "update_task" in consumers
    assert "get_current_mission" in consumers

    for consumer, identity in _CAPTURED:
        _assert_fixed_dispatch_identity(identity, consumer)


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile_caller", ["Owner", "DebugAgent"])
async def test_caller_name_never_derives_consumed_identity(dispatch_server, hostile_caller):
    """caller_name 宣稱高權限名，operation tools 消費的身分仍固定 DispatchAgent。"""
    ds = dispatch_server
    session_token = await _handshake(ds, caller_name=hostile_caller)

    await ds.dispatch_initialize_mission(
        session_token=session_token, goal=f"identity fixation vs {hostile_caller}"
    )
    await ds.dispatch_get_board(session_token=session_token)

    assert _CAPTURED, "operation tools 未觸及 MissionManager 消費點——場景失效"
    for consumer, identity in _CAPTURED:
        _assert_fixed_dispatch_identity(identity, consumer)
        assert identity.agent_id != hostile_caller
