"""Qdrant 本機模式 meta.json 的格式（快照清除 ADR-0270 與嵌入模型比對 ADR-0315 共用這一份）.

meta.json 是 ``{"collections": {名稱: 設定}, "aliases": {別名: 名稱}}``，頂層多一個鍵都不接受。
collection 的設定是 qdrant-client 寫的 CreateCollection dict，只當資料讀；要讀其中的欄位時用
``CollectionConfigView``（允許多餘欄位，認不得的值當成沒有）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class EmptyCollectionNameError(ValueError):
    """meta.json 裡有空的 collection 名稱（Pydantic 會轉成驗證錯誤）."""

    def __init__(self) -> None:
        super().__init__("collection name must not be empty")


class QdrantLocalMeta(BaseModel):
    """meta.json 的格式（多一個鍵都不接受）."""

    model_config = ConfigDict(extra="forbid")

    collections: dict[str, dict[str, Any]]
    aliases: dict[str, str]

    @field_validator("collections")
    @classmethod
    def _names_not_empty(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if any(not name for name in value):
            raise EmptyCollectionNameError
        return value


class CollectionConfigView(BaseModel):
    """一個 collection 設定裡要讀的兩欄：向量長度與備註；其他欄位照留不看."""

    model_config = ConfigDict(extra="allow")

    vectors: Any = None
    metadata: dict[str, Any] | None = None

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_or_none(cls, value: Any) -> dict[str, Any] | None:
        return value if isinstance(value, dict) else None

    @property
    def size(self) -> int | None:
        """沒有名稱的單一向量設定才有長度；具名向量或寫壞的值回 None."""
        size = self.vectors.get("size") if isinstance(self.vectors, dict) else None
        return size if type(size) is int and size > 0 else None

    def text_field(self, key: str) -> str | None:
        """備註裡的文字欄；不存在或不是非空字串回 None."""
        value = (self.metadata or {}).get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None
