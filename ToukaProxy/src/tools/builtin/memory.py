"""記憶管家轉發器 (Memory Gateway) 模組.

負責將 Agent 的記憶讀寫請求轉發至實體 `memory_server` 模組.
這是 Touka 記憶主權的一部分，透過此門戶可以存取向量資料庫中的對話歷史與知識片段.
"""

from __future__ import annotations

import logging
from typing import Any

from memory_server.factual import SupersedeTargetError
from memory_server.models.conversation_search import ConversationSearchError
from memory_server.vector import SemanticSearchUnavailableError
from src.services.continuity_events import events_scope
from src.services.memory_source_resolver import MemorySourceResolver
from src.services.tool_errors import (
    INTERNAL_ERROR,
    PERMISSION_DENIED,
    RESOURCE_NOT_FOUND,
    UPSTREAM_ERROR,
    build_tool_error,
)
from src.tools.execution_context import EXECUTION_CONTEXT_KWARG_NAMES

logger = logging.getLogger(__name__)

# 記憶來源的記下與查回（單例；閘道持有，不在 main.py 注入）
_SOURCE_RESOLVER = MemorySourceResolver()
# 可信參數：只由閘道依執行器注入的 kwargs 組成；外面帶進來的同名參數一律剝掉
_TRUSTED_SOURCE_KWARGS = frozenset({"source_context", "source_resolver"})

# --- [依賴管理：SSOT Import] ---
try:
    # 嘗試匯入外部記憶伺服器工具
    from memory_server.tools import memory_tools as external_memory_tools
except ImportError:
    external_memory_tools = None

# ToolExecutor 會注入一批與記憶子系統無關的中繼 kwargs（請求/事件/身分中繼資料）。
# 記憶管家的 save/query/forget 為嚴格方法簽章（不收 **kwargs）→ 轉發前須過濾，否則
# 觸發 TypeError（如 query_memory() got an unexpected keyword argument 'request_id'）。
# 註：記憶動作不需要 resource_proxy（向量/事實庫經 get_resource_proxy() 單例自取）；
# 只有 vault 動作需要，且 vault 走另一條 dispatcher 路徑。
_NON_MEMORY_KWARGS = EXECUTION_CONTEXT_KWARG_NAMES | {"resource_proxy"} | _TRUSTED_SOURCE_KWARGS


async def memory_tools(action: str, **kwargs: Any) -> str:
    """記憶管家轉發入口.

    將指定的核心操作 (如 search, save, forget) 轉發至外部實例化的記憶模組.

    Args:
        action (str): 執行動作 (例如：'search', 'save', 'list_tags').
        **kwargs (Any): 動作所需的參數 (例如：'query', 'content', 'tags').

    Returns:
        str: 記憶系統的回傳結果或錯誤訊息.
    """
    if action == "conversations" and (kwargs.get("_ctx") or {}).get("incognito"):
        return build_tool_error(
            PERMISSION_DENIED,
            "Conversation search is unavailable in incognito.",
            reason="incognito",
            action=action,
        )
    if external_memory_tools is None:
        logger.error("❌ MEMORY SYSTEM MISSING: memory_server.tools could not be loaded.")
        return build_tool_error(
            INTERNAL_ERROR,
            "The memory system is not loaded.",
            suggestion="Tell the user the memory system is unavailable; do not retry.",
            action=action,
        )

    try:
        # 過濾掉執行器中繼 kwargs（與 resource_proxy），只轉發 identity ＋ 領域參數。
        clean_kwargs = {k: v for k, v in kwargs.items() if k not in _NON_MEMORY_KWARGS}
        # 記憶來源只由可信執行資訊組成（session_id／request_id／_ctx 由執行器注入）；
        # 一步取代（manage_dups 的 supersede）存的新記憶同樣記來源
        supersede = action == "manage_dups" and clean_kwargs.get("operation") == "supersede"
        if action == "save" or supersede:
            source = await _SOURCE_RESOLVER.capture(
                kwargs.get("identity"),
                kwargs.get("_ctx"),
                session_id=kwargs.get("session_id"),
                request_id=kwargs.get("request_id"),
            )
            if source is not None:
                clean_kwargs["source_context"] = source
        elif action == "query" and clean_kwargs.get("include_sources"):
            clean_kwargs["source_resolver"] = _SOURCE_RESOLVER
        # 無痕、子代理、派工的記憶事件不記（記憶工具本身的行為不變）
        with events_scope(kwargs.get("_ctx")):
            return await external_memory_tools(action=action, **clean_kwargs)
    except SupersedeTargetError:  # 查無（含別人的、已封存、已被取代）：同一句，不回顯 id
        return build_tool_error(
            RESOURCE_NOT_FOUND,
            "The memory to supersede is not one of your active memories.",
            suggestion=(
                "Query again and use the ID of the current version; forgotten or already "
                "superseded memories cannot be superseded."
            ),
            action=action,
        )
    except ConversationSearchError as exc:
        return build_tool_error(
            exc.reason,
            "Conversation search could not be completed.",
            action=action,
        )
    except SemanticSearchUnavailableError:  # 語意查詢沒做成（目前只有重複掃描會到這裡）
        return build_tool_error(
            UPSTREAM_ERROR,
            "Semantic search is unavailable, so this action could not finish.",
            suggestion=(
                "Do not treat this as finding nothing (for a duplicate scan: not as no "
                "duplicates); tell the user it failed."
            ),
            action=action,
        )
    except Exception:
        if action != "conversations":
            logger.exception("🔥 Memory Gateway Error")
        return build_tool_error(
            INTERNAL_ERROR,
            "The memory system failed while handling this action.",
            suggestion="Do not treat this as having no memories; tell the user it failed.",
            action=action,
        )
