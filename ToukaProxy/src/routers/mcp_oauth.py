# ruff: noqa: ANN003, D102, D103, TC001, TC002
"""P8-A MCP OAuth control-plane and one-time callback routes."""

from __future__ import annotations

import json
import secrets
from typing import NoReturn, Protocol

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from src.config import OAUTH_CALLBACK_MAX_BODY_BYTES
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.mcp_oauth import (
    McpOAuthCallbackOutcome,
    McpOAuthDisconnectResponse,
    McpOAuthStartRequest,
    McpOAuthStartResponse,
    McpOAuthStatusResponse,
)
from src.protocols.manager import ProtocolManager
from src.protocols.mcp.canonical import McpRequestContext, McpRequestOrigin
from src.protocols.mcp.oauth.models import (
    OAuthCallbackRouteError,
    OAuthKeyRotationRecord,
)
from src.protocols.mcp.oauth.outbound import (
    OAuthMetadataDiscoveryError,
    OAuthOutboundPolicyError,
    OAuthUpstreamError,
)
from src.protocols.mcp.oauth.profile import (
    OAuthProfile,
    OAuthProfileConflictError,
    OAuthProfileDocumentError,
)
from src.protocols.mcp.oauth.protocol import (
    OAuthCallbackError,
    OAuthCallbackErrorKind,
    OAuthCallbackPayload,
    OAuthCallbackPolicy,
    parse_oauth_callback,
)
from src.protocols.mcp.oauth.registration import OAuthRegistrationClockRollbackError
from src.protocols.mcp.oauth.rotation import (
    OAuthKeyRotationError,
    OAuthKeyRotationUnavailableError,
)
from src.protocols.mcp.oauth.runtime import (
    OAuthRuntimeBindingError,
    OAuthUnsupportedModeError,
)
from src.protocols.mcp.oauth.service import (
    OAuthAuthorizationRequiredError,
    OAuthAuthorizationServerError,
    OAuthClockRollbackError,
    OAuthIssuerMismatchError,
    OAuthMetadataError,
    OAuthStaleAuthorizationError,
    OAuthStateInvalidError,
)
from src.services.authorization_gateway import require_admin_human
from src.services.identity_router import get_verified_identity
from src.services.permission_service import permission_service
from src.utils.csrf_guard import verify_origin
from src.utils.time_utils import get_current_epoch


class McpOAuthRuntime(Protocol):
    """Thin HTTP adapter seam around the single OAuth lifecycle owner."""

    def get_callback_policy(self, redirect_route: str) -> OAuthCallbackPolicy: ...
    async def start_authorization(self, **kwargs) -> McpOAuthStartResponse: ...
    async def get_status(self, **kwargs) -> McpOAuthStatusResponse: ...
    async def disconnect(self, **kwargs) -> McpOAuthDisconnectResponse: ...
    async def complete_callback(self, **kwargs) -> McpOAuthCallbackOutcome: ...
    async def list_profiles(self) -> list[OAuthProfile]: ...
    async def put_profile(self, profile: OAuthProfile) -> OAuthProfile: ...
    async def delete_profile(self, profile_id: str) -> None: ...


class McpOAuthRotation(Protocol):
    """Admin-only non-secret key rotation control seam."""

    async def status(self) -> OAuthKeyRotationRecord: ...
    async def start(self, *, now: float) -> OAuthKeyRotationRecord: ...


router = APIRouter(prefix="", tags=["mcp-oauth"])


def _raise_mapped_oauth_error(exc: Exception) -> NoReturn:
    """Map domain failures to fixed ErrorCode without propagating secret detail。"""
    if isinstance(exc, ToukaException):
        raise exc
    if isinstance(exc, OAuthStateInvalidError):
        raise ToukaException(ErrorCode.MCP_OAUTH_STATE_INVALID) from exc
    if isinstance(exc, OAuthCallbackRouteError):
        raise ToukaException(ErrorCode.MCP_OAUTH_CALLBACK_ROUTE) from exc
    if isinstance(exc, OAuthIssuerMismatchError):
        raise ToukaException(ErrorCode.MCP_OAUTH_ISSUER_MISMATCH) from exc
    if isinstance(exc, (OAuthStaleAuthorizationError, OAuthProfileConflictError)):
        raise ToukaException(ErrorCode.MCP_OAUTH_STALE_AUTHORIZATION) from exc
    if isinstance(exc, OAuthAuthorizationRequiredError):
        raise ToukaException(ErrorCode.MCP_OAUTH_INTERACTIVE_REQUIRED) from exc
    if isinstance(
        exc,
        (OAuthClockRollbackError, OAuthRegistrationClockRollbackError),
    ):
        raise ToukaException(ErrorCode.MCP_OAUTH_CLOCK_ROLLBACK) from exc
    if isinstance(exc, OAuthAuthorizationServerError):
        raise ToukaException(ErrorCode.MCP_OAUTH_CALLBACK_INVALID) from exc
    if isinstance(
        exc,
        (
            OAuthMetadataError,
            OAuthMetadataDiscoveryError,
            OAuthOutboundPolicyError,
            OAuthUpstreamError,
        ),
    ):
        raise ToukaException(ErrorCode.MCP_OAUTH_UPSTREAM_ERROR) from exc
    if isinstance(exc, OAuthUnsupportedModeError):
        raise ToukaException(ErrorCode.MCP_OAUTH_DISABLED) from exc
    if isinstance(exc, OAuthKeyRotationUnavailableError):
        raise ToukaException(ErrorCode.MCP_OAUTH_KEY_ROTATION_UNAVAILABLE) from exc
    if isinstance(exc, OAuthKeyRotationError):
        raise ToukaException(ErrorCode.MCP_OAUTH_KEY_ROTATION_CONFLICT) from exc
    if isinstance(exc, (OAuthRuntimeBindingError, OAuthProfileDocumentError)):
        raise ToukaException(ErrorCode.VALIDATION_ERROR) from exc
    raise exc


def get_mcp_oauth_runtime(request: Request) -> McpOAuthRuntime:
    """Resolve the feature-gated runtime injected by lifespan."""
    context = getattr(request.app.state, "context", {})
    runtime = context.get("mcp_oauth_runtime") if isinstance(context, dict) else None
    if runtime is None:
        raise ToukaException(ErrorCode.MCP_OAUTH_DISABLED)
    return runtime


def get_mcp_oauth_rotation(request: Request) -> McpOAuthRotation:
    """Resolve the lifespan-owned managed key rotation service."""
    context = getattr(request.app.state, "context", {})
    rotation = context.get("mcp_oauth_rotation") if isinstance(context, dict) else None
    if rotation is None:
        raise ToukaException(ErrorCode.MCP_OAUTH_DISABLED)
    return rotation


def _get_protocol_manager(request: Request) -> ProtocolManager:
    context = getattr(request.app.state, "context", {})
    manager = context.get("protocol_manager") if isinstance(context, dict) else None
    if manager is None:
        raise ToukaException(ErrorCode.SYSTEM_ERROR)
    return manager


def _build_request_context(
    request: Request,
    identity: AgentIdentity,
    server_name: str,
) -> McpRequestContext:
    human_principal_id = getattr(request.state, "human_principal_id", None)
    if not isinstance(human_principal_id, str) or not human_principal_id:
        raise ToukaException(ErrorCode.MCP_OAUTH_INTERACTIVE_REQUIRED)
    return McpRequestContext(
        request_id=str(
            getattr(
                request.state,
                "request_id",
                f"mcp-oauth-{identity.agent_id}-{server_name}",
            )
        ),
        identity=identity,
        human_principal_id=human_principal_id,
        installation_id=server_name,
        auth_level="Manual",
        origin=McpRequestOrigin.FOREGROUND,
    )


def _authorize_installation(
    manager: ProtocolManager,
    server_name: str,
    identity: AgentIdentity,
) -> int:
    if manager.registry.get_server(server_name) is None:
        raise ToukaException(ErrorCode.VALIDATION_ERROR)
    if not permission_service.is_mcp_server_allowed(server_name, identity):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    return manager.get_installation_generation(server_name)


@router.post(
    "/mcp/servers/{server_name}/oauth/start",
    response_model=McpOAuthStartResponse,
)
async def start_mcp_oauth(
    server_name: str,
    payload: McpOAuthStartRequest,
    request: Request,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
    runtime: McpOAuthRuntime = Depends(get_mcp_oauth_runtime),  # noqa: B008
    manager: ProtocolManager = Depends(_get_protocol_manager),  # noqa: B008
) -> McpOAuthStartResponse:
    verify_origin(request)
    generation = _authorize_installation(manager, server_name, identity)
    try:
        return await runtime.start_authorization(
            server_name=server_name,
            request_context=_build_request_context(request, identity, server_name),
            installation_generation=generation,
            selected_issuer=payload.authorization_server,
        )
    except Exception as exc:
        _raise_mapped_oauth_error(exc)


@router.get(
    "/mcp/servers/{server_name}/oauth/status",
    response_model=McpOAuthStatusResponse,
)
async def get_mcp_oauth_status(
    server_name: str,
    request: Request,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
    runtime: McpOAuthRuntime = Depends(get_mcp_oauth_runtime),  # noqa: B008
    manager: ProtocolManager = Depends(_get_protocol_manager),  # noqa: B008
) -> McpOAuthStatusResponse:
    generation = _authorize_installation(manager, server_name, identity)
    try:
        return await runtime.get_status(
            server_name=server_name,
            request_context=_build_request_context(request, identity, server_name),
            installation_generation=generation,
        )
    except Exception as exc:
        _raise_mapped_oauth_error(exc)


@router.delete(
    "/mcp/servers/{server_name}/oauth",
    response_model=McpOAuthDisconnectResponse,
)
async def disconnect_mcp_oauth(
    server_name: str,
    request: Request,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
    runtime: McpOAuthRuntime = Depends(get_mcp_oauth_runtime),  # noqa: B008
    manager: ProtocolManager = Depends(_get_protocol_manager),  # noqa: B008
) -> McpOAuthDisconnectResponse:
    verify_origin(request)
    generation = _authorize_installation(manager, server_name, identity)
    try:
        return await runtime.disconnect(
            server_name=server_name,
            request_context=_build_request_context(request, identity, server_name),
            installation_generation=generation,
        )
    except Exception as exc:
        _raise_mapped_oauth_error(exc)


def _callback_error(exc: OAuthCallbackError) -> ToukaException:
    if exc.kind == OAuthCallbackErrorKind.CONTENT_TYPE:
        return ToukaException(ErrorCode.MCP_OAUTH_CALLBACK_CONTENT_TYPE)
    if exc.kind == OAuthCallbackErrorKind.TOO_LARGE:
        return ToukaException(ErrorCode.MCP_OAUTH_CALLBACK_TOO_LARGE)
    if exc.kind == OAuthCallbackErrorKind.METHOD:
        return ToukaException(ErrorCode.MCP_OAUTH_CALLBACK_METHOD)
    if exc.kind == OAuthCallbackErrorKind.AMBIGUOUS:
        return ToukaException(ErrorCode.MCP_OAUTH_CALLBACK_AMBIGUOUS)
    return ToukaException(ErrorCode.MCP_OAUTH_CALLBACK_INVALID)


async def _parse_callback(
    request: Request,
    runtime: McpOAuthRuntime,
) -> tuple[OAuthCallbackPayload, str]:
    route = request.url.path
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ToukaException(ErrorCode.MCP_OAUTH_CALLBACK_INVALID) from exc
        if declared_length < 0:
            raise ToukaException(ErrorCode.MCP_OAUTH_CALLBACK_INVALID)
        if declared_length > OAUTH_CALLBACK_MAX_BODY_BYTES:
            raise ToukaException(ErrorCode.MCP_OAUTH_CALLBACK_TOO_LARGE)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > OAUTH_CALLBACK_MAX_BODY_BYTES:
            raise ToukaException(ErrorCode.MCP_OAUTH_CALLBACK_TOO_LARGE)
        body.extend(chunk)
    raw_body = bytes(body)
    policy = runtime.get_callback_policy(route)
    try:
        callback = parse_oauth_callback(
            policy=policy,
            method=request.method,
            content_type=request.headers.get("content-type", ""),
            raw_query=request.scope.get("query_string", b""),
            raw_body=raw_body,
        )
    except OAuthCallbackError as exc:
        raise _callback_error(exc) from exc
    return callback, route


def _callback_page(outcome: McpOAuthCallbackOutcome) -> HTMLResponse:
    nonce = secrets.token_urlsafe(18)
    operation_id = json.dumps(outcome.operation_id).replace("<", "\\u003c")
    status = json.dumps(outcome.status.value).replace("<", "\\u003c")
    client_origin = json.dumps(outcome.client_origin).replace("<", "\\u003c")
    page = (
        "<!doctype html><meta charset=utf-8><title>OAuth complete</title>"
        f'<script nonce="{nonce}">'
        'history.replaceState(null,"",location.pathname);'
        "if(window.opener){window.opener.postMessage({"
        f'type:"touka:mcp-oauth",operationId:{operation_id},status:{status}'
        f"}},{client_origin});}}window.close();"
        "</script>"
    )
    response = HTMLResponse(page)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; base-uri 'none'; form-action 'none'; "
        f"script-src 'nonce-{nonce}'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _callback_failure_page(*, status_code: int) -> HTMLResponse:
    """Scrub query credentials even when callback validation or exchange fails。"""
    nonce = secrets.token_urlsafe(18)
    page = (
        "<!doctype html><meta charset=utf-8><title>OAuth failed</title>"
        f'<script nonce="{nonce}">'
        'history.replaceState(null,"",location.pathname);window.close();'
        "</script>"
    )
    response = HTMLResponse(page, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; base-uri 'none'; form-action 'none'; "
        f"script-src 'nonce-{nonce}'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def _complete_callback(
    request: Request,
    runtime: McpOAuthRuntime,
) -> HTMLResponse:
    callback, route = await _parse_callback(request, runtime)
    try:
        outcome = await runtime.complete_callback(
            callback=callback,
            redirect_route=route,
        )
    except Exception as exc:
        _raise_mapped_oauth_error(exc)
    return _callback_page(outcome)


@router.post("/mcp/oauth/callback", response_class=HTMLResponse)
async def complete_mcp_oauth_form_post(
    request: Request,
    runtime: McpOAuthRuntime = Depends(get_mcp_oauth_runtime),  # noqa: B008
) -> HTMLResponse:
    return await _complete_callback(request, runtime)


@router.get("/mcp/oauth/callback", response_class=HTMLResponse)
async def complete_mcp_oauth_query(
    request: Request,
) -> HTMLResponse:
    try:
        runtime = get_mcp_oauth_runtime(request)
        return await _complete_callback(request, runtime)
    except Exception as exc:
        try:
            _raise_mapped_oauth_error(exc)
        except ToukaException as mapped:
            status_code = mapped.status_code
        except Exception:
            status_code = 500
        return _callback_failure_page(status_code=status_code)


@router.get(
    "/admin/mcp/oauth/profiles",
    response_model=list[OAuthProfile],
)
async def list_mcp_oauth_profiles(
    _admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    runtime: McpOAuthRuntime = Depends(get_mcp_oauth_runtime),  # noqa: B008
) -> list[OAuthProfile]:
    return await runtime.list_profiles()


@router.put(
    "/admin/mcp/oauth/profiles/{profile_id}",
    response_model=OAuthProfile,
)
async def put_mcp_oauth_profile(
    profile_id: str,
    profile: OAuthProfile,
    request: Request,
    _admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    runtime: McpOAuthRuntime = Depends(get_mcp_oauth_runtime),  # noqa: B008
) -> OAuthProfile:
    verify_origin(request)
    if profile_id != profile.profile_id:
        raise ToukaException(ErrorCode.VALIDATION_ERROR)
    return await runtime.put_profile(profile)


@router.delete("/admin/mcp/oauth/profiles/{profile_id}", status_code=204)
async def delete_mcp_oauth_profile(
    profile_id: str,
    request: Request,
    _admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    runtime: McpOAuthRuntime = Depends(get_mcp_oauth_runtime),  # noqa: B008
) -> None:
    verify_origin(request)
    await runtime.delete_profile(profile_id)


@router.get(
    "/admin/mcp/oauth/keys/rotation",
    response_model=OAuthKeyRotationRecord,
)
async def get_mcp_oauth_key_rotation(
    _admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    rotation: McpOAuthRotation = Depends(get_mcp_oauth_rotation),  # noqa: B008
) -> OAuthKeyRotationRecord:
    """Return the persisted non-secret key rotation state."""
    return await rotation.status()


@router.post(
    "/admin/mcp/oauth/keys/rotation",
    response_model=OAuthKeyRotationRecord,
)
async def start_mcp_oauth_key_rotation(
    request: Request,
    _admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    rotation: McpOAuthRotation = Depends(get_mcp_oauth_rotation),  # noqa: B008
) -> OAuthKeyRotationRecord:
    """Start one managed rotation after CSRF and human-admin gates."""
    verify_origin(request)
    try:
        return await rotation.start(now=get_current_epoch())
    except Exception as exc:
        _raise_mapped_oauth_error(exc)
