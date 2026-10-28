"""專案工具 (Project Tools) 模組.

負責管理與操作專案源代碼 (Codebase)。
本模組透過 ResourceManager 獲取路徑沙盒，並將底層物理操作下沉至 ToukaMemory 的核心檔案操作引擎.
所有寫入動作皆會自動記錄審計事件 (Project Events).
"""

import logging
from pathlib import Path
from typing import Any

from src.services import tool_errors
from src.services.authorization_gateway import owned_by_admin
from src.services.isolation import profiles
from src.services.resource_manager import bypasses_project_jail, resource_manager
from src.utils.async_db import add_project_event
from src.utils.touka_dirs import PROTECTED_LOCATION

logger = logging.getLogger(__name__)

# --- [依賴管理：SSOT Core Ops] ---
try:
    from memory_server.tools.core_file_ops import (
        core_delete,
        core_find,
        core_grep,
        core_list,
        core_read,
        core_str_replace,
        core_write,
    )
except ImportError:
    # 避免單行賦值過長
    core_delete = core_find = core_grep = None
    core_list = core_read = core_str_replace = core_write = None


def _root_refused(project_id: str, reason: str) -> str:
    """專案根不能用時給模型的結構化錯誤：受保護，或（容器裡）還沒查完。"""
    if reason == PROTECTED_LOCATION:
        message = f"Project root for '{project_id}' is in a location Touka protects."
        suggestion = "Ask the user to choose a project folder outside Touka's own folders."
    else:
        message = (
            f"Touka could not finish checking the project root for '{project_id}' "
            "(too many folders, a folder it cannot read, or the check is still running)."
        )
        suggestion = "Try again shortly; if it keeps failing, ask the user for a smaller folder."
    return tool_errors.build_tool_error(
        tool_errors.PERMISSION_DENIED, message, suggestion=suggestion, reason=reason
    )


async def project_tools(action: str, path: str | None = None, **kwargs: Any) -> Any:
    """專案源碼管理工具入口.

    受專案路徑沙盒保護，僅能在指定的 project_id 根目錄下執行檔案操作.

    Args:
        action (str): 執行動作 (read, write, append, str_replace, list, find, grep, delete).
        path (str | None): 相對於專案根目錄的路徑.
        **kwargs (Any): 動作所需參數 (如 content, old_str, new_str, query, pattern 等).
                       必須包含 'project_id' 以定位沙盒.

    Returns:
        Any: 操作結果文字、檔案內容或錯誤訊息.
    """
    if core_read is None:
        return "Error: Memory core engine is not properly integrated."

    project_id = kwargs.get("project_id")
    identity = kwargs.get("identity")

    if not project_id:
        return "Error: No project selected."

    # 獲取專案路徑沙盒 (SSOT - Resource Sovereignty)。
    # A-fix: 傳入呼叫者身分，使 _resolve_project_path 對非 admin/Debugger 套用工作區
    # 包含性檢查 (越界專案根目錄即拒絕，回 None)。identity 缺漏時 get_project_path
    # 退回 System 身分 → 受限 (fail-safe)。
    root_bound = await resource_manager.get_project_path(project_id, identity)
    if not root_bound:
        return f"Error: Project root for '{project_id}' not found."
    # 一般使用者的靈魂，專案根不能是 Touka 保護的地方（設定與 hooks、資料、程式碼），否則經
    # 專案寫入就能改到不經隔離執行的 hook 程式（ADR-0292）。每次呼叫都查，登記後才改的也擋。
    if not (identity and (bypasses_project_jail(identity) or owned_by_admin(identity))):
        reason = await profiles.project_root_reason(Path(root_bound), profiles.current_layout())
        if reason:
            logger.warning("Project %s root refused (%s).", project_id, reason)
            return _root_refused(project_id, reason)

    try:
        # --- [分發邏輯：Core Ops Mapping] ---
        res: Any = None

        if action == "read":
            return await core_read(path, root_bound, **kwargs)

        if action == "write":
            content = kwargs.get("content")
            res = await core_write(path, root_bound, content=content, mode="w")
            if res.startswith("Success") and identity:
                await add_project_event(project_id, identity, "write", f"Wrote to {path}")

        elif action == "append":
            content = kwargs.get("content")
            res = await core_write(path, root_bound, content=content, mode="a")
            if res.startswith("Success") and identity:
                await add_project_event(project_id, identity, "append", f"Appended to {path}")

        elif action == "str_replace":
            old_str = kwargs.get("old_str")
            new_str = kwargs.get("new_str")
            res = await core_str_replace(path, root_bound, old_str=old_str, new_str=new_str)
            if res.startswith("Success") and identity:
                await add_project_event(
                    project_id, identity, "replace", f"Replaced content in {path}"
                )

        elif action == "list":
            return await core_list(path or ".", root_bound, max_depth=kwargs.get("max_depth", 2))

        elif action == "find":
            return await core_find(kwargs.get("pattern", "*"), root_bound)

        elif action == "grep":
            return await core_grep(kwargs.get("query", ""), root_bound)

        elif action == "delete":
            res = await core_delete(path, root_bound)
            if res.startswith("Success") and identity:
                await add_project_event(project_id, identity, "delete", f"Deleted {path}")

        else:
            return f"Error: Unknown action '{action}' for project_tools."

    except Exception as e:
        logger.exception("🔥 Error in project_tools execution")
        return f"Error executing {action}: {str(e)}"
    else:
        return res


# --- Schema Definition ---

# 檔案動詞家族的條件必填（oneOf、action 判別式）——project_tools 與 vault_tools 動詞
# 同義同名（Tier 2 詞彙對齊），共用同一張表（SSOT）。Gemini 出場端會剝除 oneOf
# （其 OpenAPI 子集不支援）；Claude 出場只剝根層組合子（tool_schema）；OpenAI 相容原樣收到。
FILE_ACTION_CONDITIONALS = [
    {"properties": {"action": {"const": "read"}}, "required": ["path"]},
    {"properties": {"action": {"const": "write"}}, "required": ["path", "content"]},
    {"properties": {"action": {"const": "append"}}, "required": ["path", "content"]},
    {
        "properties": {"action": {"const": "str_replace"}},
        "required": ["path", "old_str", "new_str"],
    },
    {"properties": {"action": {"const": "list"}}, "required": []},
    {"properties": {"action": {"const": "find"}}, "required": ["pattern"]},
    {"properties": {"action": {"const": "grep"}}, "required": ["query"]},
    {"properties": {"action": {"const": "delete"}}, "required": ["path"]},
]

FILE_ACTION_PARAM_HINT = (
    "動作。必要參數：read/delete 需 path；write/append 需 path+content；"
    "str_replace 需 path+old_str+new_str；grep 需 query；find 需 pattern；"
    "list 免必填（path 可選）"
)

PROJECT_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "project_tools",
        "description": (
            "專案工具：讀取、搜尋與精密修改「當前專案」的源代碼，操作受專案路徑"
            "沙盒保護。何時使用：處理使用者選定專案內的檔案。何時不要用：個人筆記"
            "或非專案檔案請用 vault_tools；網路資訊請用 research。project_id 與身分"
            "由系統依當前會話自動注入，你不需要（也無法）提供。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": FILE_ACTION_PARAM_HINT,
                    "enum": [
                        "read",
                        "write",
                        "append",
                        "str_replace",
                        "list",
                        "find",
                        "grep",
                        "delete",
                    ],
                },
                "path": {"type": "string", "description": "相對於專案根目錄的路徑"},
                "content": {"type": "string", "description": "write 或 append 的內容"},
                "old_str": {
                    "type": "string",
                    "description": "str_replace 的舊字串（需精確且唯一匹配）",
                },
                "new_str": {"type": "string", "description": "str_replace 的新字串"},
                "query": {"type": "string", "description": "grep 的搜尋關鍵字"},
                "pattern": {"type": "string", "description": "find 的檔名匹配模式"},
                "max_depth": {"type": "integer", "default": 2, "description": "list 的深度"},
            },
            "required": ["action"],
            "oneOf": FILE_ACTION_CONDITIONALS,
        },
    },
}
