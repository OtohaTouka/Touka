"""對話歸屬、內部頻道命名與空草稿判斷；身分須由呼叫端解析。"""

import hashlib
import json
from typing import Any

from memory_server.models.conversation_search import ConversationSearchError

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity


def require_conversation_actor(identity: AgentIdentity, passport: dict[str, Any] | None) -> None:
    """新搜尋只接受靈魂與精確 DebugAgent；角色不擴張可見範圍。"""
    if identity.is_human or not (
        identity.agent_id == "DebugAgent" or (passport and passport["is_toukasoul"])
    ):
        raise ConversationSearchError("permission_denied")


def is_searchable_session(row: dict[str, Any]) -> bool:
    """舊列不補認證；退休頻道即使有標記也不可見。"""
    return (
        type(row["session_scope_version"]) is int
        and row["session_scope_version"] == 1
        and not is_retired_session(row["id"])
    )


def is_retired_session(session_id: str) -> bool:
    """舊共享頻道只供讀取，不再接受續寫。"""
    return session_id == "pulse_channel_default" or session_id.startswith("sns_")


def channel_session_id(kind: str, soul_id: str, *coordinates: str) -> str:
    """完整雜湊避免不同靈魂或含分隔符的外部頻道互相碰撞。"""
    payload = json.dumps([kind, soul_id, *coordinates], ensure_ascii=False, separators=(",", ":"))
    prefix = "hb" if kind == "heartbeat" else "sns"
    return f"touka.{prefix}.v1.{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def require_session_scope(
    row: dict[str, Any], bound: str | None, target: str | None, home: str | None
) -> None:
    """角色權限不能代替精確綁定；矛盾家欄位也不能當普通對話續寫。"""
    if (
        is_retired_session(row["id"])
        or bound != target
        or (row["is_home"] and (not target or home != target))
        or (row["home_for_agent"] is not None and home != target)
    ):
        raise ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH)


def can_rebind_session(row: dict[str, Any], *, human_ui: bool, target: str | None) -> bool:
    """家與內部頻道不能藉一般 UI 保存換綁，也不能經 NULL 中轉。"""
    return bool(
        human_ui
        and target
        and not row["is_home"]
        and row["home_for_agent"] is None
        and not is_retired_session(row["id"])
        and not row["id"].startswith("touka.")
    )


def is_empty_draft(row: dict[str, Any]) -> bool:
    """整棵原樹（含旁支）與摘要均空才保留建立標記。"""
    return (
        not row["messages"]["messages"]
        and row["context_summary"] is None
        and not row["is_home"]
        and row["home_for_agent"] is None
        and not is_retired_session(row["id"])
        and not row["id"].startswith("touka.")
    )


def is_ui_turn_base(row: dict[str, Any], user_id: str, assistant_id: str) -> bool:
    """生成只能接在已保存的目前 user 節點，不能覆蓋既存助理節點。"""
    tree = row["messages"]
    user = tree["messages"].get(user_id)
    return bool(
        user
        and user.get("role") == "user"
        and tree.get("currentId") == user_id
        and assistant_id not in tree["messages"]
    )
