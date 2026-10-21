"""ADR-0061：ToolLayer skill_required_mcp 連線與工具可見性測試 (TDD).

驗證 ToolLayer 從 ctx["skill_required_mcp"] 讀取技能所需 MCP 伺服器清單後：
  1. 對每個已授權且存在於 registry 的 server 呼叫 ensure_connected()
  2. 未授權或不存在的 server 被靜默跳過
  3. Dispatcher 模式下，技能所需 server 的工具仍然可見
     （修復前：dispatcher 只顯示 active_session_mcps 中的 server 工具）
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.utils.permission_grants import NameGrant

# ---------------------------------------------------------------------------
# 測試輔助
# ---------------------------------------------------------------------------

_BASE_IDENTITY = AgentIdentity.for_role(agent_id="TestAgent", role="Base")


def _make_pm(
    server_in_registry: bool = True,
    get_all_tools_result: list[dict] | None = None,
) -> AsyncMock:
    mock_pm = AsyncMock()
    mock_pm.get_all_tools = AsyncMock(return_value=get_all_tools_result or [])
    mock_pm.ensure_connected = AsyncMock()
    mock_registry = MagicMock()
    mock_registry.get_server.return_value = MagicMock() if server_in_registry else None
    mock_pm.registry = mock_registry
    return mock_pm


def _make_ctx(
    skill_required_mcp: list[str] | None = None,
    active_skills: list[str] | None = None,
    identity: AgentIdentity = _BASE_IDENTITY,
) -> PipelineContext:
    ctx = PipelineContext(identity=identity)
    ctx["agent_id"] = identity.agent_id
    ctx["is_custom"] = False
    ctx["is_toukasoul"] = False
    ctx["headers"] = {}
    # session_id 不設定 → 跳過 session_service lazy import
    if skill_required_mcp is not None:
        ctx["skill_required_mcp"] = skill_required_mcp
    if active_skills is not None:
        ctx["active_skills"] = active_skills
    return ctx


def _make_request() -> MagicMock:
    req = MagicMock()
    req.tools = []
    req.project_id = None
    return req


def _setup_perm_deny_builtins(mock_perm: MagicMock) -> None:
    """讓所有 builtin 工具被 DENY，MCP RBAC 預設 True，可在測試中覆蓋。"""
    mock_perm.get_allowed_tools.return_value = NameGrant()
    mock_perm.evaluate_policy.return_value = MagicMock(effect="DENY")
    mock_perm.is_tool_allowed.return_value = False
    mock_perm.is_mcp_server_allowed.return_value = True
    # ADR-0195：廣告面的工具可見性改由 `is_mcp_tool_allowed`（軸 B，工具粒度）把關；
    # 掛載決策（ensure_connected）仍走 `is_mcp_server_allowed`。**兩者必須分別設定**——
    # 漏設會讓 MagicMock 回傳真值物件，未授權工具靜默外洩而測試照樣綠。
    mock_perm.is_mcp_tool_allowed.return_value = True


# ---------------------------------------------------------------------------
# 測試案例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("src.middleware.layers.tool.permission_service")
async def test_skill_mcp_servers_trigger_ensure_connected(mock_perm: MagicMock) -> None:
    """RBAC 通過 + server 在 registry → ensure_connected 被 await。"""
    _setup_perm_deny_builtins(mock_perm)

    mock_pm = _make_pm(server_in_registry=True)
    ctx = _make_ctx(skill_required_mcp=["srv-alpha"])
    ctx["protocol_manager"] = mock_pm

    await ToolLayer().process(_make_request(), ctx)

    mock_pm.ensure_connected.assert_awaited_once_with("srv-alpha")


@pytest.mark.asyncio
@patch("src.middleware.layers.tool.permission_service")
async def test_skill_mcp_rbac_denied_server_not_connected(mock_perm: MagicMock) -> None:
    """is_mcp_server_allowed 回傳 False → ensure_connected 不被呼叫。"""
    _setup_perm_deny_builtins(mock_perm)
    mock_perm.is_mcp_server_allowed.return_value = False

    mock_pm = _make_pm(server_in_registry=True)
    ctx = _make_ctx(skill_required_mcp=["srv-forbidden"])
    ctx["protocol_manager"] = mock_pm

    await ToolLayer().process(_make_request(), ctx)

    mock_pm.ensure_connected.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.middleware.layers.tool.permission_service")
async def test_skill_mcp_unknown_server_not_in_registry_skipped(mock_perm: MagicMock) -> None:
    """registry.get_server() 回傳 None（未知 server）→ ensure_connected 不被呼叫。"""
    _setup_perm_deny_builtins(mock_perm)

    mock_pm = _make_pm(server_in_registry=False)
    ctx = _make_ctx(skill_required_mcp=["phantom-server"])
    ctx["protocol_manager"] = mock_pm

    await ToolLayer().process(_make_request(), ctx)

    mock_pm.ensure_connected.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.middleware.layers.tool.permission_service")
async def test_skill_mcp_tools_visible_in_dispatcher_mode(mock_perm: MagicMock) -> None:
    """Dispatcher 模式下，skill_required_mcp 的 server 工具必須可見。

    修復前：dispatcher 只顯示 active_session_mcps 中的工具，
    skill_required_mcp 的 server 因不在 active_session_mcps 中而被隱藏。
    修復後：active_session_mcps OR skill_required_mcp 均可見。
    """
    _setup_perm_deny_builtins(mock_perm)

    _skill_tool: dict[str, Any] = {
        "server": "srv-skill",
        "type": "function",
        "function": {"name": "skill_tool", "description": "a skill tool", "parameters": {}},
    }
    mock_pm = _make_pm(server_in_registry=True, get_all_tools_result=[_skill_tool])
    ctx = _make_ctx(
        skill_required_mcp=["srv-skill"],
        active_skills=["system-mcp-dispatcher"],
        # session_id 未設定 → active_session_mcps = []（不含 srv-skill）
    )
    ctx["protocol_manager"] = mock_pm

    request = _make_request()
    await ToolLayer().process(request, ctx)

    injected_names = [
        t.get("function", {}).get("name")
        for t in request.tools
        if isinstance(t, dict)
    ]
    assert "skill_tool" in injected_names, (
        "Dispatcher 模式下，skill_required_mcp server 的工具應可見，"
        "即使該 server 不在 active_session_mcps 中"
    )


@pytest.mark.asyncio
@patch("src.middleware.layers.tool.permission_service")
async def test_no_skill_required_mcp_in_ctx_ensure_connected_not_called(
    mock_perm: MagicMock,
) -> None:
    """ctx 中沒有 skill_required_mcp key → ensure_connected 完全不被觸發。"""
    _setup_perm_deny_builtins(mock_perm)

    mock_pm = _make_pm()
    ctx = _make_ctx()  # 不設定 skill_required_mcp
    ctx["protocol_manager"] = mock_pm

    await ToolLayer().process(_make_request(), ctx)

    mock_pm.ensure_connected.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.middleware.layers.tool.permission_service")
async def test_multiple_skill_mcp_servers_all_authorized_all_connected(
    mock_perm: MagicMock,
) -> None:
    """多個授權 server 均在 registry → ensure_connected 對每個呼叫一次。"""
    _setup_perm_deny_builtins(mock_perm)

    mock_pm = _make_pm(server_in_registry=True)
    ctx = _make_ctx(skill_required_mcp=["srv-a", "srv-b"])
    ctx["protocol_manager"] = mock_pm

    await ToolLayer().process(_make_request(), ctx)

    assert mock_pm.ensure_connected.await_count == 2
    called_servers = {call.args[0] for call in mock_pm.ensure_connected.await_args_list}
    assert called_servers == {"srv-a", "srv-b"}


# ---------------------------------------------------------------------------
# E31／E32（ADR-0061，批⑤-B）：dispatcher 模式工具可見性的 RBAC deny 與 bounded-union
#
# tool.py:191-195：`elif is_dispatcher_active: if is_authorized and (server in
# active_session_mcps or server in skill_required_mcp): show=True`。既有測試只證明
# 「授權且在 union 內 → 可見」（正路徑）；兩條負路徑無測：
#   E31＝授權子句（`is_authorized and`）——在 union 內但 RBAC 拒絕須隱藏。
#   E32＝union 子句——已授權但不在 active∪skill_required 須隱藏。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("src.middleware.layers.tool.permission_service")
async def test_dispatcher_hides_unauthorized_server_tool(mock_perm: MagicMock) -> None:
    """E31：dispatcher 模式下，server 在 union 內但 RBAC 未授權 → 工具隱藏。

    server 置於 skill_required_mcp（union 條件為真），is_mcp_server_allowed=False——
    唯一擋住它的是 `is_authorized and`。移除該子句 → 未授權工具外洩 → 紅。
    """
    _setup_perm_deny_builtins(mock_perm)
    mock_perm.is_mcp_server_allowed.return_value = False  # RBAC 拒絕（掛載面）
    mock_perm.is_mcp_tool_allowed.return_value = False  # RBAC 拒絕（廣告面，ADR-0195）

    _tool: dict[str, Any] = {
        "server": "srv-forbidden",
        "type": "function",
        "function": {"name": "forbidden_tool", "description": "x", "parameters": {}},
    }
    mock_pm = _make_pm(server_in_registry=True, get_all_tools_result=[_tool])
    ctx = _make_ctx(
        skill_required_mcp=["srv-forbidden"],  # 在 union 內
        active_skills=["system-mcp-dispatcher"],  # dispatcher 啟用
    )
    ctx["protocol_manager"] = mock_pm

    request = _make_request()
    await ToolLayer().process(request, ctx)

    names = [
        t.get("function", {}).get("name") for t in request.tools if isinstance(t, dict)
    ]
    assert "forbidden_tool" not in names, (
        "dispatcher 下 RBAC 未授權的 server 工具不得可見（即使該 server 在 union 內）"
    )


@pytest.mark.asyncio
@patch("src.middleware.layers.tool.permission_service")
async def test_dispatcher_hides_authorized_server_outside_union(mock_perm: MagicMock) -> None:
    """E32：dispatcher 模式下，server 已授權但不在 active∪skill_required → 隱藏。

    bounded-union 排除半：授權不等於可見，必須「授權 AND 在 union 內」。塌成
    `if is_authorized: show=True` → union 外的工具外洩 → 紅。
    """
    _setup_perm_deny_builtins(mock_perm)  # is_mcp_server_allowed 預設 True

    _tool: dict[str, Any] = {
        "server": "srv-orphan",
        "type": "function",
        "function": {"name": "orphan_tool", "description": "x", "parameters": {}},
    }
    mock_pm = _make_pm(server_in_registry=True, get_all_tools_result=[_tool])
    ctx = _make_ctx(
        skill_required_mcp=[],  # 不含 srv-orphan
        active_skills=["system-mcp-dispatcher"],  # dispatcher 啟用；無 session → 空 active
    )
    ctx["protocol_manager"] = mock_pm

    request = _make_request()
    await ToolLayer().process(request, ctx)

    names = [
        t.get("function", {}).get("name") for t in request.tools if isinstance(t, dict)
    ]
    assert "orphan_tool" not in names, (
        "dispatcher 下、授權但不在 active∪skill_required 的 server 工具必須隱藏（排除半）"
    )


# ---------------------------------------------------------------------------
# 批⑤-E E44（ADR-0085）：ask_user headless 廣告閘（tool.py:106）A/B 對釘
# 三處既有回歸防線皆不斷言 ask_user 在/不在 names；反轉/刪閘全綠＝自主 agent
# 被注入 ask_user 卡住等人。
# ---------------------------------------------------------------------------


def _setup_perm_allow_builtins(mock_perm: MagicMock) -> None:
    """讓所有 builtin 工具被 ALLOW（E44 需要正向廣告路徑）。"""
    mock_perm.get_allowed_tools.return_value = NameGrant.from_lists(["*"])
    mock_perm.evaluate_policy.return_value = MagicMock(effect="ALLOW")
    mock_perm.is_tool_allowed.return_value = True
    mock_perm.is_mcp_server_allowed.return_value = True


def _advertised_names(request: Any) -> list[str]:
    return [
        t.get("function", {}).get("name") for t in request.tools if isinstance(t, dict)
    ]


@pytest.mark.asyncio
@patch("src.middleware.layers.tool.permission_service")
async def test_ask_user_hidden_in_headless_context(mock_perm: MagicMock) -> None:
    """A：非互動（無 resumable_stream）→ ask_user 不得被廣告（自主 agent 無人可問）。

    變異反轉 :106 的 `not` 或刪閘 → ask_user 出現在 headless 廣告 → 紅。
    """
    _setup_perm_allow_builtins(mock_perm)
    ctx = _make_ctx()  # 不設 resumable_stream＝heartbeat/SNS 自主情境
    ctx["protocol_manager"] = _make_pm()

    request = _make_request()
    await ToolLayer().process(request, ctx)

    names = _advertised_names(request)
    assert "ask_user" not in names
    # 陽性對照：廣告管線真的跑了（非空砲）——vault_tools 等 god-tools 刻意不廣告，
    # 取現行廣告面工具 edit_soul 為對照
    assert "edit_soul" in names


@pytest.mark.asyncio
@patch("src.middleware.layers.tool.permission_service")
async def test_ask_user_advertised_in_interactive_context(mock_perm: MagicMock) -> None:
    """B：互動（resumable_stream=True，proxy UI 端點）→ ask_user 正常廣告。"""
    _setup_perm_allow_builtins(mock_perm)
    ctx = _make_ctx()
    ctx["resumable_stream"] = True
    ctx["protocol_manager"] = _make_pm()

    request = _make_request()
    await ToolLayer().process(request, ctx)

    assert "ask_user" in _advertised_names(request)
