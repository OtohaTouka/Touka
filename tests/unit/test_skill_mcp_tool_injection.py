"""Batch 4（ADR-0102）：skill→MCP→工具 正向鏈整合測試。

當技能所需的 MCP server 已註冊＋可連線，ToolLayer 應把該 MCP 的工具注入 request.tools
——這正是「命名對得上」時該發生、docgen 命名不符時斷掉的那一環。負向（未註冊 → 計分卡
precheck 略過）已由 test_scorecard_runner_context 覆蓋；此處補正向。
"""
from __future__ import annotations

import pytest
from src.adapters.schemas import AnthropicRequest
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity

_OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])
_DOCGEN_TOOL = {
    "server": "mcp-docgen",
    "type": "function",
    "function": {"name": "mcp-docgen__create_xlsx", "description": "x", "parameters": {}},
}


class _FakeRegistry:
    """假 protocol registry：只認得 mcp-docgen。"""

    def get_server(self, name: str):
        """已註冊回 truthy，未註冊回 None。"""
        return {"command": "uvx"} if name == "mcp-docgen" else None


class _FakePM:
    """假 ProtocolManager：mcp-docgen 已連線並提供 create_xlsx 工具。"""

    registry = _FakeRegistry()

    async def ensure_connected(self, _name: str) -> None:
        """連線 no-op。"""

    async def get_all_tools(self) -> list[dict]:
        """回傳 docgen 的 MCP 工具（前綴命名 server__tool）。"""
        return [_DOCGEN_TOOL]


@pytest.mark.asyncio
async def test_toollayer_injects_mcp_tool_when_skill_requires_registered_server():
    """技能要求的 MCP 已註冊 → ToolLayer 把其工具注入 request.tools（正向鏈）。"""
    ctx = PipelineContext(identity=_OWNER)
    ctx["agent_id"] = "DebugAgent"
    ctx["skill_required_mcp"] = ["mcp-docgen"]
    ctx["protocol_manager"] = _FakePM()
    req = AnthropicRequest(
        model="deepseek-v4-flash",
        max_tokens=512,
        messages=[{"role": "user", "content": "做 Excel"}],
    )
    await ToolLayer().process(req, ctx)
    names = {t.get("function", {}).get("name") for t in (req.tools or [])}
    assert "mcp-docgen__create_xlsx" in names
