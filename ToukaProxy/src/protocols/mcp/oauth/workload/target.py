"""Canonical MCP OAuth target bindings shared by admission and egress guards。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field
from src.protocols.mcp.oauth.outbound import (
    OAuthOutboundPolicyError,
    validate_oauth_endpoint,
)

_AMBIGUOUS_PATH_ESCAPE_RE = re.compile(r"%(?:2f|5c|2e)", re.IGNORECASE)


class McpOAuthTargetBindingError(ValueError):
    """The configured or actual MCP endpoint does not match exact authority。"""


def _canonical_host(hostname: str) -> str:
    try:
        canonical = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise McpOAuthTargetBindingError() from exc
    if not canonical or canonical.endswith("."):
        raise McpOAuthTargetBindingError()
    return f"[{canonical}]" if ":" in canonical else canonical


def canonical_mcp_transport_url(value: str) -> str:
    """Return one unambiguous HTTPS transport URL with a canonical origin。"""
    if not value or value != value.strip() or "\\" in value:
        raise McpOAuthTargetBindingError()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise McpOAuthTargetBindingError() from exc
    hostname = parsed.hostname
    path = parsed.path or "/"
    path_segments = path.split("/")
    if (
        parsed.scheme.lower() != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "%" in hostname
        or (port or 443) != 443
        or any(segment in {".", ".."} for segment in path_segments)
        or _AMBIGUOUS_PATH_ESCAPE_RE.search(path) is not None
    ):
        raise McpOAuthTargetBindingError()
    netloc = _canonical_host(hostname)
    return urlunsplit(("https", netloc, path, "", ""))


def _canonical_origin(value: str) -> str:
    try:
        validated = validate_oauth_endpoint(value)
        parsed = urlsplit(validated)
    except (OAuthOutboundPolicyError, ValueError) as exc:
        raise McpOAuthTargetBindingError() from exc
    hostname = parsed.hostname
    if hostname is None:
        raise McpOAuthTargetBindingError()
    return f"https://{_canonical_host(hostname)}"


def resource_and_transport_share_origin(resource: str, transport: str) -> bool:
    """Compare only canonical scheme, IDNA host, and effective port。"""
    try:
        resource_origin = _canonical_origin(resource)
        transport_origin = _canonical_origin(canonical_mcp_transport_url(transport))
    except McpOAuthTargetBindingError:
        return False
    return hmac.compare_digest(resource_origin, transport_origin)


class McpOAuthTargetBinding(BaseModel):
    """Immutable protected-resource and exact-transport authority snapshot。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_resource: str
    expected_transport_url: str
    profile_id: str = Field(min_length=1, max_length=64)
    profile_generation: int = Field(ge=1)
    profile_fingerprint: str = Field(min_length=1, max_length=256)
    installation_id: str = Field(min_length=1, max_length=256)
    installation_generation: int = Field(ge=1)

    def canonical_bytes(self) -> bytes:
        """Return deterministic bytes for ticket, permit, and audit binding。"""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        """Return the opaque target identity safe for durable references。"""
        return f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}"


def build_mcp_oauth_target_binding(
    *,
    expected_resource: str,
    expected_transport_url: str,
    configured_transport_url: str | None,
    profile_id: str,
    profile_generation: int,
    profile_fingerprint: str,
    installation_id: str,
    installation_generation: int,
) -> McpOAuthTargetBinding:
    """Build authority only when profile and installation targets are exact。"""
    expected = canonical_mcp_transport_url(expected_transport_url)
    if configured_transport_url is None:
        raise McpOAuthTargetBindingError()
    configured = canonical_mcp_transport_url(configured_transport_url)
    if not hmac.compare_digest(expected, configured) or not (
        resource_and_transport_share_origin(expected_resource, expected)
    ):
        raise McpOAuthTargetBindingError()
    return McpOAuthTargetBinding(
        expected_resource=expected_resource,
        expected_transport_url=expected,
        profile_id=profile_id,
        profile_generation=profile_generation,
        profile_fingerprint=profile_fingerprint,
        installation_id=installation_id,
        installation_generation=installation_generation,
    )


def target_allows_transport(binding: McpOAuthTargetBinding, actual_url: str) -> bool:
    """Use constant-time comparison after canonicalizing the real wire target。"""
    try:
        actual = canonical_mcp_transport_url(actual_url)
    except McpOAuthTargetBindingError:
        return False
    return hmac.compare_digest(binding.expected_transport_url, actual)
