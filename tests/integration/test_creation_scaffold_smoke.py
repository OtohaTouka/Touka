"""ADR-0069 Phase C — SCAFFOLD 整合：生成 stub → 真啟動 → 列出工具 → 走完整流水線。

不 mock：scaffold 生成 MCPServer v2 stub，經 ``smoke_mcp_server`` 以 ``sys.executable`` 真實
啟動、握手、tools/list；再走完整 ``create('mcp', spec)``（VALIDATE→SMOKE→COMMIT→EVAL）。
本地、無網路、跨平台（僅需已安裝的 ``mcp`` 套件）；用既有 ``_stub_mcp_server.py`` 同形態。
"""
from __future__ import annotations

from src.config import get_config_dir
from src.services.creation import creation_engine
from src.services.creation.kinds import launch_context
from src.services.creation.models import McpScaffoldSpec, ParamSpec, ToolSpec
from src.services.creation.scaffold import mcp_scaffolder
from src.services.creation.smoke import smoke_mcp_server
from src.services.resource_manager import resource_manager


async def test_scaffold_then_smoke_starts_and_lists_tools():
    spec = McpScaffoldSpec(
        name="scaffold-smoke-srv",
        tools=[
            ToolSpec(name="ping", description="Return pong."),
            ToolSpec(name="echo", description="Echo text.", params=[ParamSpec(name="text")]),
        ],
    )
    res = mcp_scaffolder.scaffold(spec)
    assert res.ok is True, res.errors
    report = await smoke_mcp_server(
        res.spec.command, res.spec.args, res.spec.env, timeout=30.0,
        name=res.spec.name, context=await launch_context("scaffold"),
    )
    assert report.ok is True, report.detail
    assert report.tool_count == 2


async def test_scaffold_then_engine_create_full_pipeline():
    spec = McpScaffoldSpec(
        name="scaffold-create-srv",
        tools=[
            ToolSpec(name="ping", description="Return pong."),
            ToolSpec(name="status", description="Return status."),
        ],
    )
    res = mcp_scaffolder.scaffold(spec)
    assert res.ok is True, res.errors
    out = await creation_engine.create(
        "mcp", res.spec, run_eval=True, context=await launch_context("scaffold"),
    )
    assert out.committed is True
    assert out.smoke is not None and out.smoke.tool_count == 2
    data = resource_manager.read_json(get_config_dir() / "server_config.json")
    assert "scaffold-create-srv" in data["mcpServers"]
    assert out.evaluation is not None and out.evaluation.ok is True
    assert len(out.evaluation.tasks) == 2
    assert resource_manager.exists(
        get_config_dir() / "evals" / "mcp" / "scaffold-create-srv.eval.md"
    )
