"""Qdrant 本機模式的 meta.json：從快照拿掉一個 collection 時同步改清單.

meta.json 是 ``{"collections": {名稱: 設定}, "aliases": {別名: 名稱}}``。Pydantic 只用來驗證格式，
實際修改在原始 dict 上做（其他鍵值原樣保留）。Touka 不用 alias，
有 alias 就交給人判斷（fail closed）。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from src.services.memory_scrub.swap import ScrubItemFailure


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


def _load(meta: bytes) -> dict[str, Any]:
    try:
        QdrantLocalMeta.model_validate_json(meta)
        raw = json.loads(meta)
    except (ValidationError, ValueError) as exc:
        raise ScrubItemFailure("unknown_qdrant_meta") from exc
    if raw["aliases"]:
        raise ScrubItemFailure("unknown_qdrant_meta")
    return raw


def collection_names(meta: bytes) -> set[str]:
    """meta.json 列出的 collection 名稱（格式不認得就丟 unknown_qdrant_meta）."""
    return set(_load(meta)["collections"])


def without_collection(meta: bytes, name: str) -> bytes:
    """拿掉一個 collection 之後的 meta.json（和 Qdrant 自己寫的格式一樣：預設分隔符）."""
    raw = _load(meta)
    raw["collections"].pop(name, None)
    return json.dumps(raw).encode("utf-8")
