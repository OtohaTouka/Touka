"""對話歷史資料模型 (History Data Models).

定義對話樹節點 (MessageNode) 與樹狀對話歷史 (TreeHistory) 結構，支援非線性對話分支。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class MessageNode(BaseModel):
    """對話樹中的單一訊息節點"""
    id: str
    parentId: str | None = None
    role: Literal["user", "assistant", "system", "tool"]
    content: str | None = ""
    timestamp: int | None = None
    childrenIds: list[str] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] | None = None
    # 支援擴充欄位以防前端有自定義屬性
    model_config = {"extra": "allow"}

class HistoryValidationError(ValueError):
    """對話歷史驗證異常。"""


class CurrentIdNotFoundError(HistoryValidationError):
    """目前的對話 ID 不在訊息對照表中。"""
    def __init__(self, current_id: str) -> None:
        """初始化當前節點 ID 缺失異常.

        Args:
            current_id (str): 觸發異常的訊息 ID。
        """
        super().__init__(f"currentId '{current_id}' does not exist in messages map")


class TreeHistory(BaseModel):
    """完整對話樹歷史結構 (SSOT)"""
    messages: dict[str, MessageNode]
    currentId: str | None = None

    @model_validator(mode="after")
    def validate_current_id_exists(self) -> TreeHistory:
        """驗證 currentId 是否存在於 messages 對照表中.

        Raises:
            CurrentIdNotFoundError: 當 currentId 指向不存在的節點時拋出。
        """
        if self.currentId and self.currentId not in self.messages:
            raise CurrentIdNotFoundError(self.currentId)
        return self

    @classmethod
    def from_flat_list(cls, messages: list[dict[str, Any]]) -> TreeHistory:
        """將舊式的扁平列表轉換為樹狀結構 (用於向後相容)
        """
        tree_map = {}
        last_id = None
        
        for i, m in enumerate(messages):
            msg_id = m.get("id") or f"msg_legacy_{i}"
            node = MessageNode(
                id=msg_id,
                parentId=last_id,
                role=m.get("role", "user"),
                content=m.get("content", ""),
                timestamp=m.get("timestamp"),
                childrenIds=[]
            )
            tree_map[msg_id] = node
            if last_id and last_id in tree_map:
                tree_map[last_id].childrenIds.append(msg_id)
            last_id = msg_id
            
        return cls(messages=tree_map, currentId=last_id)
