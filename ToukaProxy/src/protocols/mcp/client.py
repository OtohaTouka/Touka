"""MCP SDK v2 public Client 連線適配器。

支援兩種傳輸：**stdio**（本地子進程 IPC，處理環境變數注入與哨兵還原）與 **remote
streamable HTTP**（ADR-0192；憑證存 ``env``、header 以 ``${VAR}`` 引用後於連線時解析）。
兩者共用同一套 negotiation 逾時與 ``AsyncExitStack`` 生命週期管理。

stdio 有兩條路（iso-c2 C2-3）：啟動判定給了 runner（``McpIsolatedLaunch``）才走自寫的隔離傳輸
（``isolated_stdio``）；其餘（direct）照舊用 SDK 的 ``stdio_client``。

SSE 傳輸刻意不支援：三家桌面版均已將其標為 deprecated，主線為 streamable HTTP。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

from mcp import Client, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from src.config import settings
from src.protocols.mcp.environment import LaunchEnvironmentPolicy
from src.protocols.mcp.http_client import build_mcp_http_client
from src.protocols.mcp.interaction import decline_unbound_elicitation
from src.protocols.mcp.isolated_stdio import isolated_stdio_client
from src.protocols.mcp.oauth.auth import McpOAuthBearerAuth
from src.protocols.mcp.oauth.outbound import PinnedOAuthTransport
from src.services.isolation.runners import ISOLATION_UNAVAILABLE, IsolationUnavailableError
from src.utils.mcp_remote import resolve_headers
from src.utils.mcp_sentinels import resolve_args_sentinels

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.client import CacheConfig
    from src.protocols.mcp.isolated_stdio import McpEofMode
    from src.protocols.mcp.oauth.auth import OAuthCredentialProvider
    from src.protocols.mcp.oauth.outbound import IPNetwork
    from src.protocols.registry import ProtocolServerConfig
    from src.services.isolation.mcp_launch import McpLaunch

logger = logging.getLogger(__name__)

# 初次 negotiation 的回應等待上限（ADR-0191／ADR-0199）。
#
# 沒有它時，「進程起得來但永不回應 JSON-RPC」的伺服器會讓 await 永遠不返回——掛住不是
# exception，``ProtocolManager.initialize_all`` 的 try/except 接不住，而該迴圈位於
# ``main.py`` 啟動 lifespan 且為序列執行 → 整個 ToukaProxy 開機流程永不完成、排在其後
# 的每一台 server 都連不上。金鑰無效的 stdio↔HTTP 橋接器正是此形態（遠端 401 只被
# ``console.error`` 吞掉、stdio 不關），等於「一個打錯字的金鑰＝全域開機拒絕服務」。
#
# 取 60s 是因為此等待**涵蓋首次 npx/uvx 的套件下載**（下載期間 initialize 請求已送出、
# 尚無回應），過短會誤殺慢速網路下的正常首裝。
#
# 已知邊界（刻意不在本批處理）：``ProtocolManager.connect_server`` 掛有
# ``@with_retry(max_retries=2)``，故單一掛死伺服器最壞會等 3×60s。仍是**有界**且
# 有錯誤日誌，與修復前的「永久靜默卡死」是質變；要進一步收斂需改動全系統共用的
# ``with_retry``（加例外跳過清單）或把 ``initialize_all`` 並行化，屬另案。
MCP_INIT_TIMEOUT_SECONDS = 60.0
# 隔離傳輸要 pass_fds、新 session 與 killpg：只在 POSIX。Windows 的判定不會給 runner，這是縱深
ISOLATED_STDIO_SUPPORTED = sys.platform != "win32"


@dataclass(frozen=True)
class McpIsolatedLaunch:
    """隔離路徑的 stdio 連線：判定結果加上傳輸要的記錄與回報（direct 不給這個）.

    ``on_eof``：在 ``McpConnectionOwner`` 裡連線用 ``raise``（伺服器結束＝失聯，``on_lost``
    一次）；不在 owner 裡的呼叫端（演化驗證、創建試跑）一律用 ``close``，否則伺服器結束時會
    取消呼叫端自己的 task。``report_failure`` 收啟動器／relay 的失敗代碼；``sanitize`` 在遮罩
    exec_env 之後再套到 stderr 的記錄；``stderr_tail`` 離開時收到最後一段 stderr（已遮罩）。
    """

    launch: McpLaunch
    label: str
    report_failure: Callable[[str], None]
    on_eof: McpEofMode
    sanitize: Callable[[str], str] | None = None
    stderr_tail: Callable[[str], None] | None = None


def tool_base_url() -> str:
    """Touka 給 stdio 伺服器的工具網址（``TOOL_BASE_URL``）；HOST 是 0.0.0.0 時用 localhost."""
    unspecified_host = str(ipaddress.IPv4Address(0))
    host = "localhost" if unspecified_host == settings.HOST else settings.HOST
    return f"http://{host}:{settings.PORT}"


async def connect_mcp_server(
    config: ProtocolServerConfig,
    *,
    cache_config: CacheConfig | None = None,
    launch_environment: dict[str, str] | None = None,
    read_timeout_seconds: float | None = None,
    oauth_credential_provider: OAuthCredentialProvider | None = None,
    oauth_allowed_private_cidrs: tuple[IPNetwork, ...] = (),
    isolated: McpIsolatedLaunch | None = None,
) -> tuple[Client, contextlib.AsyncExitStack]:
    """以 SDK v2 public ``Client`` 連線 stdio 或 remote server。

    ``launch_environment`` 僅供 Creation clean sandbox 傳入已淨化環境；正式 runtime
    省略時走 ``LaunchEnvironmentPolicy.production``。``isolated`` 只給判定包住的 stdio
    伺服器（隔離傳輸，見 ``McpIsolatedLaunch``）。所有連線都先送出
    ``server/discover``，完整驗證後才 adopt；任何失敗均關閉原 transport，
    不以舊協定重新連線。
    """
    effective_timeout = (
        MCP_INIT_TIMEOUT_SECONDS if read_timeout_seconds is None else read_timeout_seconds
    )
    client, stack = await _connect_once(
        config,
        mode=types.LATEST_PROTOCOL_VERSION,
        cache_config=cache_config,
        launch_environment=launch_environment,
        read_timeout_seconds=effective_timeout,
        oauth_credential_provider=oauth_credential_provider,
        oauth_allowed_private_cidrs=oauth_allowed_private_cidrs,
        isolated=isolated,
    )
    try:
        raw_discover = await client.session.send_discover(types.LATEST_PROTOCOL_VERSION)
        discover = types.DiscoverResult.model_validate(raw_discover)
        client.session.adopt(discover)
    except BaseException as error:
        await _discard_connection_stack(stack)
        _raise_connection_error(error)
    return client, stack


async def _connect_once(
    config: ProtocolServerConfig,
    *,
    mode: str,
    cache_config: CacheConfig | None,
    launch_environment: dict[str, str] | None,
    read_timeout_seconds: float,
    oauth_credential_provider: OAuthCredentialProvider | None,
    oauth_allowed_private_cidrs: tuple[IPNetwork, ...],
    isolated: McpIsolatedLaunch | None = None,
) -> tuple[Client, contextlib.AsyncExitStack]:
    """建立一次全新的 modern transport 與 Client 生命週期。"""
    stack = contextlib.AsyncExitStack()
    try:
        if config.is_remote:
            # remote（ADR-0192）：憑證存 env、header 以 ${VAR} 引用，於此解析成實值。
            # 缺變數時 resolve_headers fail-closed 拋錯——不送半成品憑證去換一台
            # 「連得上但每次呼叫都 401」的殭屍伺服器。
            headers = resolve_headers(config.headers, config.env)
            http_client = await stack.enter_async_context(
                build_mcp_http_client(
                    headers=headers,
                    auth=(
                        McpOAuthBearerAuth(oauth_credential_provider)
                        if oauth_credential_provider is not None
                        else None
                    ),
                    transport=(
                        PinnedOAuthTransport(allowed_private_cidrs=oauth_allowed_private_cidrs)
                        if oauth_credential_provider is not None
                        else None
                    ),
                )
            )
            target = streamable_http_client(
                config.url,
                http_client=http_client,
            )
        else:
            target = _build_stdio_transport(
                config,
                launch_environment=launch_environment,
                isolated=isolated,
            )

        # explicit modern mode 先建立 session，Touka 再於上層發出可控的 discover。
        client = await stack.enter_async_context(
            Client(
                target,
                mode=mode,
                cache=cache_config,
                read_timeout_seconds=read_timeout_seconds,
                # P7：只宣告 elicitation；Sampling／Roots 已 deprecated 且不宣告。
                # 真正 MRTR 由 manager 的同-coroutine low-level driver 處理；若 SDK
                # 收到沒有精確 request context 的 standalone callback，立即 decline。
                elicitation_callback=decline_unbound_elicitation,
            )
        )
    except BaseException as e:
        # 捕 BaseException 而非 Exception 是**必要**的（ADR-0194）：遠端握手失敗時，
        # transport 的 anyio 子任務可能因握手失敗取消其 cancel
        # scope，呼叫端收到的是繼承 BaseException 的 `CancelledError`——`except Exception`
        # 一律接不住，於是連 stack 都不會關，且該例外會逃逸 `connect_server` 的
        # `@with_retry` 與 `initialize_all` 的 except，**單一台憑證錯誤的遠端伺服器就能
        # 中斷整個開機迴圈**（實測：壞金鑰的 stripe 排在前面時，其後的健康伺服器連嘗試
        # 都沒嘗試）。這與 ADR-0191 修掉的「錯金鑰＝全域開機拒絕服務」是同一類事故。
        #
        # teardown 失敗也不得遮蔽原因：aclose() 常在不同 task 退出 cancel scope 而拋
        # RuntimeError，若不吞掉就會**取代** e，讓「token 無效」變成一句沒人看得懂的
        # "Attempted to exit cancel scope in a different task"。
        await _discard_connection_stack(stack)
        _raise_connection_error(e)
    else:
        return client, stack


async def _discard_connection_stack(stack: contextlib.AsyncExitStack) -> None:
    """清理失敗 attempt；teardown 錯誤不得遮蔽原始協商原因。"""
    with contextlib.suppress(Exception):
        await stack.aclose()


def _raise_connection_error(error: BaseException) -> NoReturn:
    """保留外部取消，只正規化 transport 內部洩漏的取消。"""
    if isinstance(error, asyncio.CancelledError):
        task = asyncio.current_task()
        # uncancel() 回傳「剩餘」的取消請求數：0 ＝ 這次取消完全源自本函式內部的
        # 傳輸層，可安全轉成一般連線錯誤讓上游照常處理；>0 ＝ 呼叫端也要求取消
        # （關機／中止），那必須原樣尊重，不得吞掉。
        if task is not None and task.uncancel() == 0:
            msg = "MCP 遠端連線在握手階段被傳輸層中止（常見於憑證無效或端點拒絕連線）。"
            raise ConnectionError(msg) from error
    raise error


def _build_stdio_transport(
    config: ProtocolServerConfig,
    *,
    launch_environment: dict[str, str] | None,
    isolated: McpIsolatedLaunch | None = None,
) -> Any:
    """建立 stdio transport；生命週期交由 SDK v2 ``Client``。"""
    if isolated is not None:
        return _isolated_stdio_transport(isolated)
    full_env = (
        dict(launch_environment)
        if launch_environment is not None
        else LaunchEnvironmentPolicy.production(config.env, tool_base_url=tool_base_url())
    )

    # args 哨兵（如官方 filesystem 的允許目錄 @TOUKA_APPDATA@）同樣於啟動時還原——
    # 與 env 同語意（ADR-0186），server_config.json 手寫哨兵亦可攜。
    server_params = StdioServerParameters(
        command=config.command, args=resolve_args_sentinels(config.args), env=full_env
    )
    return stdio_client(server_params)


def _isolated_stdio_transport(isolated: McpIsolatedLaunch) -> Any:
    """把 runner 改寫的啟動參數交給自寫傳輸；不支援的平台或沒有 runner 一律拒絕（不改走未隔離）."""
    acquired = isolated.launch.acquired
    if not ISOLATED_STDIO_SUPPORTED or acquired is None:
        raise IsolationUnavailableError(ISOLATION_UNAVAILABLE)
    return isolated_stdio_client(
        acquired.runner,
        isolated.launch.request(LaunchEnvironmentPolicy.launcher_base()),
        label=isolated.label,
        report_failure=isolated.report_failure,
        on_eof=isolated.on_eof,
        sanitize=isolated.sanitize,
        stderr_tail=isolated.stderr_tail,
    )
