"""MCP 協議管理器 (Protocol Manager).

負責載入、連線與管理 MCP 伺服器，提供工具解析、斷路器保護與熱插拔支援。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

from mcp import types
from mcp.client import CacheConfig
from mcp.shared.exceptions import MCPError

from src.config import get_config_dir
from src.core.circuit_breaker import BreakerNeutralError, CircuitBreaker, CircuitState
from src.core.exceptions import ErrorCode
from src.core.retry import with_retry
from src.protocols.ide.discovery import discover_ide_servers
from src.protocols.ide.handler import call_ide_tool
from src.protocols.mcp.cache import PrincipalAwareResponseCacheStore
from src.protocols.mcp.canonical import (
    ExecutionKind,
    McpCatalogIssue,
    McpInvocationAuthorization,
    McpOperationException,
    McpPromptCatalog,
    McpResourceCatalog,
    McpResourceTemplateCatalog,
    McpToolCatalog,
    McpTransportBootstrapAuthorization,
    McpWorkloadWireAttempt,
)
from src.protocols.mcp.canonical import (
    digest_mcp_arguments as digest_wire_payload,
)
from src.protocols.mcp.catalog import (
    MCP_LIST_TIMEOUT_SECONDS,
    collect_paginated,
    validate_catalog_name,
    validate_prompt_arguments,
    validate_resource_uri,
)
from src.protocols.mcp.client import McpIsolatedLaunch, connect_mcp_server, tool_base_url
from src.protocols.mcp.handles import (
    InstallationState,
    LegacyHandlePool,
    LegacyMcpHandle,
    McpHandleKey,
)
from src.protocols.mcp.header_policy import (
    McpHeaderPolicyError,
    inspect_tool_header_policy,
    validate_header_arguments,
)
from src.protocols.mcp.icons import McpIconService
from src.protocols.mcp.interaction import InProcessMcpInteractionBroker
from src.protocols.mcp.mrtr import (
    McpMrtrAuthorizationError,
    McpMrtrBinding,
    McpMrtrDriver,
    digest_mcp_arguments,
)
from src.protocols.mcp.oauth.workload.target import (
    McpOAuthTargetBindingError,
    canonical_mcp_transport_url,
)
from src.protocols.mcp.owner import (
    OWNER_CLOSE_GRACE_SECONDS,
    OWNER_CLOSE_SECONDS,
    McpConnectionAborted,
    McpConnectionOwner,
    root_cause,
)
from src.protocols.mcp.schema_validation import McpSchemaError, normalize_mcp_schema
from src.protocols.mcp.sdk_adapter import (
    UNKNOWN_PROTOCOL_VERSION,
    adapt_call_exception,
    adapt_call_tool_result,
    adapt_get_prompt_result,
    adapt_prompt_descriptor,
    adapt_read_resource_result,
    adapt_resource_descriptor,
    adapt_resource_template_descriptor,
    adapt_tool_descriptor,
)
from src.protocols.mcp.subscriptions import McpSubscriptionSupervisor
from src.protocols.mcp.telemetry import McpTelemetry
from src.services.auth_service import AuthService
from src.services.isolation.mcp_launch import (
    McpLaunch,
    McpLaunchWaiting,
    McpManualStart,
    entry_identity,
    mcp_launch_book,
    mcp_launcher,
)
from src.services.isolation.status import isolation_monitor
from src.services.mcp_isolation_store import McpIsolationStore, bound_of
from src.services.permission_service import permission_service
from src.utils.async_db import session_service
from src.utils.mcp_secrets import redact_values, sanitize_diagnostic, secret_values
from src.utils.task_utils import spawn_logged
from src.utils.time_utils import get_current_epoch, get_monotonic_ms

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from src.protocols.mcp.canonical import (
        McpCompleteOutcome,
        McpPromptDescriptor,
        McpPromptResult,
        McpRequestContext,
        McpResourceDescriptor,
        McpResourceReadResult,
        McpResourceTemplateDescriptor,
        McpToolDescriptor,
    )
    from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
    from src.services.isolation.consent import Consent
    from src.services.isolation.mcp_launch import (
        IsolationRecordReader,
        McpLaunchBook,
        McpLauncher,
    )
    from src.services.isolation.status import IsolationMonitor, IsolationSnapshot

logger = logging.getLogger(__name__)

MCP_RELAUNCH_CONCURRENCY = 4
MCP_HTTP_RESTART_LOCK_SECONDS = 5.0
MCP_HTTP_RESTART_SECONDS = 15.0
MCP_LOST_RELAUNCH_DELAY_SECONDS = 5.0
MCP_LOST_RELAUNCH_LIMIT = 3
MCP_LOST_RELAUNCH_RESET_SECONDS = 600.0
_JUDGED_DIRECT = object()
MCP_SERVER_WAITING = "MCP server is waiting for isolation; it has not started."
MCP_SERVER_BUSY = "MCP server is reconnecting; try again shortly."


@dataclass
class _RelaunchIntent:
    due: float
    epoch: int
    owner: McpConnectionOwner | None
    dirty: bool = True
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class McpOAuthAuthService(Protocol):
    """Narrow request-time bearer seam injected by the application lifespan。"""

    def bearer_credential_guard(self, **kwargs: Any) -> Any:
        """Return one interactive OAuth credential context manager。"""
        ...

    async def authorize_operation(
        self,
        **kwargs: Any,
    ) -> None:
        """Authorize one interactive request-time OAuth operation。"""
        ...


class McpWorkloadOAuthService(Protocol):
    """Dedicated P8-B authorization and bearer owner。"""

    def workload_bearer_credential_guard(self, **kwargs: Any) -> Any:
        """Return one exact-ticket workload credential context manager。"""
        ...

    async def authorize_operation(
        self,
        **kwargs: Any,
    ) -> McpInvocationAuthorization:
        """Authorize one exact heartbeat tool invocation。"""
        ...

    async def authorize_transport_bootstrap(
        self,
        **kwargs: Any,
    ) -> McpTransportBootstrapAuthorization:
        """Authorize only authenticated transport bootstrap and catalog sends。"""
        ...


_CURRENT_WORKLOAD_AUTHORIZATION: ContextVar[
    McpInvocationAuthorization | McpTransportBootstrapAuthorization | None
] = ContextVar("current_mcp_workload_authorization", default=None)


_AUTH_LEVEL_RANK = {
    "Manual": 0,
    "Auto": 1,
    "YOLO": 2,
}
# ADR-0246 U2c-2：心跳的 MCP 呼叫已正確標成 heartbeat，但讓無人在場的心跳用 workload
# OAuth（ADR-0207）是新能力，要不要打開留給人類決定；在那之前維持關閉（同 07eeab59 前）。
HEARTBEAT_WORKLOAD_OAUTH_ENABLED = False


def _protocol_version(client: Any) -> str:
    """讀取已 negotiation 的版本；不得拿 server identity 欄位作授權。"""
    try:
        version = getattr(client, "protocol_version", None)
    except RuntimeError:  # life1：失聯後已關閉的 SDK Client 仍留在 pool
        return UNKNOWN_PROTOCOL_VERSION
    return str(version or UNKNOWN_PROTOCOL_VERSION)


def _modern_low_level_session(client: Any) -> Any | None:
    """取得 SDK v2 mechanics session；legacy／test double 維持既有高階路徑。"""
    if _protocol_version(client) != types.LATEST_PROTOCOL_VERSION:
        return None
    try:
        low_level = getattr(client, "session", None)
    except RuntimeError:
        return None
    return low_level if low_level is not None and low_level is not client else None


def _connection_fact(value: Any) -> dict[str, Any] | None:
    """把 SDK connection fact 轉為健康頁可序列化顯示資料。"""
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json", exclude_none=True, by_alias=True)
        return dumped if isinstance(dumped, dict) else None
    return None


def _stricter_auth_level(original: str, current: str) -> str:
    """有效等級只能維持或收緊；未知值 fail-safe 視為 Manual。"""
    original_rank = _AUTH_LEVEL_RANK.get(original, _AUTH_LEVEL_RANK["Manual"])
    current_rank = _AUTH_LEVEL_RANK.get(current, _AUTH_LEVEL_RANK["Manual"])
    effective_rank = min(original_rank, current_rank)
    return next(name for name, rank in _AUTH_LEVEL_RANK.items() if rank == effective_rank)


class ProtocolError(ValueError):
    """MCP 協議管理器基礎異常。"""


def _raise_protocol_error(message: str) -> NoReturn:
    """Raise the manager's stable error type from one lint-clean boundary。"""
    raise ProtocolError(message)


class McpToolCallRefusal(ProtocolError, BreakerNeutralError):
    """呼叫前的本機拒絕：不是上游故障，對斷路器中立、不寫健康頁（ADR-0203 dep1）。"""

    reason: str


class McpToolArgumentRefusal(McpToolCallRefusal):
    """模型給的參數放進標頭後超過上限；縮短參數即可重試。訊息不含參數值。"""

    reason = "mcp_header_argument_invalid"

    def __init__(self, server_name: str, tool_name: str, detail: str) -> None:
        super().__init__(f"MCP tool '{tool_name}' on '{server_name}' refused: {detail}.")


class McpToolCatalogRefusal(McpToolCallRefusal):
    """呼叫當下的伺服器工具清單不允許這次呼叫（ADR-0203 dep1）；不是上游故障。

    ``quarantined``：同名有隔離紀錄或同名多筆；否則是當次清單找不到或伺服器不支援列工具。
    """

    def __init__(self, server_name: str, tool_name: str, *, quarantined: bool) -> None:
        self.quarantined = quarantined
        self.reason = "mcp_tool_quarantined" if quarantined else "mcp_tool_not_listed"
        super().__init__(f"MCP tool '{tool_name}' on '{server_name}' refused: {self.reason}.")


def _validate_catalog_header_policy(
    catalog: McpToolCatalog,
    *,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    approved_for_sensitive: bool,
) -> None:
    """呼叫當下核對清單（ADR-0203 dep1），再驗宣告的 header policy。

    同名有隔離紀錄（icon 問題只記紀錄、不算）或同名多筆 → 隔離拒絕；當次清單找不到或
    不支援列工具 → 不在清單。SDK 驗結果時用自己快取的 schema，不能只靠廣告面擋。
    """
    matching = [descriptor for descriptor in catalog.items if descriptor.name == tool_name]
    quarantined = len(matching) > 1 or any(
        issue.item_name == tool_name and not issue.code.startswith("McpIcon:")
        for issue in catalog.issues
    )
    if quarantined or not catalog.supported or not matching:
        raise McpToolCatalogRefusal(server_name, tool_name, quarantined=quarantined)
    policy = inspect_tool_header_policy(
        matching[0].input_schema,
        approved_for_sensitive=approved_for_sensitive,
    )
    try:
        validate_header_arguments(policy, arguments)
    except McpHeaderPolicyError as exc:
        raise McpToolArgumentRefusal(server_name, tool_name, str(exc)) from exc


class ServerNotFoundError(ProtocolError):
    """找不到指定的 MCP 伺服器異常。"""

    def __init__(self, server_name: str) -> None:
        super().__init__(f"Server '{server_name}' 不存在。")


class McpServerBusyError(ProtocolError):
    """連線鎖等待超過 HTTP 或工具呼叫的上限。"""


class McpServerWaiting(ConnectionError):
    """等待隔離環境，不是一次連線失敗。"""


class ToolDisabledError(ProtocolError):
    """該工具被伺服器條目的 per-installation 政策關閉（ADR-0195 軸 A）。

    與 ``ServerNotFoundError`` 分開是必要的：前者代表「這台不存在」，本例外代表「這台在、
    但這個工具此次安裝沒開放」——執行層據此回可讀的 PERMISSION_DENIED 而非「未知工具」，
    模型才知道不該換個名字重試。
    """

    def __init__(self, server_name: str, tool_name: str) -> None:
        self.server_name = server_name
        self.tool_name = tool_name
        super().__init__(f"工具 '{tool_name}' 已於伺服器 '{server_name}' 的安裝設定中關閉。")


class McpFeatureUnsupportedError(ProtocolError):
    """對端未宣告或未實作要求的 MCP feature family。"""

    def __init__(self, server_name: str, feature: str) -> None:
        super().__init__(f"Server '{server_name}' does not support MCP {feature}.")


def _feature_support(client: Any, feature: str) -> bool | None:
    """回傳 True/False/unknown；unknown 才允許以真方法探測。"""
    try:
        capabilities = getattr(client, "server_capabilities", None)
    except RuntimeError:  # life1：已關閉的 SDK Client；真方法會以連線錯誤失敗
        return None
    if capabilities is None:
        return None
    if isinstance(capabilities, dict):
        return feature in capabilities and capabilities.get(feature) is not None
    return getattr(capabilities, feature, None) is not None


def _is_method_not_found(exc: BaseException) -> bool:
    return isinstance(exc, MCPError) and exc.code == -32601


class ProtocolManager:
    """MCP 伺服器連接與調度管理器.

    負責 MCP 生命週期、熱插拔、工具調用與斷路器韌性。
    """

    def __init__(
        self,
        registry: ProtocolRegistry,
        *,
        icon_service: McpIconService | None = None,
        mrtr_broker: Any | None = None,
        mrtr_authorizer: Callable[[McpMrtrBinding], Awaitable[None]] | None = None,
        oauth_auth_service: McpOAuthAuthService | None = None,
        workload_oauth_service: McpWorkloadOAuthService | None = None,
        launcher: McpLauncher = mcp_launcher,
        launch_book: McpLaunchBook = mcp_launch_book,
        monitor: IsolationMonitor = isolation_monitor,
        isolation_reader: Callable[[], IsolationRecordReader] | None = None,
    ) -> None:
        """初始化協議管理器.

        Args:
            registry (ProtocolRegistry): 註冊有可用 MCP 伺服器配置的註冊表實例。
            icon_service: 可注入的安全 icon 擷取服務。
            mrtr_broker: 可注入的行程內 MRTR interaction broker。
            mrtr_authorizer: 每輪上游 retry 前的重新授權函式。
            oauth_auth_service: P8-A 互動式 OAuth runtime。
            workload_oauth_service: P8-B 單一 Proxy workload OAuth runtime。
            launcher: stdio 啟動判定。
            launch_book: 啟動狀態帳本。
            monitor: 隔離可用性與失敗回報。
            isolation_reader: 每次判定使用的隔離設定讀取器。
        """
        self.registry = registry
        # 兩個舊 dict 僅作 v1 shared-handle 相容視圖；所有 runtime 尋址都經 HandlePool。
        self._active_sessions = {}
        self._exit_stacks = {}
        self.handle_pool = LegacyHandlePool(
            self._active_sessions,
            self._exit_stacks,
        )
        self._installations: dict[str, InstallationState] = {}
        self._handle_meta: dict[McpHandleKey, dict[str, Any]] = {}
        self._connect_locks: dict[McpHandleKey, asyncio.Lock] = {}
        self._circuit_breakers: dict[str | McpHandleKey, CircuitBreaker] = {}
        self._ide_clients = {}
        # v1 相容診斷視圖；SSOT 寫入 InstallationState，舊測試/呼叫者仍可讀本 dict。
        self._server_meta: dict[str, dict[str, Any]] = {}
        # per-server 重啟鎖：序列化同一台的並發重啟，避免重複 stack.aclose 造成孤兒子進程/狀態錯亂。
        self._installation_locks: dict[str, asyncio.Lock] = {}
        self._launcher, self._book, self._monitor = launcher, launch_book, monitor
        self._isolation_reader = isolation_reader or (lambda: McpIsolationStore(get_config_dir()))
        self._relaunch_slots = asyncio.Semaphore(MCP_RELAUNCH_CONCURRENCY)
        self._relaunch_pending: dict[str, _RelaunchIntent] = {}
        self._relaunch_tasks: set[asyncio.Task] = set()
        self._lifecycle_epoch = 0
        self._reload_epoch = 0
        self._reload_cleanup_lock = asyncio.Lock()
        self._closing = False
        self._shutdown_started = False
        self._listening_on: Callable | None = None
        self.manual_starter = McpManualStart(self, monitor=monitor, book=launch_book)
        self._lost_relaunches: dict[str, int] = {}
        self._installed_at: dict[str, float] = {}
        self._launch_secrets: dict[str, set[str]] = {}
        # P6：每個 installation 恰有一個 SDK backing store；public/private 分區仍由
        # SDK hints＋Touka request principal context 共同決定。
        self._cache_stores: dict[str, PrincipalAwareResponseCacheStore] = {}
        self._subscription_supervisors: dict[
            McpHandleKey,
            McpSubscriptionSupervisor,
        ] = {}
        self._resource_subscriptions: dict[McpHandleKey, set[str]] = {}
        # life1：每條連線 owner task 的強參考（asyncio 只留弱參考）；cleanup 結尾等它們收完。
        self._connection_owners: set[McpConnectionOwner] = set()
        self._icon_service = icon_service or McpIconService()
        self._telemetry = McpTelemetry()
        self._oauth_auth_service = oauth_auth_service
        self._workload_oauth_service = workload_oauth_service
        self._refresh_installations()
        self._mrtr_driver = McpMrtrDriver(
            broker=mrtr_broker or InProcessMcpInteractionBroker(),
            authorize=mrtr_authorizer or self._authorize_mrtr_binding,
        )

    def set_oauth_auth_service(
        self,
        service: McpOAuthAuthService | None,
    ) -> None:
        """Bind or clear the lifespan-owned request-time OAuth service。"""
        self._oauth_auth_service = service

    def set_workload_oauth_service(
        self,
        service: McpWorkloadOAuthService | None,
    ) -> None:
        """Bind or clear the lifespan-owned P8-B runtime。"""
        self._workload_oauth_service = service

    async def close_workload_authority_handles(self, authority_key: str) -> int:
        """Close only the exact workload/delegation handle partition。"""
        if not authority_key:
            raise ValueError()
        keys = [
            key
            for state in self._installations.values()
            for key in self.handle_pool.keys_for_installation(state.installation_id)
            if key.principal_key == authority_key
        ]
        handles = self.handle_pool.pop_principal(authority_key)
        await self._close_handles(handles)
        for key in keys:
            self._handle_meta.pop(key, None)
            self._connect_locks.pop(key, None)
            self._circuit_breakers.pop(key, None)
        return len(handles)

    async def close_all_workload_handles(self) -> int:
        """Close every P8-B handle during a single-process global fence。"""
        keys = [
            key
            for state in self._installations.values()
            if state.config.auth_mode == "oauth_client_credentials"
            for key in self.handle_pool.keys_for_installation(state.installation_id)
        ]
        handles = [
            handle
            for state in self._installations.values()
            if state.config.auth_mode == "oauth_client_credentials"
            for handle in self.handle_pool.pop_installation(state.installation_id)
        ]
        await self._close_handles(handles)
        for key in keys:
            self._handle_meta.pop(key, None)
            self._connect_locks.pop(key, None)
            self._circuit_breakers.pop(key, None)
        return len(handles)

    def _cache_store_for(
        self,
        name: str,
        config: ProtocolServerConfig,
    ) -> PrincipalAwareResponseCacheStore:
        store = self._cache_stores.get(name)
        if store is None:
            store = PrincipalAwareResponseCacheStore(
                max_entries=config.cache_max_entries,
            )
            self._cache_stores[name] = store
        return store

    async def _authorize_oauth_operation(
        self,
        *,
        name: str,
        config: ProtocolServerConfig | None,
        request_context: McpRequestContext,
        operation: str,
        selector: str,
        arguments: dict[str, Any],
    ) -> McpInvocationAuthorization | None:
        if config is None or not config.is_remote or config.auth_profile_id is None:
            return None
        if config.auth_mode == "enterprise":
            _raise_protocol_error(f"MCP server '{name}' enterprise auth is fail-closed.")
        if config.auth_mode not in {"oauth_user", "oauth_client_credentials"}:
            return None
        installation_generation = self.get_installation_generation(name)
        if config.auth_mode == "oauth_user":
            if self._oauth_auth_service is None:
                _raise_protocol_error(f"MCP server '{name}' requires an OAuth runtime.")
            await self._oauth_auth_service.authorize_operation(
                server_name=name,
                request_context=request_context,
                installation_generation=installation_generation,
                operation=operation,
                selector=selector,
            )
            return None
        if (
            not HEARTBEAT_WORKLOAD_OAUTH_ENABLED
            or request_context.execution_kind != ExecutionKind.HEARTBEAT
            or request_context.origin != "heartbeat"
        ):
            _raise_protocol_error("MCP workload OAuth is heartbeat-only.")
        if self._workload_oauth_service is None:
            _raise_protocol_error(f"MCP server '{name}' requires a workload OAuth runtime.")
        authorization = await self._workload_oauth_service.authorize_operation(
            server_name=name,
            request_context=request_context,
            installation_generation=installation_generation,
            operation=operation,
            selector=selector,
            arguments=arguments,
        )
        if not isinstance(authorization, McpInvocationAuthorization):
            _raise_protocol_error("MCP workload OAuth requires an exact authorization ticket.")
        authorization.verify_wire_attempt(
            method=operation,
            tool_name=selector,
            arguments=arguments,
        )
        return authorization

    @contextmanager
    def _workload_authorization_scope(
        self,
        authorization: (McpInvocationAuthorization | McpTransportBootstrapAuthorization | None),
    ) -> Iterator[None]:
        """Bind one invocation ticket to this task and always clear it afterward。"""
        if authorization is None:
            yield
            return
        token = _CURRENT_WORKLOAD_AUTHORIZATION.set(authorization)
        try:
            yield
        finally:
            _CURRENT_WORKLOAD_AUTHORIZATION.reset(token)

    @asynccontextmanager
    async def _workload_bootstrap_scope(
        self,
        *,
        server_name: str,
        authorization: McpInvocationAuthorization | None,
    ) -> AsyncIterator[None]:
        """Replace the tool ticket with a distinct bootstrap authority temporarily。"""
        if authorization is None:
            yield
            return
        service = self._workload_oauth_service
        if service is None:
            _raise_protocol_error("MCP workload OAuth runtime is unavailable.")
        bootstrap = await service.authorize_transport_bootstrap(
            server_name=server_name,
            authorization=authorization,
        )
        if not isinstance(bootstrap, McpTransportBootstrapAuthorization):
            _raise_protocol_error("MCP workload bootstrap authority is unavailable.")
        with self._workload_authorization_scope(bootstrap):
            yield

    @staticmethod
    def _decode_workload_wire_payload(wire_request: Any) -> dict[str, Any] | None:
        """Decode one actual JSON-RPC body and reject batches or duplicate keys。"""
        http_method = str(getattr(wire_request, "method", "")).upper()
        if http_method == "GET":
            return None
        if http_method != "POST":
            _raise_protocol_error("MCP workload OAuth rejects this HTTP method.")
        content = bytes(getattr(wire_request, "content", b""))

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    _raise_protocol_error("MCP workload OAuth rejects duplicate JSON fields.")
                result[key] = value
            return result

        try:
            payload = json.loads(content, object_pairs_hook=reject_duplicates)
        except (TypeError, ValueError, UnicodeError):
            _raise_protocol_error("MCP workload OAuth requires canonical JSON-RPC.")
        if not isinstance(payload, dict):
            _raise_protocol_error("MCP workload OAuth rejects JSON-RPC batches.")
        return payload

    @classmethod
    def _workload_wire_attempt(
        cls,
        authority: McpInvocationAuthorization | McpTransportBootstrapAuthorization,
        wire_request: Any,
    ) -> tuple[
        McpInvocationAuthorization,
        McpTransportBootstrapAuthorization | None,
        McpWorkloadWireAttempt,
    ]:
        payload = cls._decode_workload_wire_payload(wire_request)
        if payload is None:
            method = "transport/get"
            payload_digest = digest_wire_payload(
                {"http_method": "GET", "transport": "streamable-http"}
            )
        else:
            method = payload.get("method")
            if not isinstance(method, str) or not method:
                _raise_protocol_error("MCP workload OAuth requires one JSON-RPC method.")
            payload_digest = digest_wire_payload(payload)
        if isinstance(authority, McpInvocationAuthorization):
            if payload is None:
                _raise_protocol_error("MCP tool authority cannot authenticate transport GET.")
            params = payload.get("params")
            if not isinstance(params, dict):
                _raise_protocol_error("MCP tools/call requires object params.")
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(tool_name, str) or not isinstance(arguments, dict):
                _raise_protocol_error("MCP tools/call wire params are invalid.")
            authority.verify_wire_attempt(
                method=method,
                tool_name=tool_name,
                arguments=arguments,
            )
            return (
                authority,
                None,
                McpWorkloadWireAttempt(
                    authorization_id=authority.authorization_id,
                    egress_kind="mcp_bearer",
                    method=method,
                    payload_digest=payload_digest,
                    target_binding_digest=authority.target_binding_digest,
                    raw_tool_name=tool_name,
                    arguments_digest=digest_wire_payload(arguments),
                ),
            )
        if method not in authority.allowed_methods:
            _raise_protocol_error("MCP bootstrap authority cannot authenticate this method.")
        return (
            authority.parent_authorization,
            authority,
            McpWorkloadWireAttempt(
                authorization_id=authority.authorization_id,
                egress_kind="transport_bootstrap",
                method=method,
                payload_digest=payload_digest,
                target_binding_digest=(authority.parent_authorization.target_binding_digest),
            ),
        )

    @asynccontextmanager
    async def _workload_bearer_credential_guard(
        self,
        *,
        server_name: str,
        wire_request: Any,
    ) -> AsyncIterator[Any]:
        """Resolve the task-local ticket on every transport HTTP send。"""
        wire_authority = _CURRENT_WORKLOAD_AUTHORIZATION.get()
        if wire_authority is None:
            _raise_protocol_error("MCP bearer send requires an exact workload authorization.")
        authorization, bootstrap, wire_attempt = self._workload_wire_attempt(
            wire_authority,
            wire_request,
        )
        state = self._installations.get(server_name)
        if (
            state is None
            or authorization.installation_id != server_name
            or authorization.installation_generation != state.generation
            or state.config.auth_profile_id != authorization.auth_profile_id
        ):
            _raise_protocol_error("MCP bearer send requires an exact workload authorization.")
        try:
            configured_target = canonical_mcp_transport_url(state.config.url or "")
            actual_target = canonical_mcp_transport_url(str(wire_request.url))
        except McpOAuthTargetBindingError:
            _raise_protocol_error("MCP workload transport target mismatch.")
        if configured_target != actual_target:
            _raise_protocol_error("MCP workload transport target mismatch.")
        if self._workload_oauth_service is None:
            _raise_protocol_error("MCP workload OAuth runtime is unavailable.")
        guard = getattr(
            self._workload_oauth_service,
            "workload_bearer_credential_guard",
            None,
        )
        if not callable(guard):
            _raise_protocol_error("MCP workload OAuth bearer runtime is unavailable.")
        async with guard(
            server_name=server_name,
            authorization=authorization,
            wire_attempt=wire_attempt,
            transport_url=actual_target,
            bootstrap_authorization=bootstrap,
        ) as credential:
            yield credential

    def _cache_config_for(
        self,
        name: str,
        config: ProtocolServerConfig,
        key: McpHandleKey,
    ) -> CacheConfig | None:
        if not config.cache_enabled:
            return None
        return CacheConfig(
            store=self._cache_store_for(name, config),
            # Dynamic principal isolation lives in the store context. This fixed
            # non-empty SDK partition prevents accidental use with another store.
            partition="touka-request-principal",
            target_id=(f"installation:{name}:generation:{key.config_generation}"),
            default_ttl_ms=config.cache_default_ttl_ms,
            share_public=True,
        )

    @contextmanager
    def _cache_scope(
        self,
        name: str,
        request_context: McpRequestContext | None,
    ) -> Iterator[None]:
        store = self._cache_stores.get(name)
        if store is None:
            yield
            return
        principal = request_context.principal_key if request_context is not None else "touka-system"
        with store.bind_principal(principal):
            yield

    def _start_subscription(
        self,
        key: McpHandleKey,
        session: Any,
        config: ProtocolServerConfig,
    ) -> None:
        """Start a modern change listener when the peer declared useful filters."""
        if (
            not config.subscriptions_enabled
            or _protocol_version(session) != "2026-07-28"
            or not callable(getattr(session, "listen", None))
        ):
            return
        capabilities = getattr(session, "server_capabilities", None)
        tools = getattr(capabilities, "tools", None)
        prompts = getattr(capabilities, "prompts", None)
        resources = getattr(capabilities, "resources", None)
        filters = {
            "tools_list_changed": bool(getattr(tools, "list_changed", False)),
            "prompts_list_changed": bool(getattr(prompts, "list_changed", False)),
            "resources_list_changed": bool(getattr(resources, "list_changed", False)),
        }
        if not any(filters.values()) and not bool(getattr(resources, "subscribe", False)):
            return
        supervisor = McpSubscriptionSupervisor(
            session,
            **filters,
            max_backoff_seconds=config.subscription_max_backoff_seconds,
            on_event=lambda kind: logger.info(
                "MCP change event installation=%s kind=%s",
                key.installation_id,
                kind,
            ),
        )
        self._subscription_supervisors[key] = supervisor
        # life1：supervisor 用清掉 workload 一次性票的 context 建立，不長期持有票。
        context = copy_context()
        context.run(_CURRENT_WORKLOAD_AUTHORIZATION.set, None)
        context.run(supervisor.start)

    async def _stop_subscriptions_for_installation(self, name: str) -> None:
        keys = [key for key in self._subscription_supervisors if key.installation_id == name]
        for key in keys:
            supervisor = self._subscription_supervisors.pop(key, None)
            if supervisor is None:
                continue  # life1：等前一個 stop 時，失聯處理已先停掉這個
            await supervisor.stop()
            self._resource_subscriptions.pop(key, None)

    async def _clear_installation_cache(self, name: str) -> None:
        store = self._cache_stores.pop(name, None)
        if store is not None:
            await store.clear()

    def _mark_fresh(
        self,
        name: str,
        request_context: McpRequestContext | None,
        feature: str,
    ) -> None:
        try:
            key = self.handle_key_for(name, request_context)
        except ProtocolError:
            return
        supervisor = self._subscription_supervisors.get(key)
        if supervisor is not None:
            supervisor.mark_fresh(feature)

    async def _watch_resource(
        self,
        name: str,
        request_context: McpRequestContext,
        uri: str,
    ) -> None:
        key = self.handle_key_for(name, request_context)
        supervisor = self._subscription_supervisors.get(key)
        if supervisor is None:
            return
        watched = self._resource_subscriptions.setdefault(key, set())
        if uri in watched:
            return
        watched.add(uri)
        await supervisor.replace_resource_subscriptions(sorted(watched))

    async def _safe_descriptor_icons(
        self,
        name: str,
        item_name: str,
        descriptor: Any,
        issues: list[McpCatalogIssue],
    ) -> Any:
        config = self.registry.get_server(name)
        if config is None or not config.icons_enabled:
            return descriptor.model_copy(update={"icons": []})
        icons, icon_issues = await self._icon_service.sanitize_icons(
            descriptor.icons,
            server_url=config.url,
        )
        issues.extend(
            McpCatalogIssue(
                item_name=item_name,
                code=f"McpIcon:{issue.code}",
                message=issue.message,
            )
            for issue in icon_issues
        )
        return descriptor.model_copy(update={"icons": icons})

    def _refresh_installations(self) -> None:
        """從 registry 重建 installation state；重載時 generation 單調遞增。"""
        previous = self._installations
        refreshed: dict[str, InstallationState] = {}
        for name, config in self.registry.get_all_servers().items():
            old_state = previous.get(name)
            refreshed[name] = InstallationState(
                installation_id=name,
                config=config,
                generation=old_state.generation + 1 if old_state else 1,
                enabled=config.enabled,
            )
        self._installations = refreshed

    def handle_key_for(
        self,
        name: str,
        request_context: McpRequestContext | None,
    ) -> McpHandleKey:
        """依 installation 設定決定 shared 或 principal-scoped handle key。"""
        state = self._installations.get(name)
        if state is None:
            config = self.registry.get_server(name)
            if config is None:
                raise ServerNotFoundError(name)
            state = InstallationState(name, config)
            self._installations[name] = state
        if request_context is not None and request_context.installation_id != name:
            msg = (
                f"MCP request context installation '{request_context.installation_id}' "
                f"does not match target '{name}'."
            )
            raise ProtocolError(msg)
        if state.config.is_principal_scoped and request_context is None:
            msg = f"MCP server '{name}' requires a principal-bound request context."
            raise ProtocolError(msg)
        principal_key = None
        if state.config.is_principal_scoped and request_context is not None:
            if state.config.auth_mode == "oauth_user":
                principal_key = request_context.oauth_principal_key
            elif state.config.auth_mode == "oauth_client_credentials":
                wire_authority = _CURRENT_WORKLOAD_AUTHORIZATION.get()
                if wire_authority is None:
                    _raise_protocol_error("MCP handle requires an exact workload authorization.")
                authorization = (
                    wire_authority.parent_authorization
                    if isinstance(
                        wire_authority,
                        McpTransportBootstrapAuthorization,
                    )
                    else wire_authority
                )
                if (
                    authorization.installation_id != name
                    or authorization.installation_generation != state.generation
                    or authorization.execution_agent_id != request_context.identity.agent_id
                    or authorization.origin != request_context.origin
                ):
                    _raise_protocol_error("MCP workload handle authorization is stale.")
                principal_key = authorization.handle_principal_key
            else:
                principal_key = request_context.principal_key
        return McpHandleKey(
            installation_id=name,
            principal_key=principal_key,
            protocol_mode=state.config.protocol_mode,
            config_generation=state.generation,
        )

    def get_installation_generation(self, name: str) -> int:
        """Return the current in-process generation for an exact installation."""
        state = self._installations.get(name)
        if state is None:
            config = self.registry.get_server(name)
            if config is None:
                raise ServerNotFoundError(name)
            state = InstallationState(name, config)
            self._installations[name] = state
        return state.generation

    def _mrtr_binding(
        self,
        name: str,
        *,
        request_context: McpRequestContext,
        method: str,
        operation_name: str,
        arguments: Any,
    ) -> McpMrtrBinding:
        """擷取 operation 起點的 generation 與不可變安全 context。"""
        state = self._installations.get(name)
        if state is None:
            raise ServerNotFoundError(name)
        return McpMrtrBinding(
            request_context=request_context,
            installation_generation=state.generation,
            operation_method=method,
            operation_name=operation_name,
            arguments_digest=digest_mcp_arguments(arguments),
        )

    async def _authorize_mrtr_binding(self, binding: McpMrtrBinding) -> None:
        """每輪 retry 前重驗人類、installation、grant、工具與 PaC。"""
        context = binding.request_context
        state = self._installations.get(context.installation_id)
        if (
            state is None
            or not state.enabled
            or not state.config.enabled
            or state.generation != binding.installation_generation
        ):
            raise McpMrtrAuthorizationError()

        human_id = context.human_principal_id
        if human_id is None:
            raise McpMrtrAuthorizationError()
        human = AuthService().get_user_by_id(human_id)
        if human is None or not human.is_active:
            raise McpMrtrAuthorizationError()
        if (
            human.role != "admin"
            and context.identity.agent_id != human.id
            and context.identity.agent_id not in human.owned_agents
        ):
            raise McpMrtrAuthorizationError()

        if binding.operation_method == "tools/call":
            if not state.config.allows_tool(binding.operation_name):
                raise McpMrtrAuthorizationError()
            allowed = permission_service.is_mcp_tool_allowed(
                context.installation_id,
                binding.operation_name,
                context.identity,
            )
        else:
            allowed = permission_service.is_mcp_server_allowed(
                context.installation_id,
                context.identity,
            )
        if not allowed:
            raise McpMrtrAuthorizationError()

        current_auth_level = context.auth_level
        if context.session_id:
            session = await session_service.get_session_by_id(context.session_id)
            if session is None:
                raise McpMrtrAuthorizationError()
            owner_id = session.get("owner_user_id")
            if owner_id and owner_id != human_id and human.role != "admin":
                raise McpMrtrAuthorizationError()
            current_auth_level = str(session.get("auth_level") or context.auth_level)
        effective_auth_level = _stricter_auth_level(
            context.auth_level,
            current_auth_level,
        )
        if context.policy_context is not None:
            environment = dict(context.policy_context.environment)
            environment["auth_level"] = effective_auth_level
            decision = permission_service.evaluate_policy(
                context.policy_context.model_copy(
                    update={"environment": environment},
                )
            )
            if decision.effect == "DENY":
                raise McpMrtrAuthorizationError()

    async def _drive_mrtr(
        self,
        first: Any,
        *,
        retry: Callable[
            [types.InputResponses | None, str | None],
            Awaitable[Any],
        ],
        binding: McpMrtrBinding,
    ) -> Any:
        """委派 P7 driver；非 input_required 結果零額外行為。"""
        return await self._mrtr_driver.drive(
            first,
            retry=retry,
            binding=binding,
        )

    def _breaker_for(self, key: McpHandleKey) -> CircuitBreaker:
        """斷路器以 handle key 隔離；shared v1 仍讀既有 server-name 注入點。"""
        if key.is_shared and key.installation_id in self._circuit_breakers:
            return self._circuit_breakers[key.installation_id]
        breaker = self._circuit_breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)
            self._circuit_breakers[key] = breaker
        return breaker

    async def initialize_all(self) -> None:
        """啟動 shared installations；principal-scoped installations 保持 lazy。"""
        if self._shutdown_started:
            return
        # 第一個 await 前作廢舊同意；快照不可在等待之後借用別次重載的版本。
        self._reload_epoch += 1
        epoch = self._reload_epoch
        self._refresh_installations()
        servers = dict(self.registry.get_all_servers())
        installations = dict(self._installations)
        async with self._reload_cleanup_lock:
            if epoch != self._reload_epoch:
                return
            await self.cleanup()
        if epoch != self._reload_epoch or self._shutdown_started:
            return
        self._book.retain(servers)
        self._lost_relaunches.clear()
        if self._listening_on != self._monitor.on_level_change:
            self._monitor.on_level_change(self._on_isolation_change)
            self._listening_on = self._monitor.on_level_change
        if any(not c.is_remote and not c.requires_deferred_auth for c in servers.values()):
            await self._monitor.ensure_ready()
        for name, config in servers.items():
            if epoch != self._reload_epoch or self._shutdown_started:
                return
            try:
                self._check_stdio_scope(name, config)
            except ProtocolError:
                continue
            # 所有非 none/static auth mode 都不得在 lifespan 階段建立未授權 transport。
            # oauth_user 等 request context；P8-B modes 則維持 fail-closed、不偷跑舊 client。
            if config.requires_deferred_auth:
                continue
            self._circuit_breakers[name] = CircuitBreaker(
                failure_threshold=3,
                recovery_timeout=30.0,
            )
            async with self._installation_lock(name):
                if epoch != self._reload_epoch:
                    return
                if self._installations.get(name) is not installations[name]:
                    continue
                try:
                    old = self.handle_pool.pop_installation(name)
                    await self._stop_subscriptions_for_installation(name)
                    await self._clear_installation_cache(name)
                    await self._close_handles(old)
                    if epoch != self._reload_epoch:
                        return
                    decision = await self._judge(name, config)
                    if epoch != self._reload_epoch:
                        return
                    await self.connect_server(name, config, **decision)
                except McpLaunchWaiting:
                    continue
                except Exception as e:
                    logger.warning(
                        "連線至 Server '%s' 失敗：%s", name, self._sanitize_for(name, str(e))
                    )
                    self._record_error(name, str(e))
        clients = await discover_ide_servers()
        if epoch == self._reload_epoch:
            self._ide_clients = clients

    async def connect_server(
        self,
        name: str,
        config: ProtocolServerConfig,
        request_context: McpRequestContext | None = None,
        *,
        launch: McpLaunch | None = None,
        judged_direct: object | None = None,
        retry: bool = True,
    ) -> None:
        """建立一個 shared 或 principal-scoped SDK v2 Client handle。"""
        if self._closing or self._shutdown_started:
            return
        self._check_stdio_scope(name, config)
        if (
            not config.is_remote
            and not isinstance(launch, McpLaunch)
            and judged_direct is not _JUDGED_DIRECT
        ):
            _raise_protocol_error("MCP stdio requires a launch decision before connecting.")
        key = self.handle_key_for(name, request_context)
        connect = self._connect_server_with_retry if retry else self._connect_server_once
        await connect(name, config, request_context, launch=launch, key=key,
                      epoch=self._lifecycle_epoch)

    @with_retry(max_retries=2, base_delay=1.0)
    async def _connect_server_with_retry(
        self, name: str, config: ProtocolServerConfig,
        request_context: McpRequestContext | None, *, launch: McpLaunch | None,
        key: McpHandleKey, epoch: int,
    ) -> None:
        await self._connect_server_once(name, config, request_context, launch=launch,
                                        key=key, epoch=epoch)

    async def _connect_server_once(
        self,
        name: str,
        config: ProtocolServerConfig,
        request_context: McpRequestContext | None,
        *,
        launch: McpLaunch | None,
        key: McpHandleKey,
        epoch: int,
    ) -> None:
        # 重試退避期間也可能開始關機，不能再建立 owner。
        state = self._installations.get(name)
        if (self._closing or self._shutdown_started or epoch != self._lifecycle_epoch
                or state is None or state.generation != key.config_generation
                or state.config != config or self.registry.get_server(name) != config):
            return
        if launch is not None:
            self._launch_secrets.setdefault(name, set()).update(secret_values(launch.exec_env))
        oauth_credential_provider = None
        if config.auth_mode == "oauth_user":
            if request_context is None or self._oauth_auth_service is None:
                _raise_protocol_error(f"MCP server '{name}' requires an interactive OAuth runtime.")
            oauth_credential_provider = partial(
                self._oauth_auth_service.bearer_credential_guard,
                server_name=name,
                request_context=request_context,
                installation_generation=key.config_generation,
            )
        elif config.auth_mode == "oauth_client_credentials":
            if request_context is None or self._workload_oauth_service is None:
                _raise_protocol_error(f"MCP server '{name}' requires a workload OAuth runtime.")
            oauth_credential_provider = partial(
                self._workload_bearer_credential_guard,
                server_name=name,
            )
        elif config.auth_mode not in {"none", "static_headers"}:
            _raise_protocol_error(f"MCP auth mode '{config.auth_mode}' is not implemented.")
        logger.info(f"正在嘗試連線至 Server: {name}")
        with self._telemetry.span(
            "server/connect",
            enabled=config.telemetry_enabled,
            attributes={"mcp.transport": "http" if config.is_remote else "stdio"},
        ):
            # life1：連線由專屬 owner task 進入與退出，出事或被關都不取消呼叫端 task。
            owner = McpConnectionOwner(
                name,
                partial(
                    connect_mcp_server,
                    config,
                    cache_config=self._cache_config_for(name, config, key),
                    oauth_credential_provider=oauth_credential_provider,
                    **(
                        {
                            "isolated": McpIsolatedLaunch(
                                launch,
                                label=name,
                                report_failure=self._monitor.report_failure,
                                on_eof="raise",
                                sanitize=partial(self._sanitize_for, name),
                            )
                        }
                        if launch is not None and launch.acquired is not None
                        else {}
                    ),
                ),
                on_lost=partial(self._on_connection_lost, key),
                registry=self._connection_owners,
                clear_on_ready=(_CURRENT_WORKLOAD_AUTHORIZATION,),
            )
            safe_error = None
            try:
                session = await owner.start()
            except McpConnectionAborted:
                return  # cleanup 中止的握手：不安裝，也不讓 with_retry 重試
            except Exception as exc:
                if launch is None or launch.acquired is None:
                    raise
                cause = root_cause(exc)
                message = f"{type(cause).__name__}: {cause}"
                safe_error = ConnectionError(self._sanitize_for(name, message))
            # 在 except 外拋出，避免 with_retry 或 traceback 保留原始憑證。
            if safe_error is not None:
                raise safe_error
        if not await self._install_handle(key, LegacyMcpHandle(session=session, stack=owner)):
            return
        if not owner.alive:
            return  # 裝好前就斷了或被要求關閉：保留失聯紀錄、不啟動訂閱
        self._start_subscription(key, session, config)
        if key.is_shared:
            state = self._installations[name]
            state.last_error = None
            state.last_error_at = None
            self._server_meta.setdefault(name, {}).update(last_error=None, last_error_at=None)
        else:
            self._handle_meta.setdefault(key, {}).update(
                last_error=None,
                last_error_at=None,
            )
        logger.info(f"成功連結至 Server: {name}")

    async def _install_handle(self, key: McpHandleKey, handle: LegacyMcpHandle) -> bool:
        """唯一放置點：先換上新 handle，再停舊訂閱、關被換掉的舊 handle（不留孤兒行程）。"""
        state = self._installations.get(key.installation_id)
        if (
            self._closing
            or self._shutdown_started
            or state is None
            or state.generation != key.config_generation
        ):
            with suppress(Exception):
                await handle.close()
            return False
        previous = self.handle_pool.swap(key, handle)
        self._installed_at[key.installation_id] = get_monotonic_ms() / 1000
        if previous is None or previous.stack is handle.stack:
            return True
        supervisor = self._subscription_supervisors.pop(key, None)
        if supervisor is not None:
            await supervisor.stop()
        # owner 自己記關閉錯誤；呼叫端的取消照常往外拋，舊 owner 在背景關完。
        with suppress(Exception):
            await previous.close()
        return True

    async def _on_connection_lost(
        self,
        key: McpHandleKey,
        owner: McpConnectionOwner,
        error: BaseException,
    ) -> None:
        """連線自己斷了（owner 回報）：只記錯誤、停訂閱，不拿鎖、不重連（handle 留著快速失敗）。"""
        current = self.handle_pool.get(key)
        if current is None or current.stack is not owner:
            return
        name = key.installation_id
        self._record_error(name, f"MCP connection lost: {type(error).__name__}: {error}", key=key)
        self._resource_subscriptions.pop(key, None)
        supervisor = self._subscription_supervisors.pop(key, None)
        if supervisor is not None:
            await supervisor.stop()
        logger.warning("MCP 連線中斷 server=%s：%s: %s", name,
                       type(error).__name__, self._sanitize_for(name, str(error)))
        self._connection_lost_hook(key, error, owner)

    def _connection_lost_hook(
        self,
        key: McpHandleKey,
        error: BaseException,
        owner: McpConnectionOwner,
    ) -> None:
        """只替隔離的 shared stdio 排重連，不繼承建立者的請求身分。"""
        del error  # 錯誤已由失聯入口記錄；重連政策只依啟動層級與次數。
        name = key.installation_id
        info = self._book.get(name)
        config = self.registry.get_server(name)
        if (
            self._closing
            or self._shutdown_started
            or not key.is_shared
            or config is None
            or config.is_remote
            or info is None
            or info.level not in {"landlock", "executor"}
        ):
            return
        n = self._lost_relaunches.get(name, 0)
        now = get_monotonic_ms() / 1000
        if now - self._installed_at.get(name, now) >= MCP_LOST_RELAUNCH_RESET_SECONDS:
            n = 0
        if n >= MCP_LOST_RELAUNCH_LIMIT:
            logger.warning("MCP %s automatic relaunch stopped; restart it manually", name)
            return
        self._lost_relaunches[name] = n + 1
        self._schedule_relaunch(
            name, delay=MCP_LOST_RELAUNCH_DELAY_SECONDS * 2**n, expect_owner=owner
        )

    def _check_stdio_scope(self, name: str, config: ProtocolServerConfig) -> None:
        if not config.is_remote and config.is_principal_scoped:
            code = ErrorCode.MCP_STDIO_PRINCIPAL_UNSUPPORTED.value
            self._record_error(name, code)
            _raise_protocol_error(code)

    async def _judge(
        self, name: str, config: ProtocolServerConfig, *, consent: Consent | None = None
    ) -> dict[str, Any]:
        self._check_stdio_scope(name, config)
        if config.is_remote:
            return {}
        launch = await self._launcher.plan_server_launch(
            name,
            config,
            self._isolation_reader(),
            tool_base_url=tool_base_url(),
            consent=consent,
        )
        decision: dict[str, Any] = {"launch": launch} if launch.acquired is not None else {
            "judged_direct": _JUDGED_DIRECT,
        }
        if consent is not None:
            decision["retry"] = False
        return decision

    @asynccontextmanager
    async def _installation_lock(
        self, name: str, *, timeout: float | None = None
    ) -> AsyncIterator[None]:
        lock = self._installation_locks.setdefault(name, asyncio.Lock())
        try:
            if timeout is None:
                await lock.acquire()
            else:
                await asyncio.wait_for(lock.acquire(), timeout)
        except TimeoutError:
            raise McpServerBusyError(MCP_SERVER_BUSY) from None
        try:
            yield
        finally:
            lock.release()

    def _schedule_relaunch(
        self, name: str, *, delay: float = 0.0, expect_owner: McpConnectionOwner | None = None
    ) -> None:
        if self._closing or self._shutdown_started:
            return
        due = get_monotonic_ms() / 1000 + delay
        pending = self._relaunch_pending.get(name)
        if pending is not None:
            if pending.dirty:
                pending.owner = (
                    None if expect_owner is None or pending.owner is None else expect_owner
                )
                pending.due = min(pending.due, due)
            else:
                pending.owner, pending.due = expect_owner, due
            pending.epoch, pending.dirty = self._lifecycle_epoch, True
            pending.wake.set()
            return
        intent = _RelaunchIntent(due, self._lifecycle_epoch, expect_owner)
        self._relaunch_pending[name] = intent
        task = Context().run(spawn_logged, self._relaunch_task(name, intent), "mcp-relaunch")
        self._relaunch_tasks.add(task)
        task.add_done_callback(self._relaunch_tasks.discard)

    async def _relaunch_task(self, name: str, intent: _RelaunchIntent) -> None:
        try:
            while intent.dirty and not (self._closing or self._shutdown_started):
                intent.wake.clear()
                delay = intent.due - get_monotonic_ms() / 1000
                if delay > 0:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(intent.wake.wait(), delay)
                    if intent.wake.is_set():
                        continue
                async with self._relaunch_slots:
                    intent.dirty = False
                    await self._reconnect(
                        name,
                        consent=None,
                        budget=None,
                        count_restart=False,
                        lock_timeout=None,
                        epoch=intent.epoch,
                        expect_owner=intent.owner,
                    )
        finally:
            self._relaunch_pending.pop(name, None)

    async def _on_isolation_change(self, snapshot: IsolationSnapshot) -> None:
        if not (self._closing or self._shutdown_started):
            for name in self._book.relaunch_targets(snapshot, self._monitor.local_decision()):
                self._schedule_relaunch(name)

    def begin_shutdown(self) -> None:
        """在取消背景工作之前，先同步封住所有新連線與重連。"""
        self._shutdown_started = True
        self._closing = True

    def reopen_for_test(self) -> None:
        """只供測試在完成 shutdown 後重用單例；正式生命週期不可重新開啟。"""
        self._shutdown_started = False
        self._closing = False

    async def shutdown(self) -> None:
        """停止重連工作，再關閉所有 owner。"""
        self.begin_shutdown()
        tasks = list(self._relaunch_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._relaunch_pending.clear()
        await self.cleanup()

    async def ensure_connected(
        self,
        name: str,
        request_context: McpRequestContext | None = None,
    ) -> bool:
        """確保伺服器已連線，若未連線則嘗試連線。已連線則跳過。"""
        config = self.registry.get_server(name)
        if not config:
            logger.warning(f"無法確保連線：伺服器 '{name}' 未在註冊表中。")
            return False

        key = self.handle_key_for(name, request_context)
        if self.handle_pool.get(key) is not None:
            return True

        try:
            await self._ensure_handle(name, request_context)
        except McpOperationException:
            return False
        return True

    def resolve_tool(self, tool_name: str) -> tuple[str | None, str | None]:
        """SSOT 工具名稱解析入口 (SOLID: SRP)。

        將任意格式的工具名稱解析為 (server_id, actual_tool_name)。

        格式支援：
        1. 'server__tool'  -> ('server', 'tool')
        2. 'tool'          -> ('server_id', 'tool')  // 查詢 registry
        3. 未知工具        -> (None, None)
        """
        # 格式 1：雙底線分隔
        if "__" in tool_name:
            server, actual = tool_name.split("__", 1)
            return (server, actual)

        # 格式 2：bare name -> 查 registry 找對應 Server
        server_id = self.registry.find_server_by_tool_name(tool_name)
        if server_id:
            return (server_id, tool_name)

        # 未知工具
        return (None, None)

    async def add_server(
        self,
        name: str,
        config: ProtocolServerConfig,
        request_context: McpRequestContext | None = None,
    ) -> bool:
        """動態新增並連線至單個 MCP Server (熱插拔支援)"""
        async with self._installation_lock(name):
            return await self._add_server_locked(name, config, request_context)

    async def _add_server_locked(
        self, name: str, config: ProtocolServerConfig, request_context: McpRequestContext | None
    ) -> bool:
        if self._closing or self._shutdown_started:
            return False
        existing = self._installations.get(name)
        generation = (existing.generation if existing else 0) + 1
        # 等待前同步兩份視圖；重載可在等待期間換代，舊掛載不得再覆寫它。
        self.registry.servers[name] = config
        state = InstallationState(
            installation_id=name,
            config=config,
            generation=generation,
            enabled=config.enabled,
        )
        self._installations[name] = state
        old_handles = self.handle_pool.pop_installation(name)
        if old_handles:
            logger.info(f"Server '{name}' 已存在，嘗試重新連線。")
            await self._stop_subscriptions_for_installation(name)
            await self._clear_installation_cache(name)
            await self._close_handles(old_handles)
        if self._installations.get(name) is not state:
            return False
        if config.is_remote and config.is_principal_scoped and request_context is None:
            return False
        key = None
        try:
            decision = await self._judge(name, config)
            if self._installations.get(name) is not state:
                return False
            key = self.handle_key_for(name, request_context)
            self._circuit_breakers[key] = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)
            await self.connect_server(
                name, config, request_context, **decision
            )
        except McpLaunchWaiting:
            return False
        except Exception as e:
            logger.warning(
                "熱插拔連線至 Server '%s' 失敗：%s", name, self._sanitize_for(name, str(e))
            )
            self._record_error(name, str(e), key=key)
            return False
        else:
            handle = self.handle_pool.get(key)
            return (
                self._installations.get(name) is state
                and handle is not None
                and (not isinstance(handle.stack, McpConnectionOwner) or handle.stack.alive)
            )

    def _sanitize_for(self, name: str, msg: str) -> str:
        """脫敏（供回傳前端）：先遮罩該伺服器 env 的實際值（公開工具網址除外），

        再套用通用路徑/token backstop。env 值才是真正會被傳進子進程、最可能洩漏的機密。
        """
        cfg = self.registry.get_server(name)
        values = self._launch_secrets.get(name, set()) | set(
            secret_values(cfg.env or {}) if cfg else ()
        )
        return sanitize_diagnostic(redact_values(msg or "", values))

    def error_message(self, name: str, error: BaseException) -> str:
        """API 邊界使用同一份實際啟動環境值遮罩。"""
        return self._sanitize_for(name, str(error))

    def _sanitize_metadata(self, name: str, value: Any) -> Any:
        """顯示用的伺服器資訊可能回顯環境值；保留 JSON 結構。"""
        if isinstance(value, str):
            return self._sanitize_for(name, value)
        if isinstance(value, dict):
            return {
                k: self._sanitize_metadata(name, v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._sanitize_metadata(name, item) for item in value]
        return value

    def _record_error(
        self,
        name: str,
        msg: str,
        *,
        key: McpHandleKey | None = None,
    ) -> None:
        """記錄脫敏錯誤；principal failure 不得污染 installation 全域 health。"""
        safe = self._sanitize_for(name, msg)
        now = get_current_epoch()
        if key is not None and not key.is_shared:
            self._handle_meta.setdefault(key, {}).update(
                last_error=safe,
                last_error_at=now,
            )
            return
        state = self._installations.get(name)
        if state is not None:
            state.last_error = safe
            state.last_error_at = now
        self._server_meta.setdefault(name, {}).update(
            last_error=safe,
            last_error_at=now,
        )

    async def restart_server(self, name: str) -> bool:
        """HTTP 重啟使用獨立的鎖等待與握手預算。"""
        self._lost_relaunches.pop(name, None)
        return await self._reconnect(
            name,
            consent=None,
            budget=MCP_HTTP_RESTART_SECONDS,
            count_restart=True,
            lock_timeout=MCP_HTTP_RESTART_LOCK_SECONDS,
        )

    def server_entry(self, name: str) -> ProtocolServerConfig | None:
        """手動啟動使用當前條目，和連線共用 registry。"""
        return self.registry.get_server(name)

    async def relaunch_with_consent(self, name: str, consent: Consent) -> bool:
        """同意只交給這次判定，使用 HTTP 重啟的時間預算。"""
        self._lost_relaunches.pop(name, None)
        return await self._reconnect(
            name, consent=consent, budget=MCP_HTTP_RESTART_SECONDS, count_restart=True,
            lock_timeout=MCP_HTTP_RESTART_LOCK_SECONDS,
        )

    def schedule_relaunch(self, name: str) -> None:
        """設定存檔後排背景重連；排程器建立空的 request context。"""
        if self.registry.get_server(name) is not None:
            self._schedule_relaunch(name)

    async def _reconnect(
        self,
        name: str,
        *,
        consent: Consent | None,
        budget: float | None,
        count_restart: bool,
        lock_timeout: float | None,
        epoch: int | None = None,
        expect_owner: McpConnectionOwner | None = None,
    ) -> bool:
        """重啟單一 MCP 伺服器（關閉舊連線並重生）；重置斷路器、清最後錯誤、累計重啟次數。

        以逾時界限避免卡住的子進程拖死 HTTP worker；伺服器不存在於註冊表即 raise。
        """
        async with self._installation_lock(name, timeout=lock_timeout):
            if (
                self._closing
                or self._shutdown_started
                or (epoch is not None and epoch != self._lifecycle_epoch)
            ):
                return False
            config = self.registry.get_server(name)
            if not config:
                if epoch is not None:
                    return False
                raise ServerNotFoundError(name)
            if consent is not None and (
                refused := self.manual_starter.consent_refusal(name, consent)
            ):
                logger.warning("AUDIT: manual start of MCP %s refused (%s)", name, refused)
                return False
            if expect_owner is not None:
                current = self.handle_pool.get(self.handle_key_for(name, None))
                if current is None or current.stack is not expect_owner:
                    return False
            launch_epoch = self._lifecycle_epoch
            prior_state = self._installations.get(name)
            generation = prior_state.generation if prior_state else None
            try:
                decision = await self._judge(name, config, consent=consent)
            except McpLaunchWaiting:
                old = self.handle_pool.pop_installation(name)
                if old:
                    state = self._installations.get(name)
                    if state is not None:
                        state.generation += 1
                    await self._stop_subscriptions_for_installation(name)
                    await self._clear_installation_cache(name)
                    await self._close_handles(old)
                return False
            except Exception as exc:
                self._record_error(name, str(exc))
                return False
            if (launch_epoch != self._lifecycle_epoch
                    or self._installations.get(name) is not prior_state
                    or (prior_state is not None and generation != prior_state.generation)
                    or self.registry.get_server(name) != config):
                return False
            state = self._installations.get(name) or InstallationState(name, config)
            state.config = config
            state.generation += 1
            if count_restart:
                state.restart_count += 1
            state.last_error = None
            self._installations[name] = state
            meta = self._server_meta.setdefault(name, {})
            meta["restart_count"] = state.restart_count
            meta["last_error"] = None
            await self._stop_subscriptions_for_installation(name)
            await self._clear_installation_cache(name)
            await self._close_handles(self.handle_pool.pop_installation(name))
            self._handle_meta = {
                key: value
                for key, value in self._handle_meta.items()
                if key.installation_id != name
            }
            self._circuit_breakers = {
                key: value
                for key, value in self._circuit_breakers.items()
                if not (
                    key == name or isinstance(key, McpHandleKey) and key.installation_id == name
                )
            }
            if config.is_principal_scoped:
                return True
            # shared handle 重啟後立即重連；principal handle 則等下一個 request。
            self._circuit_breakers[name] = CircuitBreaker(
                failure_threshold=3,
                recovery_timeout=30.0,
            )
            try:
                connection = self.connect_server(name, config, **decision)
                if budget is None:
                    await connection
                else:
                    await asyncio.wait_for(connection, timeout=budget)
            except TimeoutError:
                self._record_error(name, "restart timed out")
                return False
            except Exception as exc:
                self._record_error(name, str(exc))
                return False
            else:
                handle = self.handle_pool.get(self.handle_key_for(name, None))
                return handle is not None and (
                    not isinstance(handle.stack, McpConnectionOwner) or handle.stack.alive
                )

    async def call_tool_on_server(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict,
        *,
        request_context: McpRequestContext,
    ) -> McpCompleteOutcome:
        """Authorize one exact operation before binding any workload handle or bearer。"""
        config = self.registry.get_server(server_name)
        if config is not None and not config.allows_tool(tool_name):
            raise ToolDisabledError(server_name, tool_name)
        authorization = await self._authorize_oauth_operation(
            name=server_name,
            config=config,
            request_context=request_context,
            operation="tools/call",
            selector=tool_name,
            arguments=arguments,
        )
        with self._workload_authorization_scope(authorization):
            return await self._call_tool_on_authorized_server(
                server_name,
                tool_name,
                arguments,
                request_context=request_context,
            )

    async def _call_tool_on_authorized_server(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict,
        *,
        request_context: McpRequestContext,
    ) -> McpCompleteOutcome:
        """在明確 request context 下呼叫工具並回傳 canonical outcome。"""
        if server_name == "ToukaUI" and tool_name in ["openDiff", "closeDiff"]:
            try:
                result = await call_ide_tool(
                    self._ide_clients,
                    tool_name,
                    arguments,
                    human_approved=request_context.human_approved,
                )
            except McpMrtrAuthorizationError:
                raise
            except Exception as exc:
                error = adapt_call_exception(
                    exc,
                    sanitize=lambda message: self._sanitize_for(
                        server_name,
                        message,
                    ),
                    source_protocol_version="toukaide",
                )
                raise McpOperationException(error) from None
            return adapt_call_tool_result(
                result,
                source_protocol_version="toukaide",
            )

        # 軸 A 執行面咽喉（ADR-0195）：廣告面已濾掉關閉的工具，但模型可能從歷史脈絡、
        # 技能文本或猜測叫出未廣告的名字——供給面必須自己 fail-closed，否則「廣告面＝
        # 執法面」只是慣例而非保證。刻意置於斷路器之前：被關掉的工具不該消耗失敗計數。
        cfg = self.registry.get_server(server_name)
        if cfg is not None and not cfg.allows_tool(tool_name):
            raise ToolDisabledError(server_name, tool_name)

        key = self.handle_key_for(server_name, request_context)
        breaker = self._breaker_for(key)
        authorization_error: McpMrtrAuthorizationError | None = None
        current_authority = _CURRENT_WORKLOAD_AUTHORIZATION.get()
        invocation_authority = (
            current_authority if isinstance(current_authority, McpInvocationAuthorization) else None
        )

        async def _do_call() -> McpCompleteOutcome | None:
            nonlocal authorization_error
            async with self._workload_bootstrap_scope(
                server_name=server_name,
                authorization=invocation_authority,
            ):
                handle = await self._ensure_handle(server_name, request_context)
                # Listing is both the SDK's required x-mcp-header priming step and
                # Touka's ingest quarantine. Cache makes the common path local.
                with self._cache_scope(server_name, request_context):
                    catalog = await self._tool_catalog_from_session(
                        server_name,
                        handle.session,
                    )
            try:
                _validate_catalog_header_policy(
                    catalog,
                    server_name=server_name,
                    tool_name=tool_name,
                    arguments=arguments,
                    approved_for_sensitive=bool(cfg and tool_name in cfg.approved_header_tools),
                )
                with (
                    self._telemetry.span(
                        "tools/call",
                        enabled=bool(cfg and cfg.telemetry_enabled),
                        attributes={
                            "mcp.protocol.version": _protocol_version(handle.session),
                        },
                    ),
                    self._cache_scope(server_name, request_context),
                ):
                    low_level = _modern_low_level_session(handle.session)
                    if low_level is None:
                        result = await handle.session.call_tool(
                            tool_name,
                            arguments,
                        )
                    else:
                        binding = self._mrtr_binding(
                            server_name,
                            request_context=request_context,
                            method="tools/call",
                            operation_name=tool_name,
                            arguments=arguments,
                        )

                        async def _retry_tool(
                            responses: types.InputResponses | None,
                            state: str | None,
                        ) -> Any:
                            return await low_level.call_tool(
                                tool_name,
                                arguments,
                                input_responses=responses,
                                request_state=state,
                                allow_input_required=True,
                                allow_claimed=True,
                            )

                        first = await _retry_tool(None, None)
                        result = await self._drive_mrtr(
                            first,
                            retry=_retry_tool,
                            binding=binding,
                        )
            except McpMrtrAuthorizationError as exc:
                # 本機 retry 前撤權不是上游故障。讓 breaker 將已成功回覆
                # input_required 的 transport 視為健康，再於 breaker 外保留精確型別。
                authorization_error = exc
                return None
            except McpToolCallRefusal:
                # 呼叫前的拒絕（清單、標頭參數）不是上游故障：斷路器中立、不寫健康頁。
                raise
            except Exception as exc:
                error = adapt_call_exception(
                    exc,
                    sanitize=lambda message: self._sanitize_for(server_name, message),
                    source_protocol_version=_protocol_version(handle.session),
                )
                error = error.model_copy(
                    update={"message": self._sanitize_for(
                        server_name, f"MCP tool '{tool_name}' failed: {error.message}"
                    )}
                )
                self._record_error(server_name, error.message, key=key)
                raise McpOperationException(error) from None
            return adapt_call_tool_result(
                result,
                source_protocol_version=_protocol_version(handle.session),
            )

        try:
            outcome = await breaker.call_async(_do_call)
        except (McpMrtrAuthorizationError, McpOperationException):
            raise
        except McpToolCallRefusal as refusal:
            logger.warning("MCP tool call refused %s/%s: %s", server_name, tool_name, refusal)
            raise
        except Exception as exc:
            error = adapt_call_exception(
                exc,
                sanitize=lambda message: self._sanitize_for(server_name, message),
                source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
            )
            self._record_error(server_name, error.message, key=key)
            logger.warning(
                "呼叫工具 %s 失敗: %s", self._sanitize_for(server_name, tool_name), error.message
            )
            raise McpOperationException(error) from None
        if authorization_error is not None:
            raise authorization_error
        assert outcome is not None
        return outcome

    async def _ensure_handle(
        self,
        server_name: str,
        request_context: McpRequestContext | None,
    ) -> LegacyMcpHandle:
        """取得 handle；principal-scoped installation 僅在 request-time 建立。"""
        key = self.handle_key_for(server_name, request_context)
        handle = self.handle_pool.get(key)
        if handle is not None:
            return handle
        lock = (
            self._installation_lock(server_name, timeout=MCP_HTTP_RESTART_LOCK_SECONDS)
            if key.is_shared
            else self._connect_locks.setdefault(key, asyncio.Lock())
        )
        try:
            return await self._connect_missing_handle(server_name, request_context, lock)
        except (McpLaunchWaiting, McpServerBusyError) as exc:
            message = MCP_SERVER_WAITING if isinstance(exc, McpLaunchWaiting) else MCP_SERVER_BUSY
            error = adapt_call_exception(
                McpServerWaiting(message),
                sanitize=partial(self._sanitize_for, server_name),
                source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
            )
            raise McpOperationException(error) from None

    async def _connect_missing_handle(
        self, server_name: str, request_context: McpRequestContext | None, lock: Any
    ) -> LegacyMcpHandle:
        async with lock:
            key = self.handle_key_for(server_name, request_context)
            handle = self.handle_pool.get(key)
            if handle is not None:
                return handle
            config = self.registry.get_server(server_name)
            if config is None:
                raise ServerNotFoundError(server_name)
            try:
                decision = await self._judge(server_name, config)
                await self.connect_server(
                    server_name, config, request_context, **decision
                )
            except McpLaunchWaiting:
                raise
            except Exception as exc:
                error = adapt_call_exception(
                    exc,
                    sanitize=lambda message: self._sanitize_for(server_name, message),
                    source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
                )
                self._record_error(server_name, error.message, key=key)
                raise McpOperationException(error) from None
            handle = self.handle_pool.get(key)
            if handle is None:
                error = adapt_call_exception(
                    ConnectionError("MCP connector returned without a handle."),
                    sanitize=lambda message: self._sanitize_for(server_name, message),
                    source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
                )
                raise McpOperationException(error)
            return handle

    async def _get_catalog_session(
        self,
        name: str,
        request_context: McpRequestContext | None,
    ) -> Any | None:
        """取得精確 principal handle；無 context 時只讀既有 shared handle。"""
        config = self.registry.get_server(name)
        if config is None:
            raise ServerNotFoundError(name)
        if request_context is None:
            return self._active_sessions.get(name)
        scoped_context = request_context.model_copy(update={"installation_id": name})
        key = self.handle_key_for(name, scoped_context)
        handle = self.handle_pool.get(key)
        # shared installation 應由生命週期啟動；管理頁讀取不應替故障的 shared
        # server 偷偷建立第二條連線。principal-scoped installation 則依 P1 合約
        # 於請求時 lazy-connect，且必須使用完整 principal key。
        if handle is None and config.is_principal_scoped:
            handle = await self._ensure_handle(name, scoped_context)
        return handle.session if handle is not None else None

    def _catalog_issue(
        self,
        server_name: str,
        item_name: str,
        exc: BaseException,
    ) -> McpCatalogIssue:
        return McpCatalogIssue(
            item_name=self._sanitize_for(server_name, item_name),
            code=type(exc).__name__,
            message=self._sanitize_for(server_name, str(exc)),
        )

    async def _tool_catalog_from_session(
        self,
        name: str,
        session: Any,
    ) -> McpToolCatalog:
        version = _protocol_version(session)
        if _feature_support(session, "tools") is False:
            return McpToolCatalog(
                source_protocol_version=version,
                supported=False,
            )
        list_tools = getattr(session, "list_tools", None)
        if not callable(list_tools):
            return McpToolCatalog(
                source_protocol_version=version,
                supported=False,
            )
        config = self.registry.get_server(name)
        try:
            with self._telemetry.span(
                "tools/list",
                enabled=bool(config and config.telemetry_enabled),
                attributes={"mcp.protocol.version": version},
            ):
                raw_tools = await collect_paginated(
                    list_tools,
                    item_field="tools",
                )
        except Exception as exc:
            if _is_method_not_found(exc):
                return McpToolCatalog(
                    source_protocol_version=version,
                    supported=False,
                )
            raise
        items: list[McpToolDescriptor] = []
        issues: list[McpCatalogIssue] = []
        for raw_tool in raw_tools:
            item_name = str(getattr(raw_tool, "name", "<unknown>"))
            try:
                descriptor = adapt_tool_descriptor(
                    raw_tool,
                    source_protocol_version=version,
                )
                normalized_input = normalize_mcp_schema(
                    descriptor.input_schema,
                    require_object_root=True,
                )
                normalized_output = (
                    normalize_mcp_schema(
                        descriptor.output_schema,
                        require_object_root=False,
                    )
                    if descriptor.output_schema is not None
                    else None
                )
                inspect_tool_header_policy(
                    normalized_input,
                    approved_for_sensitive=bool(
                        config and descriptor.name in config.approved_header_tools
                    ),
                )
                normalized = descriptor.model_copy(
                    update={
                        "input_schema": normalized_input,
                        "output_schema": normalized_output,
                    }
                )
                items.append(
                    await self._safe_descriptor_icons(
                        name,
                        item_name,
                        normalized,
                        issues,
                    )
                )
            except (
                McpHeaderPolicyError,
                McpSchemaError,
                ValueError,
                TypeError,
            ) as exc:
                issues.append(self._catalog_issue(name, item_name, exc))
        return McpToolCatalog(
            source_protocol_version=version,
            items=items,
            issues=issues,
        )

    async def list_server_tool_descriptors(
        self,
        name: str,
        *,
        request_context: McpRequestContext | None = None,
    ) -> McpToolCatalog:
        """走完所有 cursor，回傳未經軸 A 過濾的 canonical tool descriptors。"""
        session = await self._get_catalog_session(name, request_context)
        if session is None:
            return McpToolCatalog(
                source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
            )
        with self._cache_scope(name, request_context):
            catalog = await self._tool_catalog_from_session(name, session)
        self._mark_fresh(name, request_context, "tools")
        return catalog

    async def list_server_resources(
        self,
        name: str,
        *,
        request_context: McpRequestContext | None = None,
    ) -> McpResourceCatalog:
        """列出全部 resources；未宣告 feature 時不發 wire request。"""
        session = await self._get_catalog_session(name, request_context)
        if session is None:
            return McpResourceCatalog(
                source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
            )
        version = _protocol_version(session)
        if _feature_support(session, "resources") is False:
            return McpResourceCatalog(
                source_protocol_version=version,
                supported=False,
            )
        method = getattr(session, "list_resources", None)
        if not callable(method):
            return McpResourceCatalog(
                source_protocol_version=version,
                supported=False,
            )
        config = self.registry.get_server(name)
        try:
            with (
                self._telemetry.span(
                    "resources/list",
                    enabled=bool(config and config.telemetry_enabled),
                    attributes={"mcp.protocol.version": version},
                ),
                self._cache_scope(name, request_context),
            ):
                raw_items = await collect_paginated(
                    method,
                    item_field="resources",
                )
        except Exception as exc:
            if _is_method_not_found(exc):
                return McpResourceCatalog(
                    source_protocol_version=version,
                    supported=False,
                )
            raise
        items: list[McpResourceDescriptor] = []
        issues: list[McpCatalogIssue] = []
        for raw_item in raw_items:
            item_name = str(getattr(raw_item, "name", "<unknown>"))
            try:
                descriptor = adapt_resource_descriptor(
                    raw_item,
                    source_protocol_version=version,
                )
                items.append(
                    await self._safe_descriptor_icons(
                        name,
                        item_name,
                        descriptor,
                        issues,
                    )
                )
            except (ValueError, TypeError) as exc:
                issues.append(self._catalog_issue(name, item_name, exc))
        catalog = McpResourceCatalog(
            source_protocol_version=version,
            items=items,
            issues=issues,
        )
        self._mark_fresh(name, request_context, "resources")
        return catalog

    async def list_server_resource_templates(
        self,
        name: str,
        *,
        request_context: McpRequestContext | None = None,
    ) -> McpResourceTemplateCatalog:
        """列出全部 resource templates。"""
        session = await self._get_catalog_session(name, request_context)
        if session is None:
            return McpResourceTemplateCatalog(
                source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
            )
        version = _protocol_version(session)
        if _feature_support(session, "resources") is False:
            return McpResourceTemplateCatalog(
                source_protocol_version=version,
                supported=False,
            )
        method = getattr(session, "list_resource_templates", None)
        if not callable(method):
            return McpResourceTemplateCatalog(
                source_protocol_version=version,
                supported=False,
            )
        config = self.registry.get_server(name)
        try:
            with (
                self._telemetry.span(
                    "resources/templates/list",
                    enabled=bool(config and config.telemetry_enabled),
                    attributes={"mcp.protocol.version": version},
                ),
                self._cache_scope(name, request_context),
            ):
                raw_items = await collect_paginated(
                    method,
                    item_field="resource_templates",
                )
        except Exception as exc:
            if _is_method_not_found(exc):
                return McpResourceTemplateCatalog(
                    source_protocol_version=version,
                    supported=False,
                )
            raise
        items: list[McpResourceTemplateDescriptor] = []
        issues: list[McpCatalogIssue] = []
        for raw_item in raw_items:
            item_name = str(getattr(raw_item, "name", "<unknown>"))
            try:
                descriptor = adapt_resource_template_descriptor(
                    raw_item,
                    source_protocol_version=version,
                )
                items.append(
                    await self._safe_descriptor_icons(
                        name,
                        item_name,
                        descriptor,
                        issues,
                    )
                )
            except (ValueError, TypeError) as exc:
                issues.append(self._catalog_issue(name, item_name, exc))
        catalog = McpResourceTemplateCatalog(
            source_protocol_version=version,
            items=items,
            issues=issues,
        )
        self._mark_fresh(name, request_context, "resources")
        return catalog

    async def read_server_resource(
        self,
        name: str,
        uri: str,
        *,
        request_context: McpRequestContext,
    ) -> McpResourceReadResult:
        """讀取絕對非 file URI；只透傳給精確 MCP handle，不碰本機 filesystem。"""
        validated_uri = validate_resource_uri(uri)
        config = self.registry.get_server(name)
        await self._authorize_oauth_operation(
            name=name,
            config=config,
            request_context=request_context,
            operation="resources/read",
            selector=validated_uri,
            arguments={},
        )
        session = await self._get_catalog_session(name, request_context)
        if session is None or _feature_support(session, "resources") is False:
            raise McpFeatureUnsupportedError(name, "resources")
        method = getattr(session, "read_resource", None)
        if not callable(method):
            raise McpFeatureUnsupportedError(name, "resources/read")
        await self._watch_resource(name, request_context, validated_uri)
        try:
            with (
                self._telemetry.span(
                    "resources/read",
                    enabled=bool(config and config.telemetry_enabled),
                    attributes={
                        "mcp.protocol.version": _protocol_version(session),
                    },
                ),
                self._cache_scope(name, request_context),
            ):
                low_level = _modern_low_level_session(session)
                if low_level is None:
                    result = await asyncio.wait_for(
                        method(validated_uri),
                        timeout=MCP_LIST_TIMEOUT_SECONDS,
                    )
                else:
                    binding = self._mrtr_binding(
                        name,
                        request_context=request_context,
                        method="resources/read",
                        operation_name=validated_uri,
                        arguments={"uri": validated_uri},
                    )

                    async def _retry_resource(
                        responses: types.InputResponses | None,
                        state: str | None,
                    ) -> Any:
                        return await low_level.read_resource(
                            validated_uri,
                            input_responses=responses,
                            request_state=state,
                            allow_input_required=True,
                        )

                    first = await _retry_resource(None, None)
                    result = await self._drive_mrtr(
                        first,
                        retry=_retry_resource,
                        binding=binding,
                    )
        except Exception as exc:
            if _is_method_not_found(exc):
                raise McpFeatureUnsupportedError(name, "resources/read") from exc
            raise
        adapted = adapt_read_resource_result(
            result,
            source_protocol_version=_protocol_version(session),
        )
        self._mark_fresh(name, request_context, "resources")
        return adapted

    async def list_server_prompts(
        self,
        name: str,
        *,
        request_context: McpRequestContext | None = None,
    ) -> McpPromptCatalog:
        """列出全部 prompts；每筆 descriptor 強型別驗證。"""
        session = await self._get_catalog_session(name, request_context)
        if session is None:
            return McpPromptCatalog(
                source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
            )
        version = _protocol_version(session)
        if _feature_support(session, "prompts") is False:
            return McpPromptCatalog(
                source_protocol_version=version,
                supported=False,
            )
        method = getattr(session, "list_prompts", None)
        if not callable(method):
            return McpPromptCatalog(
                source_protocol_version=version,
                supported=False,
            )
        config = self.registry.get_server(name)
        try:
            with (
                self._telemetry.span(
                    "prompts/list",
                    enabled=bool(config and config.telemetry_enabled),
                    attributes={"mcp.protocol.version": version},
                ),
                self._cache_scope(name, request_context),
            ):
                raw_items = await collect_paginated(
                    method,
                    item_field="prompts",
                )
        except Exception as exc:
            if _is_method_not_found(exc):
                return McpPromptCatalog(
                    source_protocol_version=version,
                    supported=False,
                )
            raise
        items: list[McpPromptDescriptor] = []
        issues: list[McpCatalogIssue] = []
        for raw_item in raw_items:
            item_name = str(getattr(raw_item, "name", "<unknown>"))
            try:
                descriptor = adapt_prompt_descriptor(
                    raw_item,
                    source_protocol_version=version,
                )
                items.append(
                    await self._safe_descriptor_icons(
                        name,
                        item_name,
                        descriptor,
                        issues,
                    )
                )
            except (ValueError, TypeError) as exc:
                issues.append(self._catalog_issue(name, item_name, exc))
        catalog = McpPromptCatalog(
            source_protocol_version=version,
            items=items,
            issues=issues,
        )
        self._mark_fresh(name, request_context, "prompts")
        return catalog

    async def get_server_prompt(
        self,
        name: str,
        prompt_name: str,
        arguments: dict[str, str] | None,
        *,
        request_context: McpRequestContext,
    ) -> McpPromptResult:
        """取得強型別外部 prompt，結果永遠標記為 untrusted。"""
        prompt_name = validate_catalog_name(prompt_name, kind="prompt")
        validated_arguments = validate_prompt_arguments(arguments)
        config = self.registry.get_server(name)
        await self._authorize_oauth_operation(
            name=name,
            config=config,
            request_context=request_context,
            operation="prompts/get",
            selector=prompt_name,
            arguments=validated_arguments,
        )
        session = await self._get_catalog_session(name, request_context)
        if session is None or _feature_support(session, "prompts") is False:
            raise McpFeatureUnsupportedError(name, "prompts")
        method = getattr(session, "get_prompt", None)
        if not callable(method):
            raise McpFeatureUnsupportedError(name, "prompts/get")
        try:
            with self._telemetry.span(
                "prompts/get",
                enabled=bool(config and config.telemetry_enabled),
                attributes={"mcp.protocol.version": _protocol_version(session)},
            ):
                low_level = _modern_low_level_session(session)
                if low_level is None:
                    result = await asyncio.wait_for(
                        method(prompt_name, validated_arguments),
                        timeout=MCP_LIST_TIMEOUT_SECONDS,
                    )
                else:
                    binding = self._mrtr_binding(
                        name,
                        request_context=request_context,
                        method="prompts/get",
                        operation_name=prompt_name,
                        arguments=validated_arguments,
                    )

                    async def _retry_prompt(
                        responses: types.InputResponses | None,
                        state: str | None,
                    ) -> Any:
                        return await low_level.get_prompt(
                            prompt_name,
                            validated_arguments,
                            input_responses=responses,
                            request_state=state,
                            allow_input_required=True,
                        )

                    first = await _retry_prompt(None, None)
                    result = await self._drive_mrtr(
                        first,
                        retry=_retry_prompt,
                        binding=binding,
                    )
        except Exception as exc:
            if _is_method_not_found(exc):
                raise McpFeatureUnsupportedError(name, "prompts/get") from exc
            raise
        return adapt_get_prompt_result(
            result,
            source_protocol_version=_protocol_version(session),
        )

    async def get_all_tools(
        self,
        request_context: McpRequestContext | None = None,
    ) -> list[dict[str, Any]]:
        """列出 shared installations 的工具並在最後一步投影為 provider function schema。

        principal-scoped catalog 必須有 request context；OAuth 尚未於 P1 開放，故不從其他
        principal handle 聚合，避免私有 catalog 跨人顯示。
        """
        all_tools = []
        for name, state in self._installations.items():
            scoped_context = (
                request_context.model_copy(update={"installation_id": name})
                if request_context is not None
                else None
            )
            if state.config.is_principal_scoped and scoped_context is None:
                continue
            key = self.handle_key_for(name, scoped_context)
            handle = self.handle_pool.get(key)
            if handle is None:
                pending = self._book.get(name)
                lock = self._installation_locks.get(name)
                if (pending is not None and pending.state != "launched") or (
                    lock is not None and lock.locked()
                ):
                    continue
                # P1 不讓 identity-less catalog enumeration 觸發私有連線；有精確 request
                # context 時才可 lazy connect。
                if scoped_context is None:
                    continue
                try:
                    handle = await self._ensure_handle(name, scoped_context)
                except McpOperationException:
                    continue
            if isinstance(handle.stack, McpConnectionOwner) and not handle.stack.alive:
                continue
            session = handle.session
            breaker = self._circuit_breakers.get(name) or self._circuit_breakers.get(key)
            if breaker and breaker.state.value not in ("CLOSED", "HALF_OPEN"):
                continue
            cfg = self.registry.get_server(name)
            try:
                with self._cache_scope(name, scoped_context):
                    catalog = await self._tool_catalog_from_session(name, session)
                self._mark_fresh(name, scoped_context, "tools")
                for issue in catalog.issues:
                    logger.warning(
                        "隔離 MCP tool descriptor %s/%s：%s",
                        name,
                        self._sanitize_for(name, issue.item_name or ""),
                        self._sanitize_for(name, issue.message),
                    )
                for descriptor in catalog.items:
                    # 軸 A 廣告面（ADR-0195）：per-installation 關閉的工具連進都不進，
                    # 故所有下游消費者（ToolLayer 廣告、debug 面板、觀測性、guard_service）
                    # 天然一致——不需要各自再記得濾一次。
                    if cfg is not None and not cfg.allows_tool(descriptor.name):
                        continue
                    all_tools.append(
                        {
                            "server": name,
                            # 裸工具名（additive，ADR-0195）：廣告名是 `server__tool` 前綴形式，
                            # 而軸 B 授權與設定面用的都是裸名。由此處一次投影為 SSOT，避免每個
                            # 消費端各自 split("__") 重刻——伺服器名本身含雙底線時就會切錯。
                            "tool": descriptor.name,
                            "type": "function",
                            "function": {
                                "name": f"{name}__{descriptor.name}",
                                "description": descriptor.description,
                                "parameters": descriptor.input_schema,
                            },
                        }
                    )
            except Exception as exc:
                logger.warning(
                    "讀取 server %s 的工具失敗：%s", name, self._sanitize_for(name, str(exc))
                )

        if "ToukaUI" in self._ide_clients:
            all_tools.append(
                {
                    "server": "ToukaUI",
                    "type": "function",
                    "function": {
                        "name": "openDiff",
                        "description": "在 ToukaUI 編輯器中開啟原生差異比對視窗。",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "filePath": {"type": "string"},
                                "newContent": {"type": "string"},
                            },
                            "required": ["filePath", "newContent"],
                        },
                    },
                }
            )
        return all_tools

    async def list_server_tools(self, name: str) -> list[dict[str, str]]:
        """列出單一伺服器**未經軸 A 過濾**的完整工具面（ADR-0195）。

        治理 UI 需要看到「全部工具＋哪些被勾選」，而 ``get_all_tools`` 依設計已濾掉關閉者
        ——拿它渲染勾選框會讓取消勾選的工具直接從清單消失、再也勾不回來。故另闢此入口。

        Args:
            name: MCP 伺服器鍵。

        Returns:
            list[dict[str, str]]: ``[{"name": 裸名, "description": ...}]``；未連線回空清單。

        Raises:
            ServerNotFoundError: 該伺服器不在註冊表（含被 ``enabled:false`` 排除者）。
        """
        catalog = await self.list_server_tool_descriptors(name)
        return [
            {
                "name": descriptor.name,
                "description": descriptor.description,
            }
            for descriptor in catalog.items
        ]

    async def get_active_server_info(self) -> list[dict[str, Any]]:
        """回傳**所有已設定** MCP 伺服器的健康狀態（含未連線/失敗者，供健康管理 UI）。

        狀態精準度：circuit_open（斷路器開）> connected（session 可列工具）> error（session 在但
        列工具失敗、或啟動失敗有 last_error）> not_started。錯誤訊息一律脫敏（不洩漏金鑰/路徑）。
        """
        results = []
        for name, state in self._installations.items():
            meta = self._server_meta.get(name, {})
            cache_store = self._cache_stores.get(name)
            cache_status: dict[str, Any] = {
                "enabled": state.config.cache_enabled,
                **(
                    cache_store.snapshot()
                    if cache_store is not None
                    else {
                        "entries": 0,
                        "public_entries": 0,
                        "private_entries": 0,
                        "hits": 0,
                        "misses": 0,
                        "writes": 0,
                        "evictions": 0,
                        "max_entries": state.config.cache_max_entries,
                    }
                ),
            }
            subscription_snapshots = [
                supervisor.snapshot()
                for key, supervisor in self._subscription_supervisors.items()
                if key.installation_id == name
            ]
            subscription_status: dict[str, Any] = {
                "enabled": state.config.subscriptions_enabled,
                "status": (
                    "disabled"
                    if not state.config.subscriptions_enabled
                    else (
                        "not_started"
                        if not subscription_snapshots
                        else (
                            "stale"
                            if any(snapshot["stale"] for snapshot in subscription_snapshots)
                            else "listening"
                        )
                    )
                ),
                "stale": any(snapshot["stale"] for snapshot in subscription_snapshots),
                "reconnect_attempts": sum(
                    int(snapshot["reconnect_attempts"]) for snapshot in subscription_snapshots
                ),
                "handle_count": len(subscription_snapshots),
            }
            shared_key = (
                self.handle_key_for(name, None) if not state.config.is_principal_scoped else None
            )
            breaker = self._circuit_breakers.get(name)
            if breaker is None and shared_key is not None:
                breaker = self._circuit_breakers.get(shared_key)
            shared_handle = self.handle_pool.get(shared_key) if shared_key is not None else None
            alive = shared_handle is not None and (
                not isinstance(shared_handle.stack, McpConnectionOwner) or shared_handle.stack.alive
            )
            session = shared_handle.session if shared_handle is not None else None
            handle_entries = []
            for key in self.handle_pool.keys_for_installation(name):
                key_handle = self.handle_pool.get(key)
                key_breaker = self._circuit_breakers.get(key)
                handle_meta = self._handle_meta.get(key, {})
                handle_status = "connected"
                if handle_meta.get("last_error"):
                    handle_status = "error"
                if key_breaker and key_breaker.state == CircuitState.OPEN:
                    handle_status = "circuit_open"
                handle_entries.append(
                    {
                        "scope": "shared" if key.is_shared else "principal",
                        "status": handle_status,
                        "last_error": handle_meta.get("last_error"),
                        "last_error_at": handle_meta.get("last_error_at"),
                        "protocol_version": (
                            _protocol_version(key_handle.session)
                            if key_handle is not None
                            else None
                        ),
                    }
                )
            entry: dict[str, Any] = {
                "name": name,
                "scope": "installation",
                "status": "not_started",
                "tools_count": 0,
                "last_error": state.last_error or meta.get("last_error"),
                "last_error_at": state.last_error_at or meta.get("last_error_at"),
                "circuit_state": breaker.state.value if breaker else None,
                "restart_count": state.restart_count,
                "generation": state.generation,
                "handle_count": len(handle_entries),
                "handles": handle_entries,
                "url": "local-process",
                "protocol_version": None,
                "server_capabilities": None,
                "cache": cache_status,
                "subscription": subscription_status,
                # serverInfo 是 display-only；不可參與身分或授權判定。
                "server_info": None,
            }
            if breaker and breaker.state == CircuitState.OPEN:
                entry["status"] = "circuit_open"
            elif shared_handle is not None and not alive:
                entry["status"] = "error"
            elif session is not None:
                try:
                    # 逾時界限：避免單一卡死的 session 拖死整個健康清單端點（其餘伺服器照常回報）。
                    with self._cache_scope(name, None):
                        tools_result = await asyncio.wait_for(
                            session.list_tools(),
                            timeout=3.0,
                        )
                    entry["tools_count"] = len(tools_result.tools)
                    entry["status"] = "connected"
                    entry["protocol_version"] = _protocol_version(session)
                    entry["server_capabilities"] = self._sanitize_metadata(
                        name, _connection_fact(getattr(session, "server_capabilities", None))
                    )
                    entry["server_info"] = self._sanitize_metadata(
                        name, _connection_fact(getattr(session, "server_info", None))
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to get tools count for %s: %s",
                        name, self._sanitize_for(name, str(e)),
                    )
                    entry["status"] = "error"
                    entry["last_error"] = self._sanitize_for(
                        name,
                        str(e) or "health check timed out",
                    )
                    entry["last_error_at"] = get_current_epoch()
            elif state.last_error or meta.get("last_error"):
                entry["status"] = "error"
            entry["isolation"] = self._book.public(
                name, connected=alive, manual_start=self._manual_start_view(name, state)
            )
            results.append(entry)
        return results

    def _manual_start_view(self, name: str, state: InstallationState) -> dict[str, object] | None:
        plan = self.manual_starter.manual_start_plan(name)
        if plan is None:
            return None
        return {
            "fallback": plan.kind, "features": sorted(plan.features),
            "generation": state.generation, "bound": bound_of(entry_identity(state.config)),
        }

    async def cleanup(self) -> None:
        """關閉所有開啟的伺服器，優雅處理 ASGI 生命週期。"""
        self._lifecycle_epoch += 1
        for supervisor in list(self._subscription_supervisors.values()):
            await supervisor.stop()
        self._subscription_supervisors.clear()
        self._resource_subscriptions.clear()
        for store in list(self._cache_stores.values()):
            await store.clear()
        self._cache_stores.clear()
        # life1：所有 owner 並行關，共用一個總期限（期限＋寬限），不論幾台都不疊加。
        loop = asyncio.get_running_loop()
        deadline = loop.time() + OWNER_CLOSE_SECONDS + OWNER_CLOSE_GRACE_SECONDS
        handles = self.handle_pool.pop_all()
        pooled = {handle.stack for handle in handles}
        for owner in list(self._connection_owners):
            if owner not in pooled:
                owner.abort()  # 握手中的中止（不被 with_retry 重試）；已連上還沒裝的要求關閉
        await self._close_handles(handles, concurrent=True)
        self._connect_locks.clear()
        running = {owner.task for owner in self._connection_owners if owner.task is not None}
        if running:
            _, pending = await asyncio.wait(running, timeout=max(deadline - loop.time(), 0.0))
            labels = sorted(o.label for o in self._connection_owners if o.task in pending)
            if labels:  # 只記件數與名稱，不記錯誤內容；之後不再等
                logger.warning(
                    "MCP cleanup: %d connection(s) still closing: %s", len(labels), labels
                )
        if not self._connection_owners:
            self._launch_secrets.clear()
        # 等待期間才裝進 pool 的（上面已要求關閉）：正常收掉，不取消。
        await self._close_handles(self.handle_pool.pop_all(), concurrent=True)

    async def _close_handles(
        self,
        handles: list[LegacyMcpHandle],
        *,
        concurrent: bool = False,
    ) -> None:
        """關閉一批 handle；單一 teardown 失敗不得中止其餘清理（cleanup 並行關）。"""
        if concurrent and len(handles) > 1:
            await asyncio.gather(*(self._close_handles([handle]) for handle in handles))
            return
        for handle in handles:
            try:
                # owner 自己掌握關閉期限；其他 stack 以同一期限 wait_for，防止容器無法退出
                await handle.close(timeout=OWNER_CLOSE_SECONDS)
            except asyncio.CancelledError:
                # uvicorn lifespan 強制關機時 asyncio task 被取消，為預期行為
                logger.debug("🔌 MCP shutdown cancelled (expected during uvicorn lifespan exit)")
            except TimeoutError:
                logger.warning("⚠️ Timeout closing MCP stack — forcing shutdown")
            except RuntimeError as e:
                # 攔截 AnyIO 在 FastAPI shutdown 時因 Task 不一致發出的 cancel scope 錯誤
                if "cancel scope" in str(e).lower():
                    logger.debug("🔌 MCP shutdown cancel scope error suppressed")
                else:
                    logger.warning("⚠️ Error closing MCP stack: %s", type(e).__name__)
            except Exception as e:
                logger.warning("⚠️ Error closing MCP stack: %s", type(e).__name__)
