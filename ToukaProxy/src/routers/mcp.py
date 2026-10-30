"""MCP 管理路由 (MCP Management Router).

負責處理 MCP (Model Context Protocol) 伺服器的配置、狀態枚舉以及與 Hub 的同步安裝。
"""

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, get_args

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.models.mcp_catalog import (
    MCPPromptGetRequest,
    MCPPromptGetResponse,
    MCPResourceReadRequest,
    MCPResourceReadResponse,
    MCPServerPromptsResponse,
    MCPServerResourcesResponse,
    MCPServerResourceTemplatesResponse,
    MCPServerToolItem,
    MCPServerToolsResponse,
)
from src.models.mcp_isolation import (
    McpConfigSaveResponse,
    McpIsolationClass,
    McpIsolationUpdateRequest,
    McpIsolationUpdateResponse,
    McpIsolationView,
    McpOrigin,
    McpRestartRequest,
    SandboxPathGrant,
)
from src.protocols.manager import McpServerBusyError, ProtocolManager
from src.protocols.mcp.canonical import (
    McpRequestContext,
    McpRequestOrigin,
    McpToolCatalog,
)
from src.protocols.mcp.mrtr import (
    McpMrtrAuthorizationError,
    McpMrtrError,
)
from src.protocols.mcp.sdk_adapter import UNKNOWN_PROTOCOL_VERSION
from src.services.authorization_gateway import require_human_requester, require_privileged
from src.services.identity_router import get_verified_identity
from src.services.isolation.consent import Consent
from src.services.isolation.mcp_launch import manual_start_subject, origin_record
from src.services.isolation.profiles import authorize_derived_path, current_layout
from src.services.isolation.status import FallbackPlan
from src.services.mcp_hub_service import MCPHubService, _read_json_or_raise, derive_server_key
from src.services.mcp_isolation_store import (
    ENTRY_CHANGED,
    McpIsolationStore,
    RecordView,
    bound_of,
    changed_programs,
    delete,
    origin_category,
    reconcile_saved_entries,
    replace_isolation,
)
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.utils import mcp_tool_policy
from src.utils.csrf_guard import verify_origin
from src.utils.mcp_origin import (
    OriginRecord,
    classify,
    proxy_base_dir,
    raw_entry,
)
from src.utils.mcp_remote import validate_remote_url
from src.utils.mcp_security import is_command_safe
from src.utils.secrets import is_masked, mask_value
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from src.protocols.mcp.canonical import (
        McpPromptCatalog,
        McpResourceCatalog,
        McpResourceTemplateCatalog,
    )


# Require Owner/Debugger/admin to manage MCP servers.
_require_mcp_role = require_privileged


def _get_protocol_manager(request: Request) -> ProtocolManager:
    """從 lifespan 注入的 base context 取得 manager，避免 router→main 循環匯入。"""
    context = getattr(request.app.state, "context", {})
    protocol_manager = context.get("protocol_manager") if isinstance(context, dict) else None
    if protocol_manager is None:
        raise ToukaException(ErrorCode.SYSTEM_ERROR)
    return protocol_manager


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mcp", tags=["mcp"])


# 使用 get_config_dir() 動態定錨路徑 (SSOT)
def get_mcp_config_path() -> Path:
    """獲取 MCP 伺服器配置檔案路徑 (SSOT)."""
    return get_config_dir() / "server_config.json"


def get_mcp_hub_path() -> Path:
    """獲取 MCP Hub 清單快取檔案路徑 (SSOT)."""
    return get_config_dir() / "mcp_hub.json"


# 保持向後相容的常量 (SSOT: 強制使用 Path)
CONFIG_PATH = get_mcp_config_path()
HUB_CONFIG_PATH = get_mcp_hub_path()


class MCPServerItem(BaseModel):
    """MCP 伺服器單體配置模型。"""

    model_config = ConfigDict(extra="forbid")
    # stdio 形狀（本地子進程）；remote 條目此二欄為空。
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] | None = None
    # remote 形狀（streamable HTTP，ADR-0192）：憑證留在 env、header 以 ${VAR} 引用，
    # 故 headers 本身非機密、無須比照 env 做遮罩回填。
    url: str | None = None
    headers: dict[str, str] | None = None
    enabled: bool = True
    # vault 隔離設定（mcp_vault_jail；ADR-0293 之後設了這個的工具一律拒絕）。納入契約以便 GET
    # 不被 extra='forbid' 整份拒、且 POST 來回不丟失；前端未回傳時由 _preserve_masked_env
    # 自磁碟還原（不可被前端清掉）。
    vault_jail_arg: str | None = None
    # per-installation 工具治理（ADR-0195 軸 A）。同樣納入契約——extra='forbid' 下漏宣告
    # 一個磁碟既存欄位，整份 GET 會被 ValidationError 打成空設定頁。
    # ``enabled_tools``：``None`` ＝未設＝全開；``["*"]`` ＝明示全開（UI 的「全部開啟」送這個，
    # 因為 None 會觸發 `_preserve_masked_env` 的自磁碟還原而清不掉）；``[]`` ＝明示全關。
    enabled_tools: list[str] | None = None
    # ``disabled_tools``：硬阻擋清單，恆優先。刻意**不由 UI 驅動**——它是手寫設定的逃生口，
    # 例行存檔不得將其抹除（同 vault_jail_arg 的「政策不因日常操作 fail open」原則）。
    disabled_tools: list[str] = []
    # ADR-0221：僅接受 modern discover 協商，移除舊協定模式。
    protocol_mode: Literal["2026-07-28"] = "2026-07-28"
    auth_mode: Literal[
        "none",
        "static_headers",
        "oauth_user",
        "oauth_client_credentials",
        "enterprise",
    ] = "none"
    auth_profile_id: str | None = None
    workload_authority_kind: Literal["direct", "owner_delegation"] | None = None
    workload_delegation_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$",
    )
    workload_source_installation_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$",
    )
    capability_policy: dict[str, Any] = Field(default_factory=dict)
    cache_enabled: bool = True
    cache_max_entries: int = Field(default=1024, ge=0, le=8192)
    cache_default_ttl_ms: int = Field(default=0, ge=0, le=86_400_000)
    subscriptions_enabled: bool = True
    subscription_max_backoff_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    approved_header_tools: list[str] = []
    icons_enabled: bool = True
    telemetry_enabled: bool = False

    @field_validator("command")
    @classmethod
    def command_must_not_contain_shell_injection(cls, v: str | None) -> str | None:
        """M4 fix: reject commands containing shell injection metacharacters.

        Prevents an attacker who can write to server_config.json from injecting
        arbitrary shell commands via the MCP server command field.
        """
        if v is not None and not is_command_safe(v):
            msg = (
                f"MCP server command contains shell injection characters: {v!r}. "
                "Only plain commands and path separators are allowed."
            )
            raise ValueError(msg)
        return v

    @field_validator("url")
    @classmethod
    def url_must_be_safe(cls, v: str | None) -> str | None:
        """Remote 端點安全閘（明文 http 僅限 loopback）——與 registry 層同一 SSOT。"""
        if v is not None:
            validate_remote_url(v)
        return v

    @model_validator(mode="after")
    def exactly_one_transport(self) -> "MCPServerItem":
        """``command`` 與 ``url`` 恰須其一（與 ``ProtocolServerConfig`` 同契約）。"""
        if bool(self.command) == bool(self.url):
            msg = (
                "MCP server 設定必須且只能擇一指定 `command`（本地 stdio）"
                "或 `url`（remote streamable HTTP）。"
            )
            raise ValueError(msg)
        return self


class MCPConfigResponse(BaseModel):
    """MCP 全域配置回應模型。"""

    model_config = ConfigDict(extra="forbid")
    mcpServers: dict[str, MCPServerItem]


class MCPHubServerItem(BaseModel):
    """MCP Hub 中的伺服器項目模型。"""

    id: str
    name: str
    description: str
    # stdio 條目帶 command/args；remote 條目（ADR-0192）帶 url/headers，兩者互斥。
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    headers: dict[str, str] | None = None
    category: str | None = None
    author: str | None = None
    github: str | None = None
    env_requirements: list[str] | None = None
    downloads: int | None = None
    popularity: int | None = None
    npm_package: str | None = None
    # 前端實際消費欄位（ADR-0187）：response_model 會剝除未宣告欄位，缺列＝靜默丟失。
    icon: str | None = None
    source_url: str | None = None
    # 徽章比對鍵（ADR-0190）：install 落盤鍵＝derive_server_key(name)，hub.id 帶前綴恆對不上；
    # 由 /hub 端點投影、前端只消費（衍生規則不得在前端重刻）。
    server_key: str | None = None


class MCPHubResponse(BaseModel):
    """MCP Hub 列表回應模型。"""

    hub: list[MCPHubServerItem]


@router.get("/list")
async def list_active_mcp_servers(
    _identity: AgentIdentity = Depends(_require_mcp_role),  # noqa: B008
) -> dict[str, Any]:
    """獲取當前活躍（已連線）的 MCP 伺服器狀態"""
    try:
        from src.main import protocol_manager

        final_results = await protocol_manager.get_active_server_info()
    except Exception:
        logger.exception("Failed to list active MCP servers")
        return {"servers": []}
    else:
        return {"servers": final_results}


MCP_NOT_WAITING_ISOLATION = "mcp_not_waiting_isolation"
MCP_ISOLATION_ACK_MISMATCH = "mcp_isolation_ack_mismatch"
MCP_MANUAL_START_STARTER = "mcp_manual_starter"
"""lifespan context 裡手動啟動實作的鍵（U3e 接上；沒有時當作沒有伺服器在等隔離）。"""


class McpManualStarter(Protocol):
    """手動啟動需要啟動層提供的兩件事（U3d 的 ``mcp_launch`` 算去處、U3e 接到 manager）。"""

    def manual_start_plan(self, name: str) -> FallbackPlan | None:
        """這台是自建而且在等隔離時，回伺服器當下算的降級去處；否則 None。"""
        ...

    async def start_with_consent(self, name: str, consent: Consent) -> bool:
        """只用這一次同意啟動這台；之後的重啟重新判定。"""
        ...


def _manual_starter(request: Request) -> McpManualStarter | None:
    context = getattr(request.app.state, "context", {})
    return context.get(MCP_MANUAL_START_STARTER) if isinstance(context, dict) else None


async def _manual_start(
    server_name: str,
    body: McpRestartRequest,
    identity: AgentIdentity,
    protocol_manager: Any,
    http_request: Request,
) -> bool:
    """人在設定頁確認降級去處後啟動等隔離的自建 MCP 一次（R3-4）；同意綁名稱、generation、bound。"""
    require_human_requester(identity)
    config = protocol_manager.registry.get_server(server_name)
    generation = protocol_manager.get_installation_generation(server_name)
    bound = bound_of(config.model_dump(include={"command", "args", "url"}))
    if body.generation != generation or body.bound != bound:
        raise ToukaException(ErrorCode.VALIDATION_ERROR, detail_override=ENTRY_CHANGED)
    starter = _manual_starter(http_request)
    plan = starter.manual_start_plan(server_name) if starter is not None else None
    if starter is None or plan is None:
        raise ToukaException(ErrorCode.VALIDATION_ERROR, detail_override=MCP_NOT_WAITING_ISOLATION)
    if plan.kind != body.isolation_ack or sorted(plan.features) != body.features:
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR, detail_override=MCP_ISOLATION_ACK_MISMATCH
        )
    subject = manual_start_subject(server_name, generation, bound)
    ok = await starter.start_with_consent(server_name, Consent(subject, plan.kind, plan.features))
    logger.warning(
        "AUDIT: %s manually started MCP %s (fallback=%s, ok=%s)",
        identity.agent_id,
        server_name,
        plan.kind,
        ok,
    )
    return ok


@router.post("/{server_name}/restart")
async def restart_mcp_server(
    server_name: str,
    http_request: Request,
    body: McpRestartRequest | None = None,
    identity: AgentIdentity = Depends(_require_mcp_role),  # noqa: B008
) -> dict[str, Any]:
    """重啟單一 MCP 伺服器（關閉舊連線並重生）。Owner/Debugger/admin＋CSRF。

    不寫設定、只重生已設定的伺服器（破壞性低）；但**會中斷該伺服器進行中的工具呼叫**。
    回傳重啟後的最新狀態供前端即時更新。body 帶 ``isolation_ack`` 時是手動啟動等隔離的
    自建 MCP：要人類請求者，``generation``、``bound`` 與降級去處都要和現在的一樣。
    """
    verify_origin(http_request)
    # protocol_manager 為 main 模組單例；router 於頂層 import main 會循環，故同 list 端點於
    # 函式內取用（置於 try 內，與既有模式一致）。
    try:
        from src.main import protocol_manager
    except ImportError as exc:  # 防禦：main 必已載入
        raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(exc)) from exc
    # 存在性檢查走註冊表（避免於 router 直接 import protocols 例外、踩模組邊界）。
    if protocol_manager.registry.get_server(server_name) is None:
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR,
            detail_override=f"找不到 MCP 伺服器：{server_name}",
        )
    try:
        if body is not None and body.isolation_ack is not None:
            ok = await _manual_start(server_name, body, identity, protocol_manager, http_request)
        else:
            ok = await protocol_manager.restart_server(server_name)
        status = "restarted" if ok else "failed"
    except McpServerBusyError:
        ok, status = False, "busy"
    info = await protocol_manager.get_active_server_info()
    new_status = next((s for s in info if s.get("name") == server_name), None)
    if status == "failed" and new_status and (
        new_status.get("isolation") or {}
    ).get("state") == "running":
        ok, status = True, "restarted"
    if status == "failed" and new_status and (new_status.get("isolation") or {}).get("state") in (
        "waiting_isolation", "waiting_probe", "suspended",
    ):
        status = "waiting"
    logger.warning("AUDIT: %s 重啟 MCP 伺服器 %s（ok=%s）", identity.agent_id, server_name, ok)
    return {"status": status, "server": new_status}


@router.get("/{server_name}/tools", response_model=MCPServerToolsResponse)
async def list_server_tools(
    server_name: str,
    http_request: Request,
    identity: AgentIdentity = Depends(_require_mcp_role),  # noqa: B008
    protocol_manager: ProtocolManager = Depends(_get_protocol_manager),  # noqa: B008
) -> dict[str, Any]:
    """列出單一伺服器的**完整**工具面與各自的開放狀態（ADR-0195 軸 A 治理 UI 的資料源）。

    刻意不走 ``get_all_tools``：那條路已依 ``enabled_tools`` 濾除關閉者，拿來渲染勾選框會讓
    取消勾選的工具直接從清單消失、再也勾不回來。未連線時回空 ``tools``（前端顯示「請先連線」），
    不是錯誤——伺服器可能只是尚未啟動。
    """
    cfg = protocol_manager.registry.get_server(server_name)
    if cfg is None:
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR,
            detail_override=f"找不到 MCP 伺服器：{server_name}",
        )
    try:
        catalog = await protocol_manager.list_server_tool_descriptors(
            server_name,
            request_context=_build_mcp_request_context(
                http_request,
                identity,
                server_name,
            ),
        )
    except Exception as exc:
        logger.warning(
            "Failed to list tools for MCP server %s: %s",
            server_name, protocol_manager.error_message(server_name, exc),
        )
        catalog = McpToolCatalog(
            source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
        )
    return {
        "server": server_name,
        "source_protocol_version": catalog.source_protocol_version,
        "supported": catalog.supported,
        "issues": catalog.issues,
        "enabled_tools": cfg.enabled_tools,
        "disabled_tools": cfg.disabled_tools,
        "tools": [
            MCPServerToolItem(
                **descriptor.model_dump(),
                enabled=cfg.allows_tool(descriptor.name),
            )
            for descriptor in catalog.items
        ],
    }


def _build_mcp_request_context(
    request: Request,
    identity: AgentIdentity,
    server_name: str,
) -> McpRequestContext:
    """把已驗證路由身分錨定成精確 MCP request context。"""
    request_id = str(
        getattr(
            request.state,
            "request_id",
            f"mcp-api-{identity.agent_id}-{server_name}",
        )
    )
    return McpRequestContext(
        request_id=request_id,
        identity=identity,
        human_principal_id=getattr(
            request.state,
            "human_principal_id",
            identity.agent_id if identity.is_human else None,
        ),
        installation_id=server_name,
        auth_level="Manual",
        origin=McpRequestOrigin.FOREGROUND,
    )


def _require_known_mcp_server(protocol_manager: Any, server_name: str) -> None:
    if protocol_manager.registry.get_server(server_name) is None:
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR,
            detail_override=f"找不到 MCP 伺服器：{server_name}",
        )


def _require_mcp_data_access(
    identity: AgentIdentity,
    server_name: str,
) -> None:
    """MCP data plane 除管理角色外，仍須通過該身分的 server grant。"""
    if not permission_service.is_mcp_server_allowed(server_name, identity):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)


def _raise_mcp_data_error(
    operation: str,
    server_name: str,
    exc: BaseException,
    protocol_manager: ProtocolManager,
) -> None:
    message = protocol_manager.error_message(server_name, exc)
    if isinstance(exc, McpMrtrAuthorizationError):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED) from None
    if isinstance(exc, McpMrtrError):
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR,
            detail_override=message,
        ) from None
    if isinstance(exc, ValueError):
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR,
            detail_override=message,
        ) from None
    logger.warning(
        "MCP %s failed for server %s: %s",
        operation,
        server_name,
        message,
    )
    raise ToukaException(ErrorCode.SYSTEM_ERROR) from None


@router.get(
    "/{server_name}/resources",
    response_model=MCPServerResourcesResponse,
)
async def list_server_resources(
    server_name: str,
    http_request: Request,
    identity: AgentIdentity = Depends(_require_mcp_role),  # noqa: B008
    protocol_manager: ProtocolManager = Depends(_get_protocol_manager),  # noqa: B008
) -> dict[str, Any]:
    """列出 server 真正宣告的 resources；未支援時 supported=false。"""
    _require_known_mcp_server(protocol_manager, server_name)
    _require_mcp_data_access(identity, server_name)
    try:
        catalog: McpResourceCatalog = await protocol_manager.list_server_resources(
            server_name,
            request_context=_build_mcp_request_context(
                http_request,
                identity,
                server_name,
            ),
        )
    except Exception as exc:
        _raise_mcp_data_error("resources/list", server_name, exc, protocol_manager)
    return {"server": server_name, **catalog.model_dump()}


@router.get(
    "/{server_name}/resource-templates",
    response_model=MCPServerResourceTemplatesResponse,
)
async def list_server_resource_templates(
    server_name: str,
    http_request: Request,
    identity: AgentIdentity = Depends(_require_mcp_role),  # noqa: B008
    protocol_manager: ProtocolManager = Depends(_get_protocol_manager),  # noqa: B008
) -> dict[str, Any]:
    """列出 server 真正宣告的 resource templates。"""
    _require_known_mcp_server(protocol_manager, server_name)
    _require_mcp_data_access(identity, server_name)
    try:
        catalog: McpResourceTemplateCatalog = await protocol_manager.list_server_resource_templates(
            server_name,
            request_context=_build_mcp_request_context(
                http_request,
                identity,
                server_name,
            ),
        )
    except Exception as exc:
        _raise_mcp_data_error("resources/templates/list", server_name, exc, protocol_manager)
    return {"server": server_name, **catalog.model_dump()}


@router.post(
    "/{server_name}/resources/read",
    response_model=MCPResourceReadResponse,
)
async def read_server_resource(
    server_name: str,
    payload: MCPResourceReadRequest,
    http_request: Request,
    identity: AgentIdentity = Depends(_require_mcp_role),  # noqa: B008
    protocol_manager: ProtocolManager = Depends(_get_protocol_manager),  # noqa: B008
) -> dict[str, Any]:
    """經 URI/MIME/bytes/DLP 邊界讀取 MCP resource；不碰本機 filesystem。"""
    verify_origin(http_request)
    _require_known_mcp_server(protocol_manager, server_name)
    _require_mcp_data_access(identity, server_name)
    try:
        result = await protocol_manager.read_server_resource(
            server_name,
            payload.uri,
            request_context=_build_mcp_request_context(
                http_request,
                identity,
                server_name,
            ),
        )
    except Exception as exc:
        _raise_mcp_data_error("resources/read", server_name, exc, protocol_manager)
    return {"server": server_name, "uri": payload.uri, "result": result}


@router.get(
    "/{server_name}/prompts",
    response_model=MCPServerPromptsResponse,
)
async def list_server_prompts(
    server_name: str,
    http_request: Request,
    identity: AgentIdentity = Depends(_require_mcp_role),  # noqa: B008
    protocol_manager: ProtocolManager = Depends(_get_protocol_manager),  # noqa: B008
) -> dict[str, Any]:
    """列出 server 真正宣告的 prompts；未支援時 supported=false。"""
    _require_known_mcp_server(protocol_manager, server_name)
    _require_mcp_data_access(identity, server_name)
    try:
        catalog: McpPromptCatalog = await protocol_manager.list_server_prompts(
            server_name,
            request_context=_build_mcp_request_context(
                http_request,
                identity,
                server_name,
            ),
        )
    except Exception as exc:
        _raise_mcp_data_error("prompts/list", server_name, exc, protocol_manager)
    return {"server": server_name, **catalog.model_dump()}


@router.post(
    "/{server_name}/prompts/get",
    response_model=MCPPromptGetResponse,
)
async def get_server_prompt(
    server_name: str,
    payload: MCPPromptGetRequest,
    http_request: Request,
    identity: AgentIdentity = Depends(_require_mcp_role),  # noqa: B008
    protocol_manager: ProtocolManager = Depends(_get_protocol_manager),  # noqa: B008
) -> dict[str, Any]:
    """取得強型別 prompt；外部訊息在 canonical/API 永遠標記 untrusted。"""
    verify_origin(http_request)
    _require_known_mcp_server(protocol_manager, server_name)
    _require_mcp_data_access(identity, server_name)
    try:
        result = await protocol_manager.get_server_prompt(
            server_name,
            payload.name,
            payload.arguments,
            request_context=_build_mcp_request_context(
                http_request,
                identity,
                server_name,
            ),
        )
    except Exception as exc:
        _raise_mcp_data_error("prompts/get", server_name, exc, protocol_manager)
    return {"server": server_name, "name": payload.name, "result": result}


def _redact_env(config_dict: dict[str, Any]) -> dict[str, Any]:
    """GET write-only：遮罩 mcpServers 各 server 的 env secret 類值（非機密鍵維持明文）。"""
    for server in (config_dict.get("mcpServers") or {}).values():
        env = server.get("env")
        if isinstance(env, dict):
            server["env"] = {k: mask_value(k, v) for k, v in env.items()}
    return config_dict


def _preserve_masked_env(
    incoming: dict[str, Any], existing_servers: dict[str, Any]
) -> dict[str, Any]:
    """POST write-only：還原前端不應竄改的磁碟既值。

    - 遮罩佔位的 env 值 → 還原為磁碟既值（sentinel 永不落盤）。
    - ``vault_jail_arg``（per-agent 隔離政策）→ 前端未回傳（None）時自磁碟還原，避免一次設定
      存檔就把隔離政策清掉（複審 High：jail fails open on routine save）。
    - ``enabled_tools``／``disabled_tools``（ADR-0195 軸 A 工具治理）→ 同理由還原：任何不認得
      這兩個欄位的寫入路徑（舊前端、hub install、鍵遷移）都不得靜默把治理政策沖掉。
      要真正「全部開啟」請送 ``enabled_tools=["*"]``（明示值），而非 ``None``。
    - P6 cache/listener/header/icon/telemetry 政策同樣是 installation 安全設定；舊前端未回傳
      時必須保留，不能因日常存檔重置成預設。
    """
    for name, server in (incoming.get("mcpServers") or {}).items():
        old = existing_servers.get(name) or {}
        preserved = mcp_tool_policy.carry_over_policy(server, old)
        server.clear()
        server.update(preserved)
        env = server.get("env")
        if isinstance(env, dict):
            old_env = old.get("env") or {}
            server["env"] = {k: (old_env.get(k, "") if is_masked(v) else v) for k, v in env.items()}
        if not server.get("vault_jail_arg") and old.get("vault_jail_arg"):
            server["vault_jail_arg"] = old["vault_jail_arg"]
        if server.get("enabled_tools") is None and old.get("enabled_tools") is not None:
            server["enabled_tools"] = old["enabled_tools"]
        if not server.get("disabled_tools") and old.get("disabled_tools"):
            server["disabled_tools"] = old["disabled_tools"]
        for field in (
            "cache_enabled",
            "cache_max_entries",
            "cache_default_ttl_ms",
            "subscriptions_enabled",
            "subscription_max_backoff_seconds",
            "approved_header_tools",
            "icons_enabled",
            "telemetry_enabled",
        ):
            if field not in server and field in old:
                server[field] = old[field]
    return incoming


def _prepare_config_for_save(
    config: MCPConfigResponse,
    existing_servers: dict[str, Any],
) -> dict[str, Any]:
    """保留 wire 未明示欄位；避免 Pydantic 預設值覆寫磁碟既有政策。"""
    incoming = config.model_dump(exclude_unset=True)
    return _preserve_masked_env(incoming, existing_servers)


@router.get("/ui/config", response_model=MCPConfigResponse)
async def get_mcp_config(
    _identity: AgentIdentity = Depends(_require_mcp_role),  # noqa: B008
) -> dict[str, Any]:
    """讀取 MCP 伺服器配置檔案 (server_config.json)。

    M4 fix: validates the on-disk config through MCPConfigResponse.model_validate()
    so that any injected malicious commands in server_config.json are rejected
    before being returned to the caller.
    """
    config_path = get_mcp_config_path()
    try:
        data = resource_manager.read_json(config_path)
        if not data:
            return {"mcpServers": {}}

        # M4 fix: validate through Pydantic — rejects shell injection in command fields
        try:
            validated = MCPConfigResponse.model_validate(data)
            # P1：回傳前遮罩 env secret 類值（已設定不洩漏內容；write-only）。
            return _redact_env(validated.model_dump())
        except ValidationError as ve:
            logger.warning("⚠️ [MCP] Config validation failed (potential injection): %s", ve)
            return {"mcpServers": {}}
    except Exception:
        logger.exception("Failed to read mcp config")
        return {"mcpServers": {}}


def _is_human_requester(identity: AgentIdentity) -> bool:
    """人類或 admin 代打（判定沿用授權閘道，不在 router 自己實作）。"""
    try:
        require_human_requester(identity)
    except ToukaException:
        return False
    return True


def _existing_servers(config_path: Path) -> dict[str, Any]:
    """讀磁碟上的條目；存在卻讀不到時丟 ``PERSISTENCE_ERROR``（不當成空的，否則每台都變新名稱）。"""
    raw = _read_json_or_raise(str(config_path))
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    return servers if isinstance(servers, dict) else {}


async def _forget_deleted(store: McpIsolationStore, names: set[str]) -> None:
    """刪掉的條目寫完設定之後才清紀錄；清不掉只記錄（孤兒紀錄會被忽略，新名稱會整筆覆寫）。"""
    try:
        await store.amutate(delete(names))
    except ToukaException:
        logger.warning("AUDIT: MCP isolation records of %d deleted server(s) kept", len(names))


@router.post("/ui/config")
async def save_mcp_config(
    config: MCPConfigResponse,
    identity: AgentIdentity = Depends(_require_mcp_role),  # noqa: B008
) -> McpConfigSaveResponse:
    """保存 MCP 伺服器配置並嘗試熱重載服務。

    隔離設定檔（ADR-0275）：新名稱與程式變了的條目先寫紀錄、再寫設定；刪掉的條目
    寫完設定才清紀錄。請求與回應的形狀照舊，只多 ``isolation_cleared``。
    """
    config_path = get_mcp_config_path()
    # P1 write-only：收到的遮罩佔位 env 還原為磁碟既值（sentinel 永不落盤）。
    existing_servers = _existing_servers(config_path)
    resolved = _prepare_config_for_save(config, existing_servers)
    incoming = resolved.get("mcpServers") or {}
    store = McpIsolationStore(config_path.parent)
    cleared: list[str] = []
    human = _is_human_requester(identity)
    if names := changed_programs(existing_servers, incoming, human=human):
        reconcile = reconcile_saved_entries(
            existing_servers, incoming, names, human=human
        )
        cleared = await store.amutate(reconcile)
    try:
        resource_manager.make_dir(config_path.parent)
        resource_manager.write_json(config_path, resolved, indent=4)
        if removed := set(existing_servers) - set(incoming):
            await _forget_deleted(store, removed)

        # 嘗試重新載入 ProtocolRegistry
        try:
            from src.main import protocol_manager, registry

            registry.load_config()
            await protocol_manager.initialize_all()
            logger.info("♻️ MCP servers reloaded after config update")
        except Exception as re_err:
            logger.warning(f"Failed to reload MCP servers in-place: {re_err}")

        # 同步更新「動態調度員」技能的內容 (SSOT)
        try:
            from src.services.skill_service import skill_service

            skill_service.sync_system_dispatcher()
        except Exception:
            logger.exception("Failed to sync dispatcher after config update")
    except Exception as e:
        logger.exception("Failed to save mcp config")
        raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(e)) from e
    return McpConfigSaveResponse(isolation_cleared=cleared)


# ── 隔離設定（ADR-0275）：來源與額外允許的資料夾只經這兩個端點改 ─────────────

MCP_SERVER_NOT_FOUND = "mcp_server_not_found"
MCP_ORIGIN_CHANGE_REFUSED = "mcp_origin_change_refused"
MCP_SANDBOX_PATHS_REFUSED = "mcp_sandbox_paths_refused"
MCP_SANDBOX_PATH_REFUSED = "mcp_sandbox_path_refused"
"""422 的 ``detail_override`` 代碼（前端用它找翻譯）；逐筆拒絕時是 ``<代碼>:<第幾筆>:<原因>``。"""
_KNOWN_ORIGINS = frozenset(get_args(McpOrigin))


def _classify_sync(
    server_name: str, entry: dict[str, Any], record: OriginRecord
) -> McpIsolationClass:
    """``classify``（會讀檔案系統中繼資料與 cwd，只在執行緒裡跑）；條目結構不對時當自建."""
    raw = raw_entry(entry)
    if raw is None:
        return "self"
    layout = current_layout()
    return classify(server_name, raw, record, layout, base_dir=proxy_base_dir()).kind


async def _classify_entry(
    server_name: str, entry: dict[str, Any], origin: str | None, *, records_unreadable: bool
) -> McpIsolationClass:
    """依紀錄（``effective_origin``／讀不到）與條目引用的路徑分類；啟發式只會更嚴.

    紀錄的對映只經 ``mcp_launch.origin_record``（和啟動判定同一份）。
    """
    record = origin_record(origin, records_unreadable=records_unreadable)
    return await asyncio.to_thread(_classify_sync, server_name, entry, record)


def _current_entry(server_name: str) -> dict[str, Any]:
    """磁碟上的條目（含停用的）；沒有就 422。"""
    entry = _existing_servers(get_mcp_config_path()).get(server_name)
    if not isinstance(entry, dict):
        raise ToukaException(ErrorCode.VALIDATION_ERROR, detail_override=MCP_SERVER_NOT_FOUND)
    return entry


async def _isolation_view(
    server_name: str, entry: dict[str, Any], view: RecordView
) -> dict[str, Any]:
    origin = view.effective_origin
    isolation_class = await _classify_entry(
        server_name, entry, origin, records_unreadable=view.records_unreadable
    )
    return {
        "server": server_name,
        "isolation_class": isolation_class,
        "origin": origin if origin in _KNOWN_ORIGINS else None,
        "record_state": view.state,
        "sandbox_paths": (
            list(view.usable_sandbox_paths)
            if isolation_class in ("third_party", "scaffold")
            else []
        ),
        "bound": view.bound,
        "can_mark_agent": (
            not view.records_unreadable
            and isolation_class != "remote"
            and origin_category(origin) in (None, "third_party")
        ),
    }


async def _refused_grant(grants: list[SandboxPathGrant]) -> str | None:
    """逐筆過永不授權（同第三方引用路徑的規則）；回第一筆被拒的 ``<第幾筆>:<原因>``。"""
    layout = current_layout()
    for index, grant in enumerate(grants):
        reason = await authorize_derived_path(Path(grant.path), layout, owner=None)
        if reason is not None:
            return f"{MCP_SANDBOX_PATH_REFUSED}:{index}:{reason}"
    return None


@router.get("/{server_name}/isolation", response_model=McpIsolationView)
async def get_mcp_isolation(
    server_name: str,
    _identity: Annotated[AgentIdentity, Depends(_require_mcp_role)],
) -> dict[str, Any]:
    """一台伺服器的來源、紀錄狀態與額外允許的資料夾（只有代碼）。"""
    entry = _current_entry(server_name)
    store = McpIsolationStore(get_mcp_config_path().parent)
    return await _isolation_view(server_name, entry, store.record_for(server_name, entry))


@router.put("/{server_name}/isolation", response_model=McpIsolationUpdateResponse)
async def put_mcp_isolation(
    server_name: str,
    body: McpIsolationUpdateRequest,
    http_request: Request,
    _human: Annotated[AgentIdentity, Depends(require_human_requester)],
    identity: Annotated[AgentIdentity, Depends(_require_mcp_role)],
) -> dict[str, Any]:
    """改來源（只能改成 agent）或額外允許的資料夾；存檔後背景重新判定並連線。

    人類請求者（Depends，403）、privileged、CSRF；``bound`` 必須等於目前條目的（422
    ``mcp_entry_changed``）；紀錄讀不到時 500 ``PERSISTENCE_ERROR``。
    """
    verify_origin(http_request)
    protocol_manager = _get_protocol_manager(http_request)
    entry = _current_entry(server_name)
    store = McpIsolationStore(get_mcp_config_path().parent)
    snapshot = store.load()
    view = snapshot.view(server_name, entry)
    if body.bound != view.bound:
        raise ToukaException(ErrorCode.VALIDATION_ERROR, detail_override=ENTRY_CHANGED)
    if view.records_unreadable:
        raise ToukaException(ErrorCode.PERSISTENCE_ERROR)
    origin = view.effective_origin
    current = await _isolation_view(server_name, entry, view)
    if body.origin is not None:
        if not current["can_mark_agent"]:
            raise ToukaException(
                ErrorCode.VALIDATION_ERROR, detail_override=MCP_ORIGIN_CHANGE_REFUSED
            )
        origin = body.origin
    isolation_class = await _classify_entry(server_name, entry, origin, records_unreadable=False)
    grants = list(view.usable_sandbox_paths)
    if body.sandbox_paths is not None:
        if body.sandbox_paths and isolation_class not in ("third_party", "scaffold"):
            raise ToukaException(
                ErrorCode.VALIDATION_ERROR, detail_override=MCP_SANDBOX_PATHS_REFUSED
            )
        if refused := await _refused_grant(body.sandbox_paths):
            raise ToukaException(ErrorCode.VALIDATION_ERROR, detail_override=refused)
        grants = body.sandbox_paths
    if isolation_class not in ("third_party", "scaffold"):
        grants = []
    await store.amutate(
        replace_isolation(
            server_name,
            origin,
            view.bound,
            grants,
            expected=snapshot.records.get(server_name),
        )
    )
    protocol_manager.schedule_relaunch(server_name)
    logger.warning(
        "AUDIT: %s changed MCP isolation of %s (origin=%s, sandbox_paths=%d)",
        identity.agent_id,
        server_name,
        origin,
        len(grants),
    )
    return await _isolation_view(server_name, entry, store.record_for(server_name, entry))


NPM_CACHE = {}
NPM_CACHE_TIME = 0

# N6 fix: strict allowlist regex for npm package names.
# Accepts: simple names (express, lodash-es), scoped packages (@scope/name).
# Rejects: path traversal (../), absolute paths, spaces, uppercase, reserved names.
_NPM_PACKAGE_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9\-_.]{0,213}/)?[a-z0-9][a-z0-9\-_.]{0,213}$")
_NPM_RESERVED = frozenset({"node_modules", "favicon.ico"})


def _validate_npm_package_name(package: str) -> bool:
    """Return True if package is a valid, safe npm package name (N6 fix).

    Prevents path traversal in the constructed npmjs.org URL.
    """
    if not package or len(package) > 214:
        return False
    name_part = package.split("/")[-1]
    if name_part in _NPM_RESERVED or name_part.startswith("."):
        return False
    return bool(_NPM_PACKAGE_RE.match(package))


async def fetch_npm_downloads(package: str) -> int | None:
    """Fetch download count for an npm package from the registry.

    N6 fix: validates package name before constructing the URL to prevent
    path traversal SSRF attacks.  Also disables follow_redirects to prevent
    open-redirect SSRF chains.

    誠實語意：回傳 int（含真 0）＝npm 的真實數據；回傳 None＝無法得知
    （非法名稱、404／非 200、網路例外）。呼叫端不得把 None 偽造成數字。
    """
    if not _validate_npm_package_name(package):
        logger.warning("🚫 [NPM] Rejected invalid package name: %r", package)
        return None

    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            res = await client.get(f"https://api.npmjs.org/downloads/point/last-month/{package}")
            if res.status_code == 200:
                data = res.json()
                count = data.get("downloads")
                return count if isinstance(count, int) and count >= 0 else None
    except Exception as e:
        logger.warning(f"Failed to fetch NPM downloads for {package}: {e}")
    return None


@router.get("/hub", response_model=MCPHubResponse)
async def get_mcp_hub(
    _identity: AgentIdentity = Depends(_require_mcp_role),  # noqa: B008
) -> dict[str, Any]:
    """獲取精選的 MCP 伺服器列表索引，並自動抓取 npm 下載量來排行"""
    global NPM_CACHE, NPM_CACHE_TIME

    hub_data = []
    hub_config_path = get_mcp_hub_path()
    try:
        hub_data_raw = resource_manager.read_json(hub_config_path)
        if hub_data_raw:
            hub_data = (
                hub_data_raw.get("hub", []) if isinstance(hub_data_raw, dict) else hub_data_raw
            )
    except Exception:
        logger.exception("Failed to read mcp hub config")

    if not hub_data or not isinstance(hub_data, list):
        return {"hub": []}

    current_time = get_current_epoch()

    # Cache expiring logic (1 hour)
    if current_time - NPM_CACHE_TIME > 3600:
        tasks = []
        packages = []
        for item in hub_data:
            if "npm_package" in item:
                tasks.append(fetch_npm_downloads(item["npm_package"]))
                packages.append(item["id"])

        if tasks:
            results = await asyncio.gather(*tasks)
            for pkg_id, count in zip(packages, results, strict=True):
                # 誠實快取：int（含真 0）＝npm 真實數據；None＝抓取失敗（一小時後重試）
                NPM_CACHE[pkg_id] = count
        NPM_CACHE_TIME = current_time

    # 只有真實 npm 數據才寫入 downloads；無數據省略欄位（前端顯示「精選」），
    # 嚴禁以假數字（舊行為：100）或 popularity 冒充下載量。
    for item in hub_data:
        count = NPM_CACHE.get(item["id"])
        if count is not None:
            item["downloads"] = count
        else:
            item.pop("downloads", None)

    # 徽章比對鍵（ADR-0190）：install 落盤鍵＝derive_server_key(name)（ADR-0102），
    # hub.id 帶 curated-/official- 前綴永遠對不上——由後端投影單一 SSOT，前端直接查表。
    for item in hub_data:
        if item.get("name"):
            item["server_key"] = derive_server_key(item["name"])

    # 排序：有真實下載量用下載量，無數據退回 popularity
    # （官方置頂權重 999999／registry 條目預設 0，僅作排序權重、不對外顯示為下載量）。
    hub_data.sort(
        key=lambda x: x["downloads"] if x.get("downloads") is not None else x.get("popularity", 0),
        reverse=True,
    )

    return {"hub": hub_data}


@router.post("/hub/sync")
async def sync_mcp_hub(
    _identity: AgentIdentity = Depends(_require_mcp_role),  # noqa: B008
) -> Any:
    """手動觸發 MCP Hub 清單同步"""
    svc = MCPHubService(hub_path=get_mcp_hub_path(), config_path=get_mcp_config_path())
    try:
        result = await svc.sync()
        return {"status": result["status"], "message": f"Synced {result.get('synced', 0)} servers"}
    except Exception as e:
        logger.exception("Sync failed")
        return JSONResponse(status_code=500, content={"error": str(e)})


class MCPInstallRequest(BaseModel):
    """從 Hub 安裝 MCP 伺服器的請求模型。"""

    hub_id: str
    env_vars: dict[str, str] = {}


@router.post("/hub/install")
async def install_mcp_from_hub(
    request: MCPInstallRequest,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> Any:
    """從 Hub 安裝指定的 MCP 伺服器到系統配置。

    N3 fix: requires Owner or admin identity — installing an MCP server writes
    to server_config.json which then gets executed, making this a privileged action.
    """
    if not identity or not identity.is_owner_or_admin():
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    svc = MCPHubService(hub_path=get_mcp_hub_path(), config_path=get_mcp_config_path())
    result = await svc.install(hub_id=request.hub_id, env_vars=request.env_vars)

    if result["status"] == "not_found":
        return JSONResponse(
            status_code=404, content={"error": f"'{request.hub_id}' not found in hub"}
        )

    # 嘗試熱插拔重載 MCP 連線
    try:
        from src.main import protocol_manager

        protocol_manager.registry.load_config()
        await protocol_manager.initialize_all()
    except ImportError:
        logger.warning("Could not reload protocol_manager: main not found")
    except AttributeError:
        logger.exception("Reload failed: protocol_manager missing attributes")

    return {"status": "success", "message": f"Installed {result['server_name']}"}
