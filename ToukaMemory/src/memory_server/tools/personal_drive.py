"""個人雲端硬碟工具 (Personal Drive Tool).

提供代理人隔離的檔案存儲空間，支援檔案讀寫、刪除、列表以及圖片 Base64 編碼讀取。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from datetime import datetime
from typing import TYPE_CHECKING, Any

from memory_server.config_utils import get_resource_proxy

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

    from memory_server.services.manager import MemoryManager

logger = logging.getLogger(__name__)


async def access_personal_drive(
    identity: AgentIdentity,
    manager: MemoryManager,
    filename: str | None = None,
    action: str = "read",
    write_content: str = "",
    read_image: bool = False,
    **kwargs: Any,
) -> Any:
    """存取您的個人檔案庫 (/personal_drive/{agent_id})。"""
    _ = kwargs
    agent_id = identity.agent_id
    agent_dir = manager.get_personal_drive_dir(identity)
    get_resource_proxy().make_dir(str(agent_dir))

    try:
        # 如果沒有檔名或 action 為 list，回傳檔案清單
        if not filename or action == "list":
            files = [f for f in agent_dir.iterdir() if f.is_file()]
            if not files:
                return f"Your personal drive ({agent_id}) is currently empty."

            file_info = []
            for f_path in files:
                # 額外安全檢查：不顯示隱藏檔或目錄外的檔案
                if f_path.name.startswith(".") or not f_path.resolve().is_relative_to(agent_dir):
                    continue
                stats = f_path.stat()
                mtime = datetime.fromtimestamp(stats.st_mtime).strftime("%Y-%m-%d %H:%M")
                size_kb = stats.st_size / 1024
                file_info.append(f"- {f_path.name} (Modified: {mtime}, Size: {size_kb:.1f} KB)")

            return f"Personal Drive for {agent_id}:\n" + "\n".join(file_info)

        # 安全沙盒路徑檢查
        target_path = (agent_dir / filename).resolve()
        if not target_path.is_relative_to(agent_dir):
            logger.warning(
                f"🚫 Security Alert: Agent {agent_id} attempted path traversal with '{filename}'"
            )
            return f"Access denied. Path '{filename}' is outside your drive sandbox."

        if action == "write" or write_content:
            get_resource_proxy().write_file_text(str(target_path), write_content)
            return f"Successfully saved '{filename}' to your personal drive."

        if action == "delete":
            if not target_path.exists():
                return f"File '{filename}' not found. Nothing to delete."
            get_resource_proxy().remove_file(str(target_path))
            return f"Successfully deleted '{filename}' from your personal drive."

        if not target_path.exists():
            return f"File '{filename}' not found in your personal drive."

        # 圖片處理
        image_exts = [".png", ".jpg", ".jpeg", ".gif", ".webp"]
        if target_path.suffix.lower() in image_exts:
            image_data = get_resource_proxy().read_file_binary(str(target_path))
            mime_type, _ = mimetypes.guess_type(target_path)
            
            if not read_image:
                size_kb = len(image_data) / 1024
                return (
                    f"Image File '{filename}': Size: {size_kb:.1f} KB. "
                    "Use read_image=True to view."
                )

            b64_data = base64.b64encode(image_data).decode("utf-8")
            return [
                {"type": "text", "text": f"Successfully loaded image '{filename}'."},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type or "image/jpeg",
                        "data": b64_data,
                    },
                },
            ]
        content = get_resource_proxy().read_file_text(str(target_path)) or ""
        if len(content) > 10000:
            content = content[:10000] + "\n...[Content truncated]..."
    except Exception:
        logger.exception("Error accessing personal drive")
        return "An internal error occurred while accessing your personal drive."
    else:
        return f"=== Personal Drive: {filename} ===\n{content}"
