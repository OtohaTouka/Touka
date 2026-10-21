"""高精度檔案編輯工具 (Advanced Text Editor Tools).

提供模擬 Anthropic 標準的檔案操作介面，包含檢視、建立、精密字串替換與插入。
同時整合了 Touka Studio IDE 的 Diff 預覽功能，支援在正式修改前發起視覺化審查。
所有操作均受 AgentSandBox 與 AGENT_SOUL_PROTECTION 雙重保護。
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


def _get_safe_path(path: str, identity: AgentIdentity) -> Path:
    """計算並驗證 Agent 個人磁碟中的安全路徑.

    確保所有檔案操作皆鎖定在個人沙盒 (personal_drive) 內，防止目錄穿越攻擊.

    Args:
        path (str): 相對於個人磁碟的目標路徑.
        identity (AgentIdentity): 當前代理人身分物件.

    Returns:
        Path: 解析後的絕對路徑實體.

    Raises:
        PermissionError: 當嘗試存取沙盒範圍外的路徑時拋出.
    """
    drive_path_str = resource_manager.get_vault_path("personal", identity)
    if not drive_path_str:
        msg = f"Access Denied: No personal drive for {identity.agent_id}"
        raise PermissionError(msg)

    drive_path = Path(drive_path_str).resolve()

    target_path = Path(path)
    if not target_path.is_absolute():
        target_path = drive_path / path

    target_path = target_path.resolve()

    # 安全檢查：路徑主權隔離 (Path Sovereignty Isolation)
    # #7 fix: use is_relative_to() instead of startswith() — the string-prefix check
    # let a sibling dir bypass the sandbox (e.g. "/drive-evil" startswith "/drive").
    # Mirrors the router-side editor fix (C4/P1); both paths are already resolved.
    if not target_path.is_relative_to(drive_path):
        msg = "Access Denied: Sandbox violation."
        raise PermissionError(msg)

    return target_path


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


async def text_editor(
    command: str,
    path: str,
    identity: AgentIdentity,
    file_text: str | None = None,
    old_str: str | None = None,
    new_str: str | None = None,
    view_range: list[int] | None = None,
    insert_line: int | None = None,
    **_kwargs: Any,
) -> str:
    """文字編輯器核心 (Anthropic-Compliant).

    具備原子性寫入與靈魂保護機制，支援精密代碼修改.

    Args:
        command (str): 指令類型 (view, create, str_replace, insert, undo_edit).
        path (str): 目標檔案路徑 (相對於個人沙盒).
        identity (AgentIdentity): 當前執行者的實體身分.
        file_text (str | None): create 指令時寫入的全文.
        old_str (str | None): str_replace 指令時要替換的精確舊字串.
        new_str (str | None): str_replace 或 insert 指令時的新內容.
        view_range (list[int] | None): view 指令時覽內容的行號範圍 [start, end].
        insert_line (int | None): insert 指令時插入的目標行號.
        **_kwargs (Any): 其他上下文資訊.

    Returns:
        str: 執行成功訊息或檔案內容.
    """
    try:
        # 取得安全路徑 (Identity SSOT)
        safe_path = _get_safe_path(path, identity)

        # --- [🛡️ AGENT_SOUL_PROTECTION] ---
        # 嚴禁透過自動化工具修改 legacy 與 soul 等意識核心檔案
        is_write_op = command in ["create", "str_replace", "insert", "undo_edit"]

        if is_write_op and resource_manager.is_protected_path(safe_path):
            return (
                "❌ Soul Protection Error: Automated modification of protected files is prohibited."
            )

        if command == "view":
            if not resource_manager.exists(safe_path):
                return f"Error: File '{path}' not found."
            lines = resource_manager.read_file_text(safe_path).splitlines(keepends=True)

            start = 1
            end = len(lines)
            if view_range and len(view_range) == 2:
                start = max(1, view_range[0])
                end = min(len(lines), view_range[1])

            output = []
            for i in range(start - 1, end):
                output.append(f"{i + 1:4}: {lines[i]}")

            return "".join(output) if output else "(Empty file)"

        if command == "create":
            if resource_manager.exists(safe_path):
                return f"Error: '{path}' already exists. Use 'str_replace'."
            resource_manager.write_file_text(safe_path, file_text or "")
            return f"Success: Created file '{path}'."

        if command == "str_replace":
            if not resource_manager.exists(safe_path):
                return f"Error: File '{path}' not found."
            if old_str is None or new_str is None:
                return "Error: 'old_str' and 'new_str' are required."

            # 備份以供 undo
            resource_manager.copy_file(safe_path, str(safe_path) + ".bak")

            content = resource_manager.read_file_text(safe_path)
            if old_str not in content:
                return f"Error: Match failure for 'old_str' in '{path}'."

            if content.count(old_str) > 1:
                return "Error: Multiple matches found. Please be more specific."

            resource_manager.write_file_text(safe_path, content.replace(old_str, new_str))
            return f"Success: Modified '{path}'."

        if command == "insert":
            if not resource_manager.exists(safe_path):
                return f"Error: File '{path}' not found."
            if insert_line is None or new_str is None:
                return "Error: 'insert_line' and 'new_str' are required."

            resource_manager.copy_file(safe_path, str(safe_path) + ".bak")
            lines = resource_manager.read_file_text(safe_path).splitlines(keepends=True)
            idx = min(len(lines), max(0, insert_line))
            lines.insert(idx, new_str + "\n")
            resource_manager.write_file_text(safe_path, "".join(lines))
            return f"Success: Inserted text at line {insert_line + 1}."

        if command == "undo_edit":
            bak_path = safe_path.with_suffix(safe_path.suffix + ".bak")
            if not resource_manager.exists(bak_path):
                return "Error: No backup found."
            resource_manager.move_file(bak_path, safe_path)
            return f"Success: Reverted last edit for '{path}'."

    except PermissionError as pe:
        return str(pe)
    except Exception as e:
        logger.exception("🔥 Text Editor Core Error")
        return f"Unexpected Error: {str(e)}"
    else:
        return f"Error: Unknown command '{command}'."


# --- [Schema 定義] ---
TEXT_EDITOR_SCHEMA = {
    "type": "function",
    "function": {
        "name": "text_editor",
        "description": "精密檔案編輯器：支援檢視、建立、精密替換與插入。受沙盒與靈魂保護限制。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["view", "create", "str_replace", "insert", "undo_edit"],
                    "description": "執行指令類型",
                },
                "path": {"type": "string", "description": "相對路徑"},
                "file_text": {"type": "string", "description": "create 使用的內容"},
                "old_str": {"type": "string", "description": "str_replace 的舊字串"},
                "new_str": {"type": "string", "description": "新內容"},
                "insert_line": {"type": "integer", "description": "插入行號"},
                "view_range": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "預覽行號 [start, end]",
                },
            },
            "required": ["command", "path"],
        },
    },
}

IDE_OPEN_DIFF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ide_open_diff",
        "description": "IDE 變更預覽：將代碼改動建議推播至 Touka Studio UI 進行 Diff 預覽。",
        "parameters": native_parameters("ide_open_diff"),
    },
}
