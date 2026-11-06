"""記憶伺服器工具層進入點 (Unified Gateway).

本模組落實 MEMORY_SSOT_MANDATE.md，作為 ToukaProxy 整合模式與 ToukaMemory 獨立模式的統一權威來源。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from memory_server.config_utils import get_resource_proxy
from memory_server.services.personal_catalog import run_vault_action
from memory_server.tools.core_file_ops import (
    PathGuardError,
    core_delete,
    core_find,
    core_grep,
    core_list,
    core_read,
    core_str_replace,
    core_write,
    drive_target,
)
from memory_server.tools.diag import (
    manage_duplicate_memories,
    memory_diagnostics,
    reindex_memories,
)
from memory_server.tools.legacy_vault import search_conversations
from memory_server.tools.mission_tools import mission_tools
from memory_server.tools.recall import random_recall

# 匯入底層實作
from memory_server.tools.schemas import MEMORY_TOOLS_SCHEMA_LIST
from memory_server.utils.memory_worker import run_memory_job

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

# --- [ 全域實例管理 ] ---
_global_manager = None


def get_manager() -> Any:
    """獲取全域 MemoryManager 實例 (單例模式)。"""
    global _global_manager
    if _global_manager is None:
        from memory_server.services.manager import MemoryManager

        _global_manager = MemoryManager()
    return _global_manager


# --- [ 核心記憶操作 (支援獨立模式) ] ---


async def save_memory(identity: AgentIdentity, **kwargs: Any) -> str:
    """儲存記憶實作。"""
    manager = kwargs.pop("manager", get_manager())
    return await manager.save_memory(identity=identity, **kwargs)


async def query_memory(identity: AgentIdentity, **kwargs: Any) -> str:
    """查詢記憶實作。"""
    manager = kwargs.pop("manager", get_manager())
    return await manager.query_memory(identity=identity, **kwargs)


async def forget_memory(identity: AgentIdentity, **kwargs: Any) -> str:
    """封存記憶實作（在記憶工作者上做，ADR-0288）。"""
    manager = kwargs.pop("manager", get_manager())
    return await run_memory_job(manager.resolve_duplicate, identity=identity, **kwargs)


async def export_facts(identity: AgentIdentity, **kwargs: Any) -> str:
    """匯出某 agent 的全部事實記憶為 JSON 字串（ADR-0110 GDPR 自助匯出，**唯讀**）。

    含已封存（``include_archived=True``）；依 ``identity.agent_id`` 嚴格範圍化——呼叫端
    僅能取得該 agent 自身的事實，不會跨 agent 洩漏。不寫入、不修改任何既有資料。
    """
    manager = kwargs.pop("manager", get_manager())
    facts = await run_memory_job(
        lambda: manager.get_factual_store().query_facts(identity=identity, include_archived=True)
    )
    return json.dumps(
        {"agent_id": identity.agent_id, "count": len(facts), "facts": facts},
        ensure_ascii=False,
    )


# --- [ 全域動作映射 (供分發器與測試使用) ] ---

VAULT_ACTIONS = {
    "read": core_read,
    "write": core_write,
    "append": lambda **kw: core_write(mode="a", **kw),
    "str_replace": core_str_replace,
    "delete": core_delete,
    "list": core_list,
    "find": core_find,
    "grep": core_grep,
}

MEMORY_ACTIONS = {
    "random": random_recall,
    "diagnostics": memory_diagnostics,
    "manage_dups": manage_duplicate_memories,
    "reindex": reindex_memories,
    "conversations": search_conversations,
    "save": save_memory,
    "query": query_memory,
    "forget": forget_memory,
    "export_facts": export_facts,
}


# --- [1. 整合模式分發器：vault_tools] ---


async def vault_tools(vault: str, action: str, **kwargs: Any) -> Any:
    """萬能檔案庫分發器 (Vault Tools Dispatcher)."""
    identity = kwargs.get("identity")
    manager = kwargs.get("manager", get_manager())
    resource_proxy = kwargs.get("resource_proxy")

    if not identity:
        return "Error: identity is required for vault operations."

    # 解析物理根目錄（SSOT）
    if vault == "personal":
        root_bound = manager.get_personal_drive_dir(identity, resource_proxy=resource_proxy)
    elif vault == "legacy":
        root_bound = manager.get_legacy_dir(identity, resource_proxy=resource_proxy)
    elif vault == "soul":
        root_bound = manager.get_soul_dir(identity, resource_proxy=resource_proxy)
    else:
        return f"Error: Unknown vault type '{vault}'."

    # 動作分發
    if action not in VAULT_ACTIONS:
        return f"Error: Unknown action '{action}' for vault_tools."

    # 安全防禦：若參數中遺失 path，則預設為根目錄 (SSOT Robustness)
    if "path" not in kwargs:
        kwargs["path"] = ""

    kwargs.setdefault("resource_proxy", resource_proxy)
    kwargs.pop("exclude", None)  # 隱藏範圍只由系統決定（個人空間見 personal_catalog）
    # 個人空間的根是 ZonePath（ADR-0293），交給目錄層；其他照舊是路徑字串
    if vault == "personal" and root_bound is not None:
        proxy = kwargs["resource_proxy"] = resource_proxy or get_resource_proxy()
        try:
            target = drive_target(str(kwargs["path"] or ""), root_bound, proxy)
        except (PathGuardError, ValueError):  # 越界或名稱不合格：交給檔案動詞報錯
            target = None
        return await run_vault_action(VAULT_ACTIONS[action], action, root_bound, target, kwargs)
    root = str(root_bound) if root_bound else ""
    return await VAULT_ACTIONS[action](root_bound=root, **kwargs)


# --- [2. 整合模式分發器：memory_tools] ---


async def memory_tools(action: str, **kwargs: Any) -> Any:
    """記憶核心分發器 (Memory Tools Dispatcher)."""
    manager = kwargs.get("manager", get_manager())

    if action not in MEMORY_ACTIONS:
        return f"Error: Unknown action '{action}' for memory_tools."

    if action == "manage_dups":
        kwargs["action"] = kwargs.pop("operation", "scan")
    return await MEMORY_ACTIONS[action](manager=manager, **kwargs)


# --- [3. 導出屬性 (供 ToukaProxy/main.py 載入)] ---

memory_tool_registry = {
    "vault_tools": vault_tools,
    "memory_tools": memory_tools,
    "mission_tools": mission_tools,
}

memory_tools_schema = MEMORY_TOOLS_SCHEMA_LIST
