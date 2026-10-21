"""ADR-0069 Phase A — SMOKE 試跑：以淨環境啟動候選 MCP server 並握手驗證。

環境與正式安裝的 server 同一個白名單底（``LaunchEnvironmentPolicy.clean``；候選 server 看不到
Touka 與廠商密鑰），只再加回該 spec 自身宣告的 ``env``；``asyncio.wait_for`` 為握手/列表加逾時，
``async with`` 上下文管理器保證子行程拆除。供 ``CreationEngine`` 的 SMOKE 階段呼叫。
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from src.protocols.mcp.client import McpIsolatedLaunch, connect_mcp_server
from src.protocols.mcp.environment import LaunchEnvironmentPolicy
from src.protocols.registry import ProtocolServerConfig
from src.services.creation.models import SmokeReport
from src.services.isolation.mcp_launch import mcp_launcher
from src.services.isolation.runners import IsolationUnavailableError
from src.services.isolation.status import isolation_monitor

if TYPE_CHECKING:
    from mcp import Client

    from src.services.creation.kinds import CreationLaunchContext

logger = logging.getLogger(__name__)

_DEFAULT_SMOKE_TIMEOUT = 30.0


def _build_launch_env(env: dict[str, str] | None) -> dict[str, str]:
    """白名單底（無 Touka 與廠商密鑰），疊加候選 server 自身宣告的 env。"""
    return LaunchEnvironmentPolicy.clean(env)


async def _connect_candidate(
    config: ProtocolServerConfig, name: str, context: CreationLaunchContext | None,
    timeout: float,
) -> tuple[Client, contextlib.AsyncExitStack]:
    if context is None:
        raise IsolationUnavailableError("missing_launch_context")
    launch = await mcp_launcher.candidate_launch(
        context.grant, name, config,
        kind="scaffold" if context.origin == "scaffold" else "self",
    )
    isolated = {}
    if launch.acquired is not None:
        isolated["isolated"] = McpIsolatedLaunch(
            launch, label=name, report_failure=isolation_monitor.report_failure, on_eof="close",
        )
    return await connect_mcp_server(
        config, launch_environment=_build_launch_env(config.env),
        read_timeout_seconds=timeout, **isolated,
    )


async def smoke_mcp_server(
    command: str,
    args: list[str],
    env: dict[str, str] | None = None,
    timeout: float = _DEFAULT_SMOKE_TIMEOUT,
    protocol_mode: Literal["2026-07-28"] = "2026-07-28",
    *,
    name: str = "candidate",
    context: CreationLaunchContext | None = None,
) -> SmokeReport:
    """啟動候選 MCP server（淨環境）→ 握手 → tools/list，回傳 SmokeReport。

    逾時或啟動/握手失敗 → ``ok=False``；``async with`` 確保子行程必被拆除。
    """
    config = ProtocolServerConfig(
        command=command,
        args=args,
        env=env,
        protocol_mode=protocol_mode,
    )
    stack = None
    try:
        client, stack = await _connect_candidate(config, name, context, timeout)
        tools_result = await client.list_tools()
        count = len(tools_result.tools)
        if count >= 1:
            return SmokeReport(ok=True, tool_count=count, detail=f"啟動成功，{count} 個工具")
        return SmokeReport(ok=False, tool_count=0, detail="server 啟動但未提供任何工具")
    except IsolationUnavailableError as e:
        return SmokeReport(ok=False, detail=f"isolation_unavailable: {e.reason}")
    except TimeoutError:
        return SmokeReport(ok=False, detail=f"逾時 {timeout}s（server 無回應）")
    except Exception as e:
        return SmokeReport(ok=False, detail=f"{type(e).__name__}: {e}")
    finally:
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()


@dataclass
class ToolInfo:
    """候選 MCP server 暴露的單一工具（唯讀枚舉用，不含執行）。"""

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)


async def list_mcp_tools(
    command: str,
    args: list[str],
    env: dict[str, str] | None = None,
    timeout: float = _DEFAULT_SMOKE_TIMEOUT,
    protocol_mode: Literal["2026-07-28"] = "2026-07-28",
    *,
    name: str = "candidate",
    context: CreationLaunchContext | None = None,
) -> list[ToolInfo]:
    """淨環境啟動候選 MCP server → 握手 → tools/list，回傳工具清單（**永不執行工具**）。

    供 EVAL 後置階段唯讀枚舉工具；逾時/啟動/握手失敗一律回 ``[]``（呼叫端優雅降級）。
    """
    config = ProtocolServerConfig(
        command=command,
        args=args,
        env=env,
        protocol_mode=protocol_mode,
    )
    stack = None
    try:
        client, stack = await _connect_candidate(config, name, context, timeout)
        tools_result = await client.list_tools()
        return [
            ToolInfo(
                name=t.name,
                description=t.description or "",
                input_schema=t.input_schema or {},
            )
            for t in tools_result.tools
        ]
    except Exception as e:
        logger.warning("list_mcp_tools 失敗（回空清單）：%s", e)
        return []
    finally:
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()
