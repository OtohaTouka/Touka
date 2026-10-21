"""ToukaProxy → ToukaIDE MCP SDK v2 client adapter."""

from __future__ import annotations

import secrets
from typing import Any

from src.protocols.mcp.client import connect_mcp_server
from src.protocols.registry import ProtocolServerConfig
from src.utils.ide_human_approval import build_human_approval_meta
from src.utils.time_utils import get_current_epoch_ms

_IDE_SESSION_ENV_NAME = "TOUKA_IDE_SESSION_TOKEN"


def _parse_mcp_result(res_json: dict[str, Any]) -> str:
    """相容舊測試／診斷資料的第一段文字解析器。"""
    content_list = res_json.get("result", {}).get("content", [])
    if not content_list:
        return ""
    return content_list[0].get("text", "")


def _serialize_content_item(item: Any) -> dict[str, Any]:
    """將 SDK v2 content model 還原成 MCP wire alias。"""
    if hasattr(item, "model_dump"):
        return item.model_dump(by_alias=True, exclude_none=True)
    if isinstance(item, dict):
        return item
    msg = "ToukaIDE returned an unsupported MCP content item."
    raise TypeError(msg)


async def call_ide_tool(
    ide_clients: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    *,
    human_approved: bool = False,
) -> dict[str, Any]:
    """以共用 SDK v2 modern negotiation 呼叫 ToukaIDE 工具。

    ``human_approved`` 只能來自人類在 Touka 審批卡上的核准（可信 ``_hitl_preapproved``）；
    為 True 時附上綁住這次呼叫的「由人核准」標記，IDE 才不跳第二次確認（ADR-0246 U2c-7）。
    審查員放行與 YOLO 放行不帶標記，照 IDE 自己的模式處理。
    """
    config = ide_clients.get("ToukaUI")
    if not config:
        msg = "ToukaUI IDE Server 尚未連接"
        raise ConnectionError(msg)

    token = config.get("token")
    if not isinstance(token, str) or not token:
        msg = "ToukaUI discovery is missing its Bearer token."
        raise ConnectionError(msg)

    protocol_mode = config.get("protocol_mode", "2026-07-28")
    server_config = ProtocolServerConfig(
        url=config["url"],
        headers={
            "Authorization": f"Bearer ${{{_IDE_SESSION_ENV_NAME}}}",
        },
        env={_IDE_SESSION_ENV_NAME: token},
        protocol_mode=protocol_mode,
    )
    client, stack = await connect_mcp_server(server_config)
    try:
        meta = (
            build_human_approval_meta(
                token,
                tool_name,
                arguments,
                now_ms=get_current_epoch_ms(),
                nonce=secrets.token_hex(16),
            )
            if human_approved
            else None
        )
        result = await client.call_tool(tool_name, arguments, meta=meta)
        return {
            "content": [_serialize_content_item(item) for item in result.content],
            "isError": bool(result.is_error),
        }
    finally:
        await stack.aclose()
