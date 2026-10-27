"""E66（ADR-0176）：vault-jailed MCP 咽喉注入的執行器接線——行為級。

ADR-0115 #1 鐵則：檔案型 MCP 存取**永不跨 agent**，vault 根由驗證身分決定、模型不可指定。
唯一咽喉＝tool_executor MCP 派發分支的 ``mcp_vault_jail.apply(...)``。既有 test_mcp_vault_jail.py
只直呼 apply() 純函式；所有真跑 execute_to_queue 的測試 protocol_manager=None（MCP 分支永不
進入）——刪掉咽喉三行全綠＝fails open 跨 agent vault 讀。本檔經真 ToolExecutor.execute_to_queue
＋fake ProtocolManager 釘死：模型給的根目錄被蓋掉、agent_id 被 pop、解不出 vault 時 fail-closed
零派發；非受管伺服器原樣通過（陽性對照）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from src.models.identity import AgentIdentity
from src.models.policy import PolicyDecision
from src.protocols.mcp.canonical import McpCompleteOutcome
from src.protocols.mcp.sdk_adapter import UNKNOWN_PROTOCOL_VERSION
from src.services import tool_errors
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.services.tool_executor import ToolExecutor

if TYPE_CHECKING:
    from src.protocols.mcp.canonical import McpRequestContext

_IDENTITY = AgentIdentity.for_role(agent_id="JailProbe", role="Owner", is_human=False)


class _FakeRegistry:
    def __init__(self, servers: dict[str, Any]) -> None:
        self._servers = servers

    def get_server(self, name: str) -> Any:
        return self._servers.get(name)


class _FakeProtocolManager:
    """resolve_tool 固定命中 MCP 伺服器；call_tool_on_server 錄下實收 args。"""

    def __init__(self, server_id: str, cfg: Any) -> None:
        self._server_id = server_id
        self.registry = _FakeRegistry({server_id: cfg})
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def resolve_tool(self, name: str) -> tuple[str | None, str]:
        return self._server_id, name

    async def call_tool_on_server(
        self,
        server_id: str,
        tool: str,
        args: dict[str, Any],
        *,
        request_context: McpRequestContext,
    ) -> McpCompleteOutcome:
        self.calls.append((server_id, tool, dict(args)))
        return McpCompleteOutcome(
            source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
            content=[{"type": "text", "text": "ok"}],
        )


def _allow_policy(monkeypatch) -> None:
    # 政策閘在咽喉上游、非本檔標的——固定 ALLOW（同 test_hitl_broker_executor 慣例）。
    monkeypatch.setattr(
        permission_service,
        "evaluate_policy",
        lambda _ctx: PolicyDecision(effect="ALLOW", reason="test-allow"),
    )


async def _dispatch(pm: _FakeProtocolManager, args: dict[str, Any]) -> str:
    executor = ToolExecutor(
        protocol_manager=pm,
        tool_registry={},
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )
    queue: asyncio.Queue = asyncio.Queue()
    return await executor.execute_to_queue(
        "docgen_read",
        args,
        "req-jail",
        {"identity": _IDENTITY, "auth_level": "Manual", "session_id": None},
        queue,
    )


async def test_model_supplied_root_is_overridden_to_own_vault(monkeypatch):
    """模型供 base_dir='/etc' → 實際派發收到「當下 agent 自己的 vault 根」且 agent_id 被 pop。"""
    _allow_policy(monkeypatch)
    pm = _FakeProtocolManager("mcp-docgen", SimpleNamespace(vault_jail_arg="base_dir"))

    await _dispatch(pm, {"base_dir": "/etc", "file_ref": "outputs/uploads/x.pdf"})

    assert len(pm.calls) == 1
    server_id, _tool, sent = pm.calls[0]
    assert server_id == "mcp-docgen"
    own_vault = resource_manager.get_vault_path("personal", _IDENTITY)
    assert sent["base_dir"] == own_vault  # 覆寫成自己 vault（模型給的 /etc 被蓋掉）
    assert sent["base_dir"] != "/etc"
    assert "agent_id" not in sent  # 外部 MCP 未宣告的注入引數已移除


async def test_fail_closed_zero_dispatch_when_vault_unresolvable(monkeypatch):
    """vault-jailed 但解不出 vault 根 → fail-closed：錯誤結果、call_tool_on_server 零呼叫。"""
    _allow_policy(monkeypatch)
    monkeypatch.setattr(resource_manager, "get_vault_path", lambda _v, _i: None)
    pm = _FakeProtocolManager("mcp-docgen", SimpleNamespace(vault_jail_arg="base_dir"))

    result = await _dispatch(pm, {"base_dir": "/etc"})

    assert pm.calls == []  # 絕不讓模型/預設根目錄生效
    assert tool_errors.is_tool_error(result)


async def test_non_jailed_server_keeps_model_args(monkeypatch):
    """無 vault_jail_arg 的伺服器不受管：模型 args 原樣通過（陽性對照＝jail 是選擇性的）。"""
    _allow_policy(monkeypatch)
    pm = _FakeProtocolManager("mcp-fetch", SimpleNamespace(vault_jail_arg=None))

    await _dispatch(pm, {"url": "https://example.com"})

    assert len(pm.calls) == 1
    _sid, _tool, sent = pm.calls[0]
    assert sent["url"] == "https://example.com"
    assert sent["agent_id"] == "JailProbe"  # 派發層注入的 agent_id 保留（jail 路徑才 pop）
