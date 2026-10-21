"""Safe MCP icon ingestion and credential-free same-origin fetch."""

from __future__ import annotations

import base64
import binascii
import struct
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx2

MAX_ICON_COUNT = 8
MAX_ICON_BYTES = 256 * 1024
MAX_ICON_DIMENSION = 1024
MAX_ICON_PIXELS = 1024 * 1024
_ICON_ACCEPT = "image/png, image/jpeg"
_ALLOWED_MIME = frozenset({"image/png", "image/jpeg"})

IconFetcher = Callable[
    [str, dict[str, str]],
    Awaitable[tuple[int, str, bytes]],
]


@dataclass(frozen=True)
class McpIconIssue:
    """One rejected optional icon; the parent descriptor remains available."""

    code: str
    message: str


class McpIconService:
    """Turn untrusted icons into verified data URIs safe for frontend display."""

    def __init__(self, *, fetch: IconFetcher | None = None) -> None:
        self._fetch = fetch or self._fetch_remote

    async def sanitize_icons(
        self,
        icons: list[dict[str, Any]],
        *,
        server_url: str | None,
    ) -> tuple[list[dict[str, Any]], list[McpIconIssue]]:
        """Validate, fetch and rewrite icons as bounded verified data URIs."""
        accepted: list[dict[str, Any]] = []
        issues: list[McpIconIssue] = []
        for icon in icons[:MAX_ICON_COUNT]:
            src = icon.get("src") if isinstance(icon, Mapping) else None
            if not isinstance(src, str):
                issues.append(McpIconIssue("invalid_src", "icon src must be a string"))
                continue
            try:
                mime, payload = await self._load_icon(src, server_url=server_url)
                width, height = _validate_image(payload, mime)
            except _IconRejected as exc:
                issues.append(McpIconIssue(exc.code, str(exc)))
                continue
            safe: dict[str, Any] = {
                "src": f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}",
                "mimeType": mime,
                "sizes": f"{width}x{height}",
            }
            theme = icon.get("theme")
            if theme in {"light", "dark"}:
                safe["theme"] = theme
            accepted.append(safe)
        if len(icons) > MAX_ICON_COUNT:
            issues.append(
                McpIconIssue(
                    "too_many_icons",
                    f"icon count exceeds {MAX_ICON_COUNT}",
                )
            )
        return accepted, issues

    async def _load_icon(
        self,
        src: str,
        *,
        server_url: str | None,
    ) -> tuple[str, bytes]:
        if src.startswith("data:"):
            return _decode_data_uri(src)
        parsed = urlsplit(src)
        if parsed.scheme.lower() != "https":
            raise _IconRejected("invalid_scheme", "icon URL must use HTTPS or data")
        if parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise _IconRejected(
                "unsafe_url",
                "icon URL must not contain credentials, fragment or query",
            )
        if server_url is None or _origin(src) != _origin(server_url):
            raise _IconRejected("cross_origin", "icon URL must be same-origin")
        status, content_type, payload = await self._fetch(
            src,
            {"Accept": _ICON_ACCEPT},
        )
        if 300 <= status < 400:
            raise _IconRejected("redirect", "icon redirects are not followed")
        if status != 200:
            raise _IconRejected("fetch_failed", "icon fetch did not return 200")
        mime = content_type.partition(";")[0].strip().lower()
        return mime, payload

    @staticmethod
    async def _fetch_remote(
        url: str,
        headers: dict[str, str],
    ) -> tuple[int, str, bytes]:
        timeout = httpx2.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
        async with (
            httpx2.AsyncClient(
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client,
            client.stream("GET", url) as response,
        ):
                content_length = response.headers.get("content-length")
                if content_length is not None and int(content_length) > MAX_ICON_BYTES:
                    raise _IconRejected(
                        "too_large",
                        f"icon bytes exceed {MAX_ICON_BYTES}",
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_ICON_BYTES:
                        raise _IconRejected(
                            "too_large",
                            f"icon bytes exceed {MAX_ICON_BYTES}",
                        )
                    chunks.append(chunk)
                return (
                    response.status_code,
                    response.headers.get("content-type", ""),
                    b"".join(chunks),
                )


class _IconRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, host, port


def _decode_data_uri(src: str) -> tuple[str, bytes]:
    header, separator, encoded = src.partition(",")
    if not separator or not header.endswith(";base64"):
        raise _IconRejected("invalid_data_uri", "icon data URI must be base64")
    mime = header[5:-7].lower()
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _IconRejected("invalid_data_uri", "invalid icon base64") from exc
    return mime, payload


def _validate_image(payload: bytes, declared_mime: str) -> tuple[int, int]:
    if len(payload) > MAX_ICON_BYTES:
        raise _IconRejected("too_large", f"icon bytes exceed {MAX_ICON_BYTES}")
    if declared_mime not in _ALLOWED_MIME:
        raise _IconRejected("unsupported_mime", "only PNG and JPEG icons are accepted")
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        actual_mime = "image/png"
        if len(payload) < 29 or payload[12:16] != b"IHDR":
            raise _IconRejected("invalid_magic", "invalid PNG header")
        width, height = struct.unpack(">II", payload[16:24])
        if b"acTL" in payload:
            raise _IconRejected("animated", "animated PNG icons are rejected")
    elif payload.startswith(b"\xff\xd8"):
        actual_mime = "image/jpeg"
        width, height = _jpeg_dimensions(payload)
    else:
        raise _IconRejected("invalid_magic", "icon MIME does not match file magic")
    if actual_mime != declared_mime:
        raise _IconRejected("mime_mismatch", "declared icon MIME does not match magic")
    if (
        width <= 0
        or height <= 0
        or width > MAX_ICON_DIMENSION
        or height > MAX_ICON_DIMENSION
        or width * height > MAX_ICON_PIXELS
    ):
        raise _IconRejected("invalid_dimensions", "icon dimensions exceed policy")
    return width, height


def _jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    offset = 2
    while offset + 9 <= len(payload):
        if payload[offset] != 0xFF:
            offset += 1
            continue
        marker = payload[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(payload):
            break
        segment_length = int.from_bytes(payload[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(payload):
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
            width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
            return width, height
        offset += segment_length
    raise _IconRejected("invalid_magic", "JPEG dimensions are missing")
