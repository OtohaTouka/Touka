"""History Management Utilities for ToukaProxy.

Handles tree-based and linear dialogue histories, including sanitization of hallucinations.
"""

import logging
import re
import uuid
from typing import Any

from .time_utils import get_current_epoch

logger = logging.getLogger(__name__)


class FixedNodeConflict(ValueError):
    """固定回合節點不能覆蓋既有節點或接到不存在的 parent。"""

    def __init__(self) -> None:
        super().__init__("Fixed conversation node conflicts with its base")


class TreeMessageManager:
    """專門處理樹狀對話歷史 (Tree History) 的管理類別。

    符合 Open WebUI 的字典格式與 currentId 指針邏輯。
    """

    @staticmethod
    def create_empty_tree() -> dict[str, Any]:
        """建立一個空的樹狀對話歷史。"""
        return {"messages": {}, "currentId": None}

    @staticmethod
    def get_linear_path(
        tree_history: dict[str, Any] | list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """從 currentId 開始往回追溯 parentId，還原出一條線性的對話路徑。

        支援樹狀字典格式或扁平陣列格式。
        """
        if not tree_history:
            return []

        # 如果已經是列表格式，直接回傳
        if isinstance(tree_history, list):
            return tree_history

        if (
            not isinstance(tree_history, dict)
            or "messages" not in tree_history
            or not tree_history.get("currentId")
        ):
            return []

        messages_map = tree_history["messages"]
        curr_id = tree_history["currentId"]

        path = []
        visited = set()

        while curr_id and curr_id in messages_map and curr_id not in visited:
            node = messages_map[curr_id]
            # 複製一份以免影響原始資料
            path.insert(0, dict(node))
            visited.add(curr_id)
            curr_id = node.get("parentId")

        return path

    @staticmethod
    def append_node(
        tree_history: dict[str, Any],
        role: str,
        content: str,
        name: str | None = None,
        parent_id: str | None = None,
        *,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        """在對話樹中新增一個節點。

        - parent_id: 如果未指定，則預設接在目前的 currentId 後面。
        - name: 用於標記具體的 Agent ID (特別是在 ToukaGroup 群聊中)。
        """
        if not tree_history or "messages" not in tree_history:
            tree_history = TreeMessageManager.create_empty_tree()

        new_id = node_id if node_id is not None else str(uuid.uuid4())
        # 如果沒指定 parent_id，則接在當前葉子節點後
        current_id = tree_history.get("currentId")
        final_parent_id = parent_id if parent_id is not None else current_id
        if node_id is not None and (
            node_id in tree_history["messages"] or final_parent_id not in tree_history["messages"]
        ):
            raise FixedNodeConflict

        node = {
            "id": new_id,
            "role": role,
            "content": content,
            "parentId": final_parent_id,
            "timestamp": int(get_current_epoch() * 1000),
        }
        if name:
            node["name"] = name

        tree_history["messages"][new_id] = node
        if node_id is not None:
            parent = tree_history["messages"][final_parent_id]
            parent["childrenIds"] = [*parent.get("childrenIds", []), new_id]
        tree_history["currentId"] = new_id

        return tree_history


def sanitize_history(messages: list[dict | Any]) -> None:
    """清理歷史訊息中的工具執行記錄與幻覺警告。"""
    patterns = [
        r"\n?(?:> 正在執行工具：|\(開始調用工具\)).*?(?:✅|❌|✓|✕)[^\n]*",
        r"\n?(?:時間記錄：|Time:)\s*?\d{4}-\d{2}-\d{2}[^\n]*",
        r"\n?20\d{2}-\d{2}-\d{2}.*?(?:週.|[A-Za-z]+)[^\n]*",
        r"\n?⚠️\s*?\[(?:工具|記憶)幻覺警告\][^\n]*",
    ]
    regex = re.compile("|".join(patterns), re.IGNORECASE | re.DOTALL)

    for msg in messages:
        if isinstance(msg, dict):
            role = msg.get("role", "")
            content = msg.get("content", "")
        else:
            role = getattr(msg, "role", "")
            content = getattr(msg, "content", "")

        if role == "assistant" and isinstance(content, str):
            new_content = content
            while True:
                temp_content, count = regex.subn("\n", new_content)
                if count == 0:
                    break
                new_content = temp_content

            new_content = new_content.strip()
            if new_content != content:
                logger.info("🛡️ Sanitized persistent hallucination from history.")
                if isinstance(msg, dict):
                    msg["content"] = new_content
                else:
                    msg.content = new_content


def get_linear_messages_from_tree(tree_history: dict[str, Any]) -> list[dict[str, Any]]:
    """相容性封裝：呼叫 TreeMessageManager 獲取線性路徑。"""
    return TreeMessageManager.get_linear_path(tree_history)


def count_rounds_from_linear(messages: list[dict[str, Any]]) -> int:
    """計算線性訊息序列中完整的來回數（SSOT）。

    定義：user + 其後緊接的 assistant = 1 round。
    - 跳過 system/nudge 等非對話角色。
    - 末尾沒有 assistant 配對的 user 訊息不計入。
    """
    rounds = 0
    i = 0
    while i < len(messages):
        if messages[i].get("role") == "user":
            j = i + 1
            while j < len(messages) and messages[j].get("role") not in ("user", "assistant"):
                j += 1
            if j < len(messages) and messages[j].get("role") == "assistant":
                rounds += 1
                i = j + 1
            else:
                i += 1
        else:
            i += 1
    return rounds


def append_message_to_history(history: dict | list, role: str, content: str) -> dict | list:
    """相容性封裝：支援舊有的列表格式與新的樹狀格式。"""
    if isinstance(history, dict) and "messages" in history:
        return TreeMessageManager.append_node(history, role, content)

    if isinstance(history, list):
        history.append(
            {
                "role": role,
                "content": content,
                "timestamp": int(get_current_epoch() * 1000),
            }
        )
        return history

    return [{"role": role, "content": content, "timestamp": int(get_current_epoch() * 1000)}]
