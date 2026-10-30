"""IDE 變更預覽工具 (IDE Diff Preview).

整合 Touka Studio IDE 的 Diff 預覽功能，支援在正式修改前發起視覺化審查。
（舊的 ``text_editor`` 已由檔案動詞取代，沒有呼叫端，ADR-0293 刪除。）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from src.config import HOST
from src.services.resource_manager import resource_manager
from src.tools.contracts import native_parameters

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)


def _get_ide_config() -> dict[str, Any] | None:
    """尋找並讀取本地 IDE 伺服器的動態配置與 Token.

    Returns:
        dict[str, Any] | None: 包含 authToken, port 等資訊的字典，若未找到則回傳 None.
    """
    # 使用 SSOT 路徑主權下的暫存目錄
    ide_temp_dir = Path(resource_manager.get_temp_path("ide"))

    if not resource_manager.exists(ide_temp_dir):
        return None

    # 搜尋動態連接埠文件
    config_files = list(ide_temp_dir.glob("gemini-ide-server-*-3002.json"))
    if not config_files:
        return None

    try:
        return json.loads(resource_manager.read_file_text(config_files[0]))
    except Exception:
        return None


async def ide_open_diff(
    path: str,
    new_content: str,
    identity: AgentIdentity,  # noqa: ARG001
    session_id: str = "global",
    **kwargs: Any,  # noqa: ARG001
) -> str:
    """呼叫 IDE 伺服器的 MCP 介面，在畫面上開啟 Diff 預覽.

    這是一個非破壞性操作，用於在決定寫入檔案前，讓使用者在 Touka Studio 中看到建議的改動。

    Args:
        path (str): 檔案路徑.
        new_content (str): 建議的新內容全文.
        identity (AgentIdentity): 發起請求的 Agent 身分 (僅供審計).
        session_id (str): 目標 IDE 會話 ID。預設為 'global'.
        **kwargs (Any): 接收額外的未使用參數.

    Returns:
        str: 預覽推播結果描述.
    """
    config = _get_ide_config()
    if not config:
        return "Error: IDE Server not found or not running on port 3002."

    payload = {
        "jsonrpc": "2.0",
        "id": "agent-diff",
        "method": "tools/call",
        "params": {
            "name": "openDiff",
            "arguments": {
                "filePath": path,
                "newContent": new_content,
                "sessionId": session_id,
            },
        },
    }

    headers = {
        "Authorization": f"Bearer {config['authToken']}",
        "Content-Type": "application/json",
    }

    try:
        ide_host = "localhost" if HOST == "0.0.0.0" else HOST  # noqa: S104
        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.post(
                f"http://{ide_host}:{config['port']}/mcp",
                json=payload,
                headers=headers,
                timeout=5.0,
            )
            if resp.status_code == 200:
                logger.info(f"🎨 IDE: Diff preview opened for {path}")
                return f"Success: Diff for '{path}' sent to UI for preview."
            return f"Error: IDE Server status {resp.status_code}"
    except Exception as e:
        logger.exception("🔥 IDE Diff Connection Error")
        return f"Error connecting to IDE Server: {str(e)}"


IDE_OPEN_DIFF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ide_open_diff",
        "description": "IDE 變更預覽：將代碼改動建議推播至 Touka Studio UI 進行 Diff 預覽。",
        "parameters": native_parameters("ide_open_diff"),
    },
}
