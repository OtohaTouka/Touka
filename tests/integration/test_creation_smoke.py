"""ADR-0069 Phase A — SMOKE 整合測試：真實啟動 MCP server 並握手。

不 mock：實際以 ``sys.executable`` 啟動 ``_stub_mcp_server.py``（MCPServer，1 工具），
驗證 ``smoke_mcp_server`` 的啟動 → negotiation → tools/list → 保證拆除 全流程。
本地、無網路、跨平台（僅需已安裝的 ``mcp`` 套件）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from src.services.creation.kinds import launch_context
from src.services.creation.smoke import smoke_mcp_server

_STUB = str(Path(__file__).parent / "_stub_mcp_server.py")


async def test_smoke_real_stub_server_passes():
    report = await smoke_mcp_server(
        sys.executable, [_STUB], {}, timeout=30.0,
        context=await launch_context("creation"),
    )
    assert report.ok is True, report.detail
    assert report.tool_count == 1


async def test_smoke_real_stub_server_supports_explicit_modern_discover():
    report = await smoke_mcp_server(
        sys.executable,
        [_STUB],
        {},
        timeout=30.0,
        protocol_mode="2026-07-28",
        context=await launch_context("creation"),
    )
    assert report.ok is True, report.detail
    assert report.tool_count == 1


async def test_smoke_bad_command_fails():
    report = await smoke_mcp_server(
        "this-command-does-not-exist-xyz", [], {}, timeout=10.0,
        context=await launch_context("creation"),
    )
    assert report.ok is False
    assert report.detail
