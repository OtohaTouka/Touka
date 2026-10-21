"""Python MCP SDK v2 → Touka canonical boundary adapter（ADR-0199）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.shared.exceptions import MCPError
from memory_server.utils.content_guard import contains_prompt_injection
from src.protocols.mcp.canonical import (
    McpAudioPromptContent,
    McpBlobResourceContent,
    McpCompleteOutcome,
    McpEmbeddedResourcePromptContent,
    McpErrorKind,
    McpImagePromptContent,
    McpPromptArgument,
    McpPromptDescriptor,
    McpPromptMessage,
    McpPromptResult,
    McpResourceDescriptor,
    McpResourceLinkPromptContent,
    McpResourceReadResult,
    McpResourceTemplateDescriptor,
    McpTextPromptContent,
    McpTextResourceContent,
    McpToolDescriptor,
)
from src.protocols.mcp.catalog import (
    MAX_PROMPT_ARGUMENTS,
    MAX_PROMPT_MESSAGES,
    MAX_PROMPT_TEXT_BYTES,
    MAX_PROMPT_TOTAL_BYTES,
    MAX_RESOURCE_BLOB_BYTES,
    MAX_RESOURCE_CONTENT_ITEMS,
    MAX_RESOURCE_SERIALIZED_BYTES,
    MAX_RESOURCE_TEXT_BYTES,
    MAX_RESOURCE_TOTAL_BYTES,
    McpContentLimitError,
    normalize_mime_type,
    validate_base64_payload,
    validate_catalog_name,
    validate_resource_template_uri,
    validate_resource_uri,
    validate_text_payload,
)
from src.protocols.mcp.error_mapping import map_mcp_error
from src.utils.secrets import redact_secrets_from_content

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.protocols.mcp.canonical import McpCanonicalError

UNKNOWN_PROTOCOL_VERSION = "unknown"


def _dump(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(exclude_none=True, by_alias=True)
        return dumped if isinstance(dumped, dict) else None
    return None


def _field(value: Any, name: str, default: Any = None) -> Any:
    """同時讀取 SDK model attribute 與 ToukaIDE bridge dict。"""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _field_any(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        marker = object()
        result = _field(value, name, marker)
        if result is not marker:
            return result
    return default


def _field_is_present(value: Any, name: str) -> bool:
    """區分欄位缺席與 JSON null；兩者在 Python 值皆會是 ``None``。"""
    if isinstance(value, dict):
        return name in value
    fields_set = getattr(value, "model_fields_set", None)
    if fields_set is not None:
        return name in fields_set
    return hasattr(value, name)


def _icons(value: Any) -> list[dict[str, Any]]:
    return [
        dumped for icon in (_field(value, "icons") or []) if (dumped := _dump(icon)) is not None
    ]


def _validated_size(value: Any) -> int | None:
    size = _field(value, "size")
    if size is None:
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise McpContentLimitError("Invalid MCP resource size.")
    if size > MAX_RESOURCE_BLOB_BYTES:
        raise McpContentLimitError("MCP resource descriptor exceeded the size limit.")
    return size


def adapt_tool_descriptor(
    tool: Any,
    *,
    source_protocol_version: str = UNKNOWN_PROTOCOL_VERSION,
) -> McpToolDescriptor:
    """把 v2 snake_case Tool 轉為 SDK-neutral descriptor。"""
    return McpToolDescriptor(
        source_protocol_version=source_protocol_version,
        name=validate_catalog_name(_field(tool, "name"), kind="tool"),
        title=getattr(tool, "title", None),
        description=getattr(tool, "description", None) or "",
        input_schema=getattr(tool, "input_schema", None) or {},
        output_schema=getattr(tool, "output_schema", None),
        annotations=_dump(getattr(tool, "annotations", None)),
        icons=_icons(tool),
        meta=_dump(getattr(tool, "meta", None)),
    )


def adapt_resource_descriptor(
    resource: Any,
    *,
    source_protocol_version: str = UNKNOWN_PROTOCOL_VERSION,
) -> McpResourceDescriptor:
    """把 SDK Resource 轉為 URI/MIME 已驗證的 canonical descriptor。"""
    mime_type = _field_any(resource, "mime_type", "mimeType")
    return McpResourceDescriptor(
        source_protocol_version=source_protocol_version,
        name=validate_catalog_name(_field(resource, "name"), kind="resource"),
        title=_field(resource, "title"),
        uri=validate_resource_uri(str(_field(resource, "uri"))),
        description=_field(resource, "description") or "",
        mime_type=(
            normalize_mime_type(mime_type, default="application/octet-stream")
            if mime_type is not None
            else None
        ),
        size=_validated_size(resource),
        icons=_icons(resource),
        annotations=_dump(_field(resource, "annotations")),
        meta=_dump(_field(resource, "meta")),
    )


def adapt_resource_template_descriptor(
    template: Any,
    *,
    source_protocol_version: str = UNKNOWN_PROTOCOL_VERSION,
) -> McpResourceTemplateDescriptor:
    """把 SDK ResourceTemplate 轉為 canonical descriptor。"""
    mime_type = _field_any(template, "mime_type", "mimeType")
    return McpResourceTemplateDescriptor(
        source_protocol_version=source_protocol_version,
        name=validate_catalog_name(
            _field(template, "name"),
            kind="resource template",
        ),
        title=_field(template, "title"),
        uri_template=validate_resource_template_uri(
            str(_field_any(template, "uri_template", "uriTemplate"))
        ),
        description=_field(template, "description") or "",
        mime_type=(
            normalize_mime_type(mime_type, default="application/octet-stream")
            if mime_type is not None
            else None
        ),
        icons=_icons(template),
        annotations=_dump(_field(template, "annotations")),
        meta=_dump(_field(template, "meta")),
    )


def adapt_prompt_descriptor(
    prompt: Any,
    *,
    source_protocol_version: str = UNKNOWN_PROTOCOL_VERSION,
) -> McpPromptDescriptor:
    """把 SDK Prompt 轉為強型別 canonical descriptor。"""
    raw_arguments = _field(prompt, "arguments") or []
    if not isinstance(raw_arguments, list) or len(raw_arguments) > MAX_PROMPT_ARGUMENTS:
        raise McpContentLimitError("MCP prompt exceeded the argument limit.")
    arguments = [
        McpPromptArgument(
            name=validate_catalog_name(
                _field(argument, "name"),
                kind="prompt argument",
            ),
            title=_field(argument, "title"),
            description=_field(argument, "description") or "",
            required=bool(_field(argument, "required", False)),
        )
        for argument in raw_arguments
    ]
    return McpPromptDescriptor(
        source_protocol_version=source_protocol_version,
        name=validate_catalog_name(_field(prompt, "name"), kind="prompt"),
        title=_field(prompt, "title"),
        description=_field(prompt, "description") or "",
        arguments=arguments,
        icons=_icons(prompt),
        meta=_dump(_field(prompt, "meta")),
    )


def adapt_resource_content(value: Any) -> McpTextResourceContent | McpBlobResourceContent:
    """驗證單一 TextResourceContents／BlobResourceContents。"""
    uri = validate_resource_uri(str(_field(value, "uri")))
    mime_type = _field_any(value, "mime_type", "mimeType")
    meta = _dump(_field(value, "meta"))
    if _field_is_present(value, "text"):
        normalized_mime = normalize_mime_type(
            mime_type,
            default="text/plain; charset=utf-8",
        )
        text, encoded_size = validate_text_payload(
            _field(value, "text"),
            limit=MAX_RESOURCE_TEXT_BYTES,
        )
        injection_flagged = contains_prompt_injection(text)
        redacted, dlp_redacted = redact_secrets_from_content(text)
        return McpTextResourceContent(
            uri=uri,
            mime_type=normalized_mime,
            text=redacted,
            encoded_size=len(redacted.encode("utf-8")) if dlp_redacted else encoded_size,
            injection_flagged=injection_flagged,
            dlp_redacted=dlp_redacted,
            meta=meta,
        )
    if _field_is_present(value, "blob"):
        normalized_mime = normalize_mime_type(
            mime_type,
            default="application/octet-stream",
        )
        blob, decoded_size = validate_base64_payload(
            _field(value, "blob"),
            limit=MAX_RESOURCE_BLOB_BYTES,
        )
        return McpBlobResourceContent(
            uri=uri,
            mime_type=normalized_mime,
            blob=blob,
            decoded_size=decoded_size,
            meta=meta,
        )
    raise McpContentLimitError("Unsupported MCP resource content type.")


def adapt_read_resource_result(
    result: Any,
    *,
    source_protocol_version: str = UNKNOWN_PROTOCOL_VERSION,
) -> McpResourceReadResult:
    """驗證 resources/read 的 item/aggregate bytes 與 DLP 狀態。"""
    raw_contents = _field(result, "contents") or []
    if not isinstance(raw_contents, list) or len(raw_contents) > MAX_RESOURCE_CONTENT_ITEMS:
        raise McpContentLimitError("MCP resource exceeded the content-item limit.")
    contents = [adapt_resource_content(item) for item in raw_contents]
    total_bytes = sum(
        item.encoded_size if isinstance(item, McpTextResourceContent) else item.decoded_size
        for item in contents
    )
    if total_bytes > MAX_RESOURCE_TOTAL_BYTES:
        raise McpContentLimitError("MCP resource exceeded the aggregate byte limit.")
    adapted = McpResourceReadResult(
        source_protocol_version=source_protocol_version,
        contents=contents,
        injection_flagged=any(
            isinstance(item, McpTextResourceContent) and item.injection_flagged for item in contents
        ),
        dlp_redacted=any(
            isinstance(item, McpTextResourceContent) and item.dlp_redacted for item in contents
        ),
        meta=_dump(_field(result, "meta")),
    )
    if len(adapted.model_dump_json(by_alias=True).encode("utf-8")) > (
        MAX_RESOURCE_SERIALIZED_BYTES
    ):
        raise McpContentLimitError("MCP resource exceeded the serialized payload limit.")
    return adapted


def _adapt_prompt_content(
    value: Any,
) -> (
    McpTextPromptContent
    | McpImagePromptContent
    | McpAudioPromptContent
    | McpResourceLinkPromptContent
    | McpEmbeddedResourcePromptContent
):
    content_type = _field(value, "type")
    annotations = _dump(_field(value, "annotations"))
    meta = _dump(_field(value, "meta"))
    if content_type == "text":
        text, _encoded_size = validate_text_payload(
            _field(value, "text"),
            limit=MAX_PROMPT_TEXT_BYTES,
        )
        injection_flagged = contains_prompt_injection(text)
        redacted, dlp_redacted = redact_secrets_from_content(text)
        return McpTextPromptContent(
            text=redacted,
            injection_flagged=injection_flagged,
            dlp_redacted=dlp_redacted,
            annotations=annotations,
            meta=meta,
        )
    if content_type in {"image", "audio"}:
        mime_type = normalize_mime_type(
            _field_any(value, "mime_type", "mimeType"),
            default="application/octet-stream",
        )
        data, decoded_size = validate_base64_payload(
            _field(value, "data"),
            limit=MAX_RESOURCE_BLOB_BYTES,
        )
        expected_prefix = f"{content_type}/"
        if not mime_type.startswith(expected_prefix):
            raise McpContentLimitError(
                f"MCP {content_type} content requires an {expected_prefix} MIME type."
            )
        content_class = McpImagePromptContent if content_type == "image" else McpAudioPromptContent
        return content_class(
            data=data,
            mime_type=mime_type,
            decoded_size=decoded_size,
            annotations=annotations,
            meta=meta,
        )
    if content_type == "resource_link":
        mime_type = _field_any(value, "mime_type", "mimeType")
        return McpResourceLinkPromptContent(
            name=validate_catalog_name(
                _field(value, "name"),
                kind="resource link",
            ),
            title=_field(value, "title"),
            uri=validate_resource_uri(str(_field(value, "uri"))),
            description=_field(value, "description") or "",
            mime_type=(
                normalize_mime_type(mime_type, default="application/octet-stream")
                if mime_type is not None
                else None
            ),
            size=_validated_size(value),
            icons=_icons(value),
            annotations=annotations,
            meta=meta,
        )
    if content_type == "resource":
        return McpEmbeddedResourcePromptContent(
            resource=adapt_resource_content(_field(value, "resource")),
            annotations=annotations,
            meta=meta,
        )
    raise McpContentLimitError("Unsupported MCP prompt content type.")


def adapt_get_prompt_result(
    result: Any,
    *,
    source_protocol_version: str = UNKNOWN_PROTOCOL_VERSION,
) -> McpPromptResult:
    """驗證 prompts/get 訊息 union，並強制標記 untrusted。"""
    raw_messages = _field(result, "messages") or []
    if not isinstance(raw_messages, list) or len(raw_messages) > MAX_PROMPT_MESSAGES:
        raise McpContentLimitError("MCP prompt exceeded the message limit.")
    messages: list[McpPromptMessage] = []
    total_bytes = 0
    for raw_message in raw_messages:
        role = _field(raw_message, "role")
        if role not in {"user", "assistant"}:
            raise McpContentLimitError("Invalid MCP prompt message role.")
        content = _adapt_prompt_content(_field(raw_message, "content"))
        total_bytes += len(
            content.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        )
        if total_bytes > MAX_PROMPT_TOTAL_BYTES:
            raise McpContentLimitError("MCP prompt exceeded the aggregate byte limit.")
        messages.append(McpPromptMessage(role=role, content=content))

    def _content_flags(
        content: (
            McpTextPromptContent
            | McpImagePromptContent
            | McpAudioPromptContent
            | McpResourceLinkPromptContent
            | McpEmbeddedResourcePromptContent
        ),
    ) -> tuple[bool, bool]:
        if isinstance(content, McpTextPromptContent):
            return content.injection_flagged, content.dlp_redacted
        if isinstance(content, McpEmbeddedResourcePromptContent) and isinstance(
            content.resource,
            McpTextResourceContent,
        ):
            return (
                content.resource.injection_flagged,
                content.resource.dlp_redacted,
            )
        return False, False

    flags = [_content_flags(message.content) for message in messages]
    adapted = McpPromptResult(
        source_protocol_version=source_protocol_version,
        description=_field(result, "description") or "",
        messages=messages,
        injection_flagged=any(injection for injection, _redacted in flags),
        dlp_redacted=any(redacted for _injection, redacted in flags),
        meta=_dump(_field(result, "meta")),
    )
    if len(adapted.model_dump_json(by_alias=True).encode("utf-8")) > (MAX_PROMPT_TOTAL_BYTES):
        raise McpContentLimitError("MCP prompt exceeded the serialized payload limit.")
    return adapted


def adapt_call_tool_result(
    result: Any,
    *,
    source_protocol_version: str = UNKNOWN_PROTOCOL_VERSION,
) -> McpCompleteOutcome:
    """保存 v2 CallToolResult 的任意 JSON structured content。"""
    content = [
        dumped for item in (_field(result, "content") or []) if (dumped := _dump(item)) is not None
    ]
    structured = _field(result, "structured_content")
    return McpCompleteOutcome(
        source_protocol_version=source_protocol_version,
        content=content,
        structured_content=structured,
        structured_content_present=_field_is_present(
            result,
            "structured_content",
        ),
        is_error=bool(_field(result, "is_error", False)),
        meta=_dump(_field(result, "meta")),
    )


def adapt_call_exception(
    exc: BaseException,
    *,
    sanitize: Callable[[str], str],
    source_protocol_version: str = UNKNOWN_PROTOCOL_VERSION,
) -> McpCanonicalError:
    """分流 SDK v2 JSON-RPC error 與 transport/runtime error。"""
    if isinstance(exc, MCPError):
        return map_mcp_error(
            McpErrorKind.PROTOCOL,
            sanitize(exc.message),
            source_protocol_version=source_protocol_version,
            protocol_code=exc.code,
            protocol_data=exc.data,
        )
    return map_mcp_error(
        McpErrorKind.TRANSPORT,
        sanitize(str(exc)),
        source_protocol_version=source_protocol_version,
    )
