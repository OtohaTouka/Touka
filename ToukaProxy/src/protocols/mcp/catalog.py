"""MCP catalog 分頁、URI、MIME 與 payload 邊界（ADR-0203）。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

MAX_CATALOG_PAGES = 32
MAX_CATALOG_ITEMS = 1000
MAX_CATALOG_PAGE_ITEMS = 250
MAX_CATALOG_ITEM_BYTES = 512 * 1024
MAX_CATALOG_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_CURSOR_LENGTH = 1024
MAX_CATALOG_NAME_LENGTH = 256
MCP_LIST_TIMEOUT_SECONDS = 10.0

MAX_RESOURCE_URI_LENGTH = 2048
MAX_RESOURCE_CONTENT_ITEMS = 16
MAX_RESOURCE_TEXT_BYTES = 512 * 1024
MAX_RESOURCE_BLOB_BYTES = 2 * 1024 * 1024
MAX_RESOURCE_TOTAL_BYTES = 2 * 1024 * 1024
MAX_RESOURCE_SERIALIZED_BYTES = 4 * 1024 * 1024
MAX_PROMPT_MESSAGES = 64
MAX_PROMPT_ARGUMENTS = 64
MAX_PROMPT_ARGUMENT_NAME_LENGTH = 128
MAX_PROMPT_ARGUMENT_VALUE_BYTES = 8 * 1024
MAX_PROMPT_TEXT_BYTES = 128 * 1024
MAX_PROMPT_TOTAL_BYTES = 512 * 1024
MAX_MIME_LENGTH = 200

_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")
_MIME_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class McpPaginationError(ValueError):
    """對端 cursor 或 catalog payload 違反 P5 上限。"""


class McpContentLimitError(ValueError):
    """resource/prompt payload 違反 P5 上限或編碼契約。"""


def validate_catalog_name(value: Any, *, kind: str) -> str:
    """驗證對端可顯示/傳回的名稱，不接受 Pydantic 前的隱式字串化。"""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_CATALOG_NAME_LENGTH
        or _CONTROL_RE.search(value)
    ):
        raise McpContentLimitError(f"Invalid MCP {kind} name.")
    return value


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True, exclude_none=True)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {
            str(key): _jsonable(item)
            for key, item in attributes.items()
            if not str(key).startswith("_")
        }
    return str(value)


def _encoded_json_size(value: Any) -> int:
    try:
        return len(
            json.dumps(
                _jsonable(value),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise McpPaginationError("MCP catalog item is not JSON serializable.") from exc


async def collect_paginated(
    fetch_page: Callable[..., Awaitable[Any]],
    *,
    item_field: str,
) -> list[Any]:
    """走完 MCP cursor，同時執行 page/item/aggregate/loop 上限。"""
    items: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    payload_bytes = 0

    for page_index in range(MAX_CATALOG_PAGES):
        request = fetch_page() if cursor is None else fetch_page(cursor=cursor)
        page = await asyncio.wait_for(
            request,
            timeout=MCP_LIST_TIMEOUT_SECONDS,
        )
        page_items = getattr(page, item_field, None)
        if not isinstance(page_items, list):
            raise McpPaginationError(f"MCP catalog page is missing list field {item_field!r}.")
        if len(page_items) > MAX_CATALOG_PAGE_ITEMS:
            raise McpPaginationError("MCP catalog exceeded the per-page item limit.")
        if len(items) + len(page_items) > MAX_CATALOG_ITEMS:
            raise McpPaginationError("MCP catalog exceeded the aggregate item limit.")

        for item in page_items:
            item_bytes = _encoded_json_size(item)
            if item_bytes > MAX_CATALOG_ITEM_BYTES:
                raise McpPaginationError("MCP catalog item exceeded the payload limit.")
            payload_bytes += item_bytes
            if payload_bytes > MAX_CATALOG_PAYLOAD_BYTES:
                raise McpPaginationError("MCP catalog exceeded the aggregate payload limit.")
            items.append(item)

        next_cursor = getattr(page, "next_cursor", None)
        if next_cursor is None:
            return items
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or len(next_cursor) > MAX_CURSOR_LENGTH
        ):
            raise McpPaginationError("MCP catalog returned an invalid cursor.")
        if next_cursor in seen_cursors:
            raise McpPaginationError("MCP catalog cursor loop detected.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

        if page_index == MAX_CATALOG_PAGES - 1:
            break
    raise McpPaginationError("MCP catalog exceeded the page limit.")


def validate_resource_uri(uri: str) -> str:
    """只接受絕對、非 file URI；永遠不把 URI 交給本機 filesystem。"""
    if (
        not isinstance(uri, str)
        or not uri
        or len(uri) > MAX_RESOURCE_URI_LENGTH
        or _CONTROL_RE.search(uri)
        or "\\" in uri
        or _WINDOWS_PATH_RE.match(uri)
    ):
        raise McpContentLimitError("Invalid MCP resource URI.")
    parsed = urlsplit(uri)
    if not parsed.scheme or not _SCHEME_RE.fullmatch(parsed.scheme):
        raise McpContentLimitError("Relative MCP resource URI is not allowed.")
    if parsed.scheme.lower() == "file":
        raise McpContentLimitError("file: MCP resource URI is not allowed.")
    return uri


def validate_resource_template_uri(uri_template: str) -> str:
    """resource template 同樣不得指向 file/相對路徑。"""
    return validate_resource_uri(uri_template)


def normalize_mime_type(
    mime_type: str | None,
    *,
    default: str,
) -> str:
    """驗 MIME token，且文字編碼只接受 UTF-8/ASCII。"""
    value = mime_type or default
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_MIME_LENGTH
        or _CONTROL_RE.search(value)
    ):
        raise McpContentLimitError("Invalid MCP MIME type.")
    parts = [part.strip() for part in value.split(";")]
    media_type = parts[0].lower()
    if not _MIME_RE.fullmatch(media_type):
        raise McpContentLimitError("Invalid MCP MIME type.")
    normalized_parameters: list[str] = []
    for parameter in parts[1:]:
        if "=" not in parameter:
            raise McpContentLimitError("Invalid MCP MIME parameter.")
        name, parameter_value = (part.strip() for part in parameter.split("=", 1))
        if not name or not parameter_value or _CONTROL_RE.search(parameter_value):
            raise McpContentLimitError("Invalid MCP MIME parameter.")
        if name.lower() == "charset":
            charset = parameter_value.strip("\"'").lower()
            if charset not in {"utf-8", "utf8", "us-ascii", "ascii"}:
                raise McpContentLimitError("Unsupported MCP text encoding.")
            parameter_value = "utf-8" if charset in {"utf-8", "utf8"} else "us-ascii"
        normalized_parameters.append(f"{name.lower()}={parameter_value}")
    return "; ".join([media_type, *normalized_parameters])


def validate_text_payload(text: Any, *, limit: int) -> tuple[str, int]:
    if not isinstance(text, str):
        raise McpContentLimitError("MCP text payload must be a string.")
    encoded_size = len(text.encode("utf-8"))
    if encoded_size > limit:
        raise McpContentLimitError("MCP text payload exceeded the UTF-8 byte limit.")
    return text, encoded_size


def validate_base64_payload(data: Any, *, limit: int) -> tuple[str, int]:
    if not isinstance(data, str):
        raise McpContentLimitError("MCP binary payload must be a base64 string.")
    if len(data) > ((limit + 2) // 3) * 4 + 4:
        raise McpContentLimitError("MCP base64 payload exceeded the encoded limit.")
    try:
        decoded = base64.b64decode(data.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise McpContentLimitError("Invalid MCP base64 payload.") from exc
    if len(decoded) > limit:
        raise McpContentLimitError("MCP binary payload exceeded the decoded limit.")
    return data, len(decoded)


def validate_prompt_arguments(
    arguments: dict[str, str] | None,
) -> dict[str, str] | None:
    if arguments is None:
        return None
    if not isinstance(arguments, dict) or len(arguments) > MAX_PROMPT_ARGUMENTS:
        raise McpContentLimitError("Invalid MCP prompt arguments.")
    validated: dict[str, str] = {}
    total_bytes = 0
    for name, value in arguments.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > MAX_PROMPT_ARGUMENT_NAME_LENGTH
            or _CONTROL_RE.search(name)
            or not isinstance(value, str)
        ):
            raise McpContentLimitError("MCP prompt arguments must be bounded strings.")
        value_bytes = len(value.encode("utf-8"))
        if value_bytes > MAX_PROMPT_ARGUMENT_VALUE_BYTES:
            raise McpContentLimitError("MCP prompt argument exceeded the byte limit.")
        total_bytes += value_bytes
        if total_bytes > MAX_PROMPT_TOTAL_BYTES:
            raise McpContentLimitError("MCP prompt arguments exceeded the total limit.")
        validated[name] = value
    return validated
