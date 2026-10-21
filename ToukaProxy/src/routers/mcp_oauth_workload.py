# ruff: noqa: D102, D103
"""P8-B workload OAuth admin and exact-owner HTTP control plane。"""

from __future__ import annotations

from typing import Any, NoReturn, Protocol

from fastapi import APIRouter, Depends, Query, Request  # noqa: TC002

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.models.mcp_oauth_workload import (
    WorkloadCredentialSetRequest,  # noqa: TC001
    WorkloadCredentialStatus,
    WorkloadDelegationPutRequest,  # noqa: TC001
    WorkloadDelegationView,
    WorkloadMetadataStatus,
    WorkloadMutationResponse,
    WorkloadRecoveryItem,
    WorkloadTransitionReconcileRequest,  # noqa: TC001
)
from src.protocols.mcp.oauth.models import OAuthKeyRotationRecord
from src.protocols.mcp.oauth.rotation import (
    OAuthKeyRotationError,
    OAuthKeyRotationUnavailableError,
)
from src.protocols.mcp.oauth.workload.control import (
    WorkloadControlDenied,
    WorkloadControlRecoveryRequired,
)
from src.protocols.mcp.oauth.workload.delegation import (
    WorkloadDelegationConflictError,
    WorkloadDelegationDocumentError,
)
from src.protocols.mcp.oauth.workload.models import WorkloadOAuthProfile
from src.protocols.mcp.oauth.workload.profile import (
    WorkloadProfileConflictError,
    WorkloadProfileDocumentError,
)
from src.protocols.mcp.oauth.workload.store import (
    WorkloadAuthorityConflictError,
    WorkloadCredentialConflictError,
    WorkloadStoreCorruptionError,
)
from src.routers.mcp_oauth import require_admin_human
from src.services.identity_router import get_verified_identity
from src.utils.csrf_guard import verify_origin
from src.utils.time_utils import get_current_epoch


class McpWorkloadOAuthControl(Protocol):
    """HTTP-facing non-secret projection of the workload control service。"""

    async def list_profiles(self) -> list[WorkloadOAuthProfile]: ...
    async def put_profile(
        self,
        profile: WorkloadOAuthProfile,
        **kwargs: Any,
    ) -> WorkloadOAuthProfile: ...
    async def delete_profile(self, **kwargs: Any) -> WorkloadMutationResponse: ...
    async def set_credential(self, **kwargs: Any) -> WorkloadCredentialStatus: ...
    async def get_credential_status(self, profile_id: str) -> WorkloadCredentialStatus: ...
    async def revoke_credential(self, **kwargs: Any) -> WorkloadMutationResponse: ...
    async def refresh_metadata(self, **kwargs: Any) -> WorkloadMetadataStatus: ...
    async def recovery_status(self) -> list[WorkloadRecoveryItem]: ...
    async def retry_recovery(self, **kwargs: Any) -> list[WorkloadRecoveryItem]: ...
    async def reconcile_transition(self, **kwargs: Any) -> WorkloadMutationResponse: ...
    async def list_delegations(self, **kwargs: Any) -> list[Any]: ...
    async def put_delegation(self, **kwargs: Any) -> Any: ...
    async def delete_delegation(self, **kwargs: Any) -> WorkloadMutationResponse: ...


class McpWorkloadOAuthRotation(Protocol):
    """Non-secret managed workload key rotation seam。"""

    async def status(self) -> OAuthKeyRotationRecord: ...
    async def start(self, *, now: float) -> OAuthKeyRotationRecord: ...
    async def maintain(self, *, now: float | None = None) -> OAuthKeyRotationRecord: ...


router = APIRouter(prefix="", tags=["mcp-oauth-workload"])


def get_mcp_workload_oauth_control(request: Request) -> McpWorkloadOAuthControl:
    """Resolve the lifespan-owned, feature-gated P8-B control service。"""
    context = getattr(request.app.state, "context", {})
    control = (
        context.get("mcp_workload_oauth_control")
        if isinstance(context, dict)
        else None
    )
    if control is None:
        raise ToukaException(ErrorCode.MCP_OAUTH_DISABLED)
    return control


def get_mcp_workload_oauth_rotation(request: Request) -> McpWorkloadOAuthRotation:
    """Resolve the lifespan-owned workload key rotation service。"""
    context = getattr(request.app.state, "context", {})
    rotation = (
        context.get("mcp_workload_oauth_rotation")
        if isinstance(context, dict)
        else None
    )
    if rotation is None:
        raise ToukaException(ErrorCode.MCP_OAUTH_DISABLED)
    return rotation


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return str(value) if value else "mcp-workload-control"


def _human_principal(request: Request) -> str:
    value = getattr(request.state, "human_principal_id", None)
    if not isinstance(value, str) or not value:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    return value


def _raise_mapped_workload_error(exc: Exception) -> NoReturn:
    """Return only stable error codes and never serialize upstream or secret detail。"""
    if isinstance(exc, ToukaException):
        raise exc
    if isinstance(exc, WorkloadControlDenied):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED) from exc
    if isinstance(exc, WorkloadControlRecoveryRequired):
        raise ToukaException(ErrorCode.MCP_OAUTH_STALE_AUTHORIZATION) from exc
    if isinstance(exc, OAuthKeyRotationUnavailableError):
        raise ToukaException(ErrorCode.MCP_OAUTH_KEY_ROTATION_UNAVAILABLE) from exc
    if isinstance(exc, OAuthKeyRotationError):
        raise ToukaException(ErrorCode.MCP_OAUTH_KEY_ROTATION_CONFLICT) from exc
    if isinstance(
        exc,
        (
            WorkloadAuthorityConflictError,
            WorkloadCredentialConflictError,
            WorkloadDelegationConflictError,
            WorkloadDelegationDocumentError,
            WorkloadProfileConflictError,
            WorkloadProfileDocumentError,
            WorkloadStoreCorruptionError,
        ),
    ):
        raise ToukaException(ErrorCode.VALIDATION_ERROR) from exc
    raise exc


@router.get(
    "/admin/mcp/oauth/workload/profiles",
    response_model=list[WorkloadOAuthProfile],
)
async def list_workload_profiles(
    _admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
) -> list[WorkloadOAuthProfile]:
    return await control.list_profiles()


@router.put(
    "/admin/mcp/oauth/workload/profiles/{profile_id}",
    response_model=WorkloadOAuthProfile,
)
async def put_workload_profile(
    profile_id: str,
    profile: WorkloadOAuthProfile,
    request: Request,
    admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
) -> WorkloadOAuthProfile:
    verify_origin(request)
    if profile_id != profile.profile_id:
        raise ToukaException(ErrorCode.VALIDATION_ERROR)
    try:
        return await control.put_profile(
            profile,
            actor_id=admin.agent_id,
            request_id=_request_id(request),
        )
    except Exception as exc:
        _raise_mapped_workload_error(exc)


@router.delete(
    "/admin/mcp/oauth/workload/profiles/{profile_id}",
    response_model=WorkloadMutationResponse,
)
async def delete_workload_profile(
    profile_id: str,
    request: Request,
    expected_generation: int = Query(ge=1),
    admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
) -> WorkloadMutationResponse:
    verify_origin(request)
    try:
        return await control.delete_profile(
            profile_id=profile_id,
            expected_generation=expected_generation,
            actor_id=admin.agent_id,
            request_id=_request_id(request),
        )
    except Exception as exc:
        _raise_mapped_workload_error(exc)


@router.get(
    "/admin/mcp/oauth/workload/profiles/{profile_id}/credential",
    response_model=WorkloadCredentialStatus,
)
async def get_workload_credential_status(
    profile_id: str,
    _admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
) -> WorkloadCredentialStatus:
    try:
        return await control.get_credential_status(profile_id)
    except Exception as exc:
        _raise_mapped_workload_error(exc)


@router.put(
    "/admin/mcp/oauth/workload/profiles/{profile_id}/credential",
    response_model=WorkloadCredentialStatus,
)
async def set_workload_credential(
    profile_id: str,
    payload: WorkloadCredentialSetRequest,
    request: Request,
    admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
) -> WorkloadCredentialStatus:
    verify_origin(request)
    try:
        return await control.set_credential(
            profile_id=profile_id,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
            actor_id=admin.agent_id,
            request_id=_request_id(request),
        )
    except Exception as exc:
        _raise_mapped_workload_error(exc)


@router.post(
    "/admin/mcp/oauth/workload/profiles/{profile_id}/credential/revoke",
    response_model=WorkloadMutationResponse,
)
async def revoke_workload_credential(
    profile_id: str,
    request: Request,
    admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
) -> WorkloadMutationResponse:
    verify_origin(request)
    try:
        return await control.revoke_credential(
            profile_id=profile_id,
            actor_id=admin.agent_id,
            request_id=_request_id(request),
        )
    except Exception as exc:
        _raise_mapped_workload_error(exc)


@router.post(
    "/admin/mcp/oauth/workload/profiles/{profile_id}/metadata/refresh",
    response_model=WorkloadMetadataStatus,
)
async def refresh_workload_metadata(
    profile_id: str,
    request: Request,
    admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
) -> WorkloadMetadataStatus:
    verify_origin(request)
    try:
        return await control.refresh_metadata(
            profile_id=profile_id,
            actor_id=admin.agent_id,
            request_id=_request_id(request),
        )
    except Exception as exc:
        _raise_mapped_workload_error(exc)


@router.get(
    "/admin/mcp/oauth/workload/recovery",
    response_model=list[WorkloadRecoveryItem],
)
async def get_workload_recovery_status(
    _admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
) -> list[WorkloadRecoveryItem]:
    return await control.recovery_status()


@router.post(
    "/admin/mcp/oauth/workload/recovery/retry",
    response_model=list[WorkloadRecoveryItem],
)
async def retry_workload_recovery(
    request: Request,
    admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
    rotation: McpWorkloadOAuthRotation = Depends(  # noqa: B008
        get_mcp_workload_oauth_rotation
    ),
) -> list[WorkloadRecoveryItem]:
    verify_origin(request)
    try:
        await rotation.maintain(now=get_current_epoch())
        return await control.retry_recovery(
            actor_id=admin.agent_id,
            request_id=_request_id(request),
        )
    except Exception as exc:
        _raise_mapped_workload_error(exc)


@router.post(
    "/admin/mcp/oauth/workload/recovery/transitions/{operation_id}/reconcile",
    response_model=WorkloadMutationResponse,
)
async def reconcile_workload_transition(
    operation_id: str,
    payload: WorkloadTransitionReconcileRequest,
    request: Request,
    admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
) -> WorkloadMutationResponse:
    verify_origin(request)
    try:
        return await control.reconcile_transition(
            operation_id=operation_id,
            resolution=payload.resolution,
            actor_id=admin.agent_id,
            request_id=_request_id(request),
        )
    except Exception as exc:
        _raise_mapped_workload_error(exc)


@router.get(
    "/admin/mcp/oauth/workload/keys/rotation",
    response_model=OAuthKeyRotationRecord,
)
async def get_workload_key_rotation(
    _admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    rotation: McpWorkloadOAuthRotation = Depends(  # noqa: B008
        get_mcp_workload_oauth_rotation
    ),
) -> OAuthKeyRotationRecord:
    return await rotation.status()


@router.post(
    "/admin/mcp/oauth/workload/keys/rotation",
    response_model=OAuthKeyRotationRecord,
)
async def start_workload_key_rotation(
    request: Request,
    _admin: AgentIdentity = Depends(require_admin_human),  # noqa: B008
    rotation: McpWorkloadOAuthRotation = Depends(  # noqa: B008
        get_mcp_workload_oauth_rotation
    ),
) -> OAuthKeyRotationRecord:
    verify_origin(request)
    try:
        return await rotation.start(now=get_current_epoch())
    except Exception as exc:
        _raise_mapped_workload_error(exc)


@router.get(
    "/mcp/agents/{agent_id}/oauth/delegations",
    response_model=list[WorkloadDelegationView],
)
async def list_owner_delegations(
    agent_id: str,  # noqa: IDENTITY_SSOT
    request: Request,
    _identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
) -> list[WorkloadDelegationView]:
    try:
        records = await control.list_delegations(
            owner_user_id=_human_principal(request),
            execution_agent_id=agent_id,
        )
        return [WorkloadDelegationView.model_validate(record) for record in records]
    except Exception as exc:
        _raise_mapped_workload_error(exc)


@router.put(
    "/mcp/agents/{agent_id}/oauth/delegations/{delegation_id}",
    response_model=WorkloadDelegationView,
)
async def put_owner_delegation(
    agent_id: str,  # noqa: IDENTITY_SSOT
    delegation_id: str,
    payload: WorkloadDelegationPutRequest,
    request: Request,
    _identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
) -> WorkloadDelegationView:
    verify_origin(request)
    try:
        return WorkloadDelegationView.model_validate(
            await control.put_delegation(
                delegation_id=delegation_id,
                owner_user_id=_human_principal(request),
                execution_agent_id=agent_id,
                request_id=_request_id(request),
                **payload.model_dump(),
            )
        )
    except Exception as exc:
        _raise_mapped_workload_error(exc)


@router.delete(
    "/mcp/agents/{agent_id}/oauth/delegations/{delegation_id}",
    response_model=WorkloadMutationResponse,
)
async def delete_owner_delegation(
    agent_id: str,  # noqa: IDENTITY_SSOT
    delegation_id: str,
    request: Request,
    _identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
    control: McpWorkloadOAuthControl = Depends(  # noqa: B008
        get_mcp_workload_oauth_control
    ),
) -> WorkloadMutationResponse:
    verify_origin(request)
    try:
        return await control.delete_delegation(
            delegation_id=delegation_id,
            owner_user_id=_human_principal(request),
            execution_agent_id=agent_id,
            request_id=_request_id(request),
        )
    except Exception as exc:
        _raise_mapped_workload_error(exc)
