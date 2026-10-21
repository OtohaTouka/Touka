"""合併 Proxy 與 Heartbeat 的 OpenAPI 契約（純函式，不載入產品程式）.

入口 ``update_openapi.py`` 先開沙盒、載入兩個 app 後呼叫本檔；測試直接匯入本檔，
不必經過會開沙盒的入口（禁令 63）。
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from fastapi.encoders import jsonable_encoder

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pydantic import BaseModel


class OpenAPIMergeConflict(ValueError):
    """A duplicate public name must not silently acquire a different contract."""

    def __init__(self, kind: str, name: str) -> None:
        super().__init__(f"Conflicting OpenAPI {kind}: {name}")


def merge_schemas(
    proxy: dict[str, Any],
    heartbeat: dict[str, Any],
    *,
    transport_models: Iterable[type[BaseModel]],
) -> dict[str, Any]:
    """Merge service contracts only when shared names describe identical schemas."""
    merged = copy.deepcopy(proxy)
    for path, methods in heartbeat.get("paths", {}).items():
        if path in merged["paths"] and merged["paths"][path] != methods:
            raise OpenAPIMergeConflict("path", path)
        merged["paths"][path] = methods
    schemas = merged.setdefault("components", {}).setdefault("schemas", {})
    for name, definition in heartbeat.get("components", {}).get("schemas", {}).items():
        if name in schemas and schemas[name] != definition:
            raise OpenAPIMergeConflict("schema", name)
        schemas[name] = definition
    # SSE 合約沒有額外假端點；生成器與同步測試使用同一份領域模型註冊。
    for model in transport_models:
        definition = jsonable_encoder(
            model.model_json_schema(ref_template="#/components/schemas/{model}"), exclude_none=True
        )
        definitions = definition.pop("$defs", {})
        definitions[model.__name__] = definition
        for name, item in definitions.items():
            if name in schemas and schemas[name] != item:
                raise OpenAPIMergeConflict("schema", name)
            schemas[name] = item
    return merged
