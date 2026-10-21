"""MCP JSON Schema 2020-12 驗證與資源上限（ADR-0203）。"""

from __future__ import annotations

import copy
import json
from typing import Any
from urllib.parse import unquote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from src.utils.time_utils import get_monotonic_ms

DEFAULT_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_SUPPORTED_SCHEMA_DIALECTS = frozenset(
    {
        DEFAULT_SCHEMA_DIALECT,
        f"{DEFAULT_SCHEMA_DIALECT}#",
    }
)
MAX_SCHEMA_DEPTH = 32
MAX_SCHEMA_SUBSCHEMAS = 512
MAX_SCHEMA_BYTES = 256 * 1024
MAX_SCHEMA_VALIDATION_MS = 100.0


class McpSchemaError(ValueError):
    """MCP schema 不符合 Touka P5 安全合約。"""


class McpSchemaDialectError(McpSchemaError):
    """對端明示 Touka 不支援的 schema dialect。"""

    def __init__(self, dialect: object) -> None:
        super().__init__(f"Unsupported MCP JSON Schema dialect: {dialect!r}")


class McpSchemaReferenceError(McpSchemaError):
    """schema 含外部或無法解析的本地 reference。"""

    def __init__(self, reference: object) -> None:
        super().__init__(f"External or unresolved MCP JSON Schema reference: {reference!r}")


class McpSchemaLimitError(McpSchemaError):
    """schema 超過深度、子結構、bytes 或驗證時間上限。"""

    def __init__(self, limit: str) -> None:
        super().__init__(f"MCP JSON Schema exceeded the {limit} limit.")


class McpSchemaShapeError(McpSchemaError):
    """schema JSON 型別或 input root 不合規。"""


_ELICITATION_PRIMITIVE_TYPES = frozenset(
    {"string", "number", "integer", "boolean"}
)


def _walk_schema(schema: dict[str, Any]) -> tuple[set[str], list[str]]:
    """一次走訪收集 anchors/refs，並在昂貴 validator 前先限縮複雜度。"""
    anchors: set[str] = set()
    references: list[str] = []
    count = 0
    stack: list[tuple[Any, int]] = [(schema, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > MAX_SCHEMA_DEPTH:
            raise McpSchemaLimitError("depth")
        if isinstance(value, dict):
            count += 1
            if count > MAX_SCHEMA_SUBSCHEMAS:
                raise McpSchemaLimitError("subschema")
            for anchor_key in ("$anchor", "$dynamicAnchor"):
                anchor = value.get(anchor_key)
                if isinstance(anchor, str):
                    anchors.add(anchor)
            for reference_key in ("$ref", "$dynamicRef"):
                reference = value.get(reference_key)
                if reference is not None:
                    if not isinstance(reference, str):
                        raise McpSchemaReferenceError(reference)
                    references.append(reference)
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            count += 1
            if count > MAX_SCHEMA_SUBSCHEMAS:
                raise McpSchemaLimitError("subschema")
            stack.extend((child, depth + 1) for child in value)
    return anchors, references


def _resolve_json_pointer(document: dict[str, Any], reference: str) -> bool:
    fragment = unquote(reference[1:])
    if fragment == "":
        return True
    if not fragment.startswith("/"):
        return False
    current: Any = document
    for raw_part in fragment[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdecimal():
            index = int(part)
            if index < len(current):
                current = current[index]
                continue
        return False
    return True


def _validate_references(
    schema: dict[str, Any],
    *,
    anchors: set[str],
    references: list[str],
) -> None:
    for reference in references:
        if not reference.startswith("#"):
            # P5 預設完全不取 external `$ref`；不只擋 HTTP，也擋 URN/檔案等外部來源。
            raise McpSchemaReferenceError(reference)
        fragment = unquote(reference[1:])
        if fragment and not fragment.startswith("/") and fragment not in anchors:
            raise McpSchemaReferenceError(reference)
        if fragment.startswith("/") and not _resolve_json_pointer(schema, reference):
            raise McpSchemaReferenceError(reference)


def normalize_mcp_schema(
    schema: dict[str, Any],
    *,
    require_object_root: bool,
) -> dict[str, Any]:
    """驗證並回傳有明確 2020-12 dialect 的獨立 schema 副本。

    ``inputSchema`` 必須以 object 為 root；``outputSchema`` 的資料 root 不受限制。
    驗證前先執行 depth/subschema/bytes 上限，因此 `jsonschema` 不會面對未設限的對端輸入。
    """
    if not isinstance(schema, dict):
        msg = "MCP JSON Schema must be an object."
        raise McpSchemaShapeError(msg)

    anchors, references = _walk_schema(schema)
    dialect = schema.get("$schema", DEFAULT_SCHEMA_DIALECT)
    if not isinstance(dialect, str) or dialect not in _SUPPORTED_SCHEMA_DIALECTS:
        raise McpSchemaDialectError(dialect)
    if require_object_root and schema.get("type") != "object":
        msg = "MCP inputSchema root must declare type='object'."
        raise McpSchemaShapeError(msg)

    try:
        encoded = json.dumps(
            schema,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        msg = "MCP JSON Schema contains a non-JSON value."
        raise McpSchemaShapeError(msg) from exc
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise McpSchemaLimitError("encoded-size")

    normalized = copy.deepcopy(schema)
    normalized["$schema"] = DEFAULT_SCHEMA_DIALECT
    _validate_references(
        normalized,
        anchors=anchors,
        references=references,
    )

    started_ms = get_monotonic_ms()
    try:
        Draft202012Validator.check_schema(normalized)
    except SchemaError as exc:
        msg = f"Invalid MCP JSON Schema: {exc.message}"
        raise McpSchemaShapeError(msg) from exc
    if get_monotonic_ms() - started_ms > MAX_SCHEMA_VALIDATION_MS:
        raise McpSchemaLimitError("validation-time")
    return normalized


def normalize_mcp_elicitation_schema(
    schema: dict[str, Any],
) -> dict[str, Any]:
    """限縮為 ToukaChat 能無損呈現的扁平 primitive form schema。"""
    normalized = normalize_mcp_schema(schema, require_object_root=True)
    properties = normalized.get("properties", {})
    if not isinstance(properties, dict):
        msg = "MCP elicitation schema properties must be an object."
        raise McpSchemaShapeError(msg)

    required = normalized.get("required", [])
    if not isinstance(required, list) or not all(
        isinstance(name, str) and name in properties for name in required
    ):
        msg = "MCP elicitation schema required must reference declared properties."
        raise McpSchemaShapeError(msg)

    for name, property_schema in properties.items():
        if not isinstance(name, str) or not isinstance(property_schema, dict):
            msg = "MCP elicitation properties must be named schema objects."
            raise McpSchemaShapeError(msg)
        property_type = property_schema.get("type")
        if property_type not in _ELICITATION_PRIMITIVE_TYPES:
            msg = (
                f"MCP elicitation property {name!r} uses unsupported "
                f"type {property_type!r}."
            )
            raise McpSchemaShapeError(msg)
        if any(
            keyword in property_schema
            for keyword in ("$ref", "$dynamicRef", "allOf", "anyOf", "oneOf", "not")
        ):
            msg = f"MCP elicitation property {name!r} uses an unsupported composition."
            raise McpSchemaShapeError(msg)
        enum_values = property_schema.get("enum")
        if enum_values is not None and (
            not isinstance(enum_values, list)
            or not enum_values
            or any(
                not isinstance(value, (str, int, float, bool))
                for value in enum_values
            )
        ):
            msg = f"MCP elicitation property {name!r} has an unsupported enum."
            raise McpSchemaShapeError(msg)
    return normalized
