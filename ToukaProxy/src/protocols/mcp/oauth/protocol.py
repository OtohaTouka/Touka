# ruff: noqa: D102
"""P8-A OAuth authorization-code wire policy and callback parser。"""

from __future__ import annotations

import base64
import hashlib
from enum import StrEnum
from typing import Literal
from urllib.parse import parse_qsl, urlencode

from pydantic import BaseModel, ConfigDict, model_validator
from src.config import (
    OAUTH_CALLBACK_MAX_BODY_BYTES,
    OAUTH_CALLBACK_MAX_FIELD_BYTES,
    OAUTH_CALLBACK_MAX_FIELDS,
)


class OAuthCallbackErrorKind(StrEnum):
    """Parser outcome categories mapped to ErrorCode at the HTTP boundary。"""

    METHOD = "method"
    CONTENT_TYPE = "content_type"
    TOO_LARGE = "too_large"
    INVALID = "invalid"
    AMBIGUOUS = "ambiguous"
    MIXED_SOURCE = "mixed_source"


class OAuthCallbackError(ValueError):
    """Callback was rejected before state lookup。"""

    def __init__(self, kind: OAuthCallbackErrorKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


class OAuthCallbackPolicy(BaseModel):
    """Deployment-wide exact callback route/method/response-mode binding。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: str
    redirect_uri: str
    response_mode: Literal["form_post", "query"] = "form_post"

    @property
    def method(self) -> str:
        return "POST" if self.response_mode == "form_post" else "GET"


class OAuthCallbackPayload(BaseModel):
    """Bounded semantic callback fields; never serialized into logs or API payloads。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: str
    code: str | None = None
    error: str | None = None
    issuer: str | None = None
    error_description: str | None = None
    error_uri: str | None = None

    @model_validator(mode="after")
    def exactly_one_outcome(self) -> OAuthCallbackPayload:
        if bool(self.code) == bool(self.error):
            raise ValueError()
        return self


def _parse_form(raw: bytes) -> list[tuple[str, str]]:
    if len(raw) > OAUTH_CALLBACK_MAX_BODY_BYTES:
        raise OAuthCallbackError(OAuthCallbackErrorKind.TOO_LARGE)
    try:
        text = raw.decode("ascii")
        fields = parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=OAUTH_CALLBACK_MAX_FIELDS,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeError, ValueError) as exc:
        kind = (
            OAuthCallbackErrorKind.TOO_LARGE
            if raw.count(b"&") + 1 > OAUTH_CALLBACK_MAX_FIELDS
            else OAuthCallbackErrorKind.INVALID
        )
        raise OAuthCallbackError(kind) from exc
    if len(fields) > OAUTH_CALLBACK_MAX_FIELDS:
        raise OAuthCallbackError(OAuthCallbackErrorKind.TOO_LARGE)
    if any(
        len(name.encode("utf-8")) > OAUTH_CALLBACK_MAX_FIELD_BYTES
        or len(value.encode("utf-8")) > OAUTH_CALLBACK_MAX_FIELD_BYTES
        for name, value in fields
    ):
        raise OAuthCallbackError(OAuthCallbackErrorKind.TOO_LARGE)
    return fields


def parse_oauth_callback(
    *,
    policy: OAuthCallbackPolicy,
    method: str,
    content_type: str,
    raw_query: bytes,
    raw_body: bytes,
) -> OAuthCallbackPayload:
    """Parse one exact callback source without consulting durable state。"""
    if method.upper() != policy.method:
        raise OAuthCallbackError(OAuthCallbackErrorKind.METHOD)
    if raw_query and raw_body:
        raise OAuthCallbackError(OAuthCallbackErrorKind.MIXED_SOURCE)
    if policy.response_mode == "form_post":
        media_type = content_type.partition(";")[0].strip().lower()
        if media_type != "application/x-www-form-urlencoded":
            raise OAuthCallbackError(OAuthCallbackErrorKind.CONTENT_TYPE)
        if raw_query:
            raise OAuthCallbackError(OAuthCallbackErrorKind.MIXED_SOURCE)
        fields = _parse_form(raw_body)
    else:
        if raw_body:
            raise OAuthCallbackError(OAuthCallbackErrorKind.MIXED_SOURCE)
        fields = _parse_form(raw_query)

    values: dict[str, list[str]] = {}
    for name, value in fields:
        values.setdefault(name, []).append(value)
    state_values = values.get("state", [])
    if len(state_values) != 1 or not state_values[0].strip():
        raise OAuthCallbackError(OAuthCallbackErrorKind.INVALID)
    semantic = ("code", "error", "iss", "error_description", "error_uri")
    if any(len(values.get(name, [])) > 1 for name in semantic):
        raise OAuthCallbackError(OAuthCallbackErrorKind.AMBIGUOUS)
    code = values.get("code", [None])[0]
    error = values.get("error", [None])[0]
    if code is not None and not code.strip() or error is not None and not error.strip():
        raise OAuthCallbackError(OAuthCallbackErrorKind.AMBIGUOUS)
    if bool(code) == bool(error):
        raise OAuthCallbackError(OAuthCallbackErrorKind.AMBIGUOUS)
    issuer = values.get("iss", [None])[0]
    if issuer is not None and not issuer.strip():
        raise OAuthCallbackError(OAuthCallbackErrorKind.AMBIGUOUS)
    return OAuthCallbackPayload(
        state=state_values[0],
        code=code,
        error=error,
        issuer=issuer,
        error_description=values.get("error_description", [None])[0],
        error_uri=values.get("error_uri", [None])[0],
    )


def pkce_s256_challenge(code_verifier: str) -> str:
    """Return RFC 7636 S256 base64url challenge without padding。"""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorization_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    resource: str,
    requested_scopes: frozenset[str],
    state: str,
    code_verifier: str,
    response_mode: str,
) -> str:
    """Build an authorization URL with exact redirect/resource and mandatory S256。"""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(sorted(requested_scopes)),
            "state": state,
            "code_challenge": pkce_s256_challenge(code_verifier),
            "code_challenge_method": "S256",
            "response_mode": response_mode,
            "resource": resource,
        }
    )
    separator = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{separator}{query}"


def validate_callback_issuer(
    *,
    expected: str,
    received: str | None,
    issuer_parameter_supported: bool,
    trusted_issuer_count: int,
) -> bool:
    """Apply exact RFC 9207 comparison and conservative multi-AS fallback。"""
    if received is not None:
        return received == expected
    if issuer_parameter_supported:
        return False
    return trusted_issuer_count == 1
