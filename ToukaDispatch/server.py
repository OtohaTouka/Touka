"""ToukaDispatch — 專用任務派發 MCP 伺服器.

認證架構（ADR-0047 + ADR-0050 + ADR-0201）：
  - dispatch_handshake：呼叫方傳入 DISPATCH_TOKEN，server 以 secrets.compare_digest 驗證
  - 驗證通過後換取 session token（secrets.token_urlsafe(32)，TTL=3600s）
  - 每次操作工具呼叫都重新驗證 session token、transport、method、audience、generation
  - server 僅儲存 token hash；身份固定為 DispatchAgent
  - 移除呼叫方宣稱的 agent_id 參數，消除身份偽冒（Vuln 1）
  - agent_id 不再進入路徑拼接，消除路徑穿越攻擊面（Vuln 2）

架構：
  - 主端點：modern Streamable HTTP /mcp；不提供 legacy 握手或 HTTP+SSE 入口
  - 認證：parameter-level auth（DISPATCH_TOKEN + compare_digest）→ 換取 session token
  - 身分解析：固定 DispatchAgent（不查詢 identity_map.json）
  - Session 管理：SessionManager（SRP，ToukaDispatch/session_manager.py）
  - 任務管理：MissionManager（ToukaMemory，透過 PYTHONPATH 共享）
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import uvicorn
from mcp import types
from mcp.server import MCPServer, mcpserver
from mcp.shared.exceptions import MCPError
from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from memory_server.utils.content_guard import sanitize_memory_content
from session_manager import SessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from telemetry import DispatchTelemetry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send


logger = logging.getLogger(__name__)
MCPContext = mcpserver.Context

_DISPATCH_PORT = int(os.environ.get("DISPATCH_PORT", "8083"))  # noqa: ENV_SSOT_VIOLATION
_PROXY_URL = os.environ.get("TOUKA_PROXY_URL", "http://localhost:8000")  # noqa: ENV_SSOT_VIOLATION
def _bind_host() -> str:
    """綁定位址（ADR-0082）：預設 loopback——與 proxy 同容器時 localhost IPC 即足。

    需對外（如 compose 發布 8083 給主機原生外部 AI）時以 DISPATCH_BIND_HOST 顯式升開；
    「預設安全、顯式開放」取代原本硬編的 0.0.0.0。
    """
    return os.environ.get("DISPATCH_BIND_HOST", "127.0.0.1")  # noqa: ENV_SSOT_VIOLATION


_session_mgr = SessionManager()
_telemetry = DispatchTelemetry()


@dataclass(frozen=True)
class DispatchRuntimeContext:
    """MCPServer lifespan 注入的 transport 分面；不承載任何身分。"""

    transport: str


@asynccontextmanager
async def _modern_lifespan(
    _server: MCPServer[Any],
) -> AsyncIterator[DispatchRuntimeContext]:
    yield DispatchRuntimeContext(transport="modern")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _verify_dispatch_token(provided: str) -> bool:
    """Timing-safe comparison of caller-provided token against DISPATCH_TOKEN env var.

    Returns False immediately if either side is empty (禁令 33: empty token rejects all).
    """
    expected = os.environ.get("DISPATCH_TOKEN", "").strip()  # noqa: ENV_SSOT_VIOLATION
    if not expected or not provided.strip():
        return False
    return secrets.compare_digest(provided.strip(), expected)


def _request_facets(
    ctx: MCPContext | None,
) -> tuple[str, str | None]:
    """從 server-owned lifespan 取 transport；client metadata 不參與授權。"""
    if ctx is None:
        return "direct", None
    runtime = ctx.request_context.lifespan_context
    if not isinstance(runtime, DispatchRuntimeContext):
        return "direct", ctx.protocol_version
    return runtime.transport, ctx.protocol_version


def _validate_session_for_request(
    session_token: str,
    method: str,
    ctx: MCPContext | None,
) -> Any | None:
    """每次工具呼叫重新驗證 bearer 與 request 分面。"""
    transport, protocol_version = _request_facets(ctx)
    session = _session_mgr.validate(
        session_token,
        transport=transport,
        method=method,
    )
    if session is not None:
        _telemetry.record_tool_use(
            caller_type=session.caller_type,
            transport=transport,
            protocol_version=protocol_version,
        )
    return session


def _get_dispatch_identity() -> Any:
    """Fixed DispatchAgent identity for all external callers (Vuln 1 fix).

    All sessions are bound to DispatchAgent — caller-supplied identities are never trusted.
    """

    class _DispatchIdentity:  # noqa: IDENTITY_SSOT
        agent_id: str = "DispatchAgent"
        # ADR-0141 分面角色介面：Base＝agent 軀體、無人類權威。下游消費點
        # （soul_service／mission_manager）拆分後改讀 agent_role／is_privileged()。
        human_role: str | None = None
        agent_role: str | None = "Base"
        capabilities: list[str] = []

        @property
        def display_role(self) -> str:  # ADR-0141 P7：顯示面有效角色（＝Base）
            return self.human_role or self.agent_role or "Base"

        def is_privileged(self) -> bool:
            return False

    return _DispatchIdentity()


# ---------------------------------------------------------------------------
# Internal helpers — ADR-0067 auto-invoke (dispatch_hooks.json CaC)
# ---------------------------------------------------------------------------

_HOOKS_PATH = Path(__file__).parent / "config" / "dispatch_hooks.json"

# env whitelist（預設拒絕；只保留執行必需的系統 env + 注入的 DISPATCH_*）。
# 連不含關鍵字的密鑰（如 STRIPE_SK）也因不在白名單而被擋。若外部 AI 需非標準
# env，在此手動加入。
_CHILD_ENV_WHITELIST = frozenset(
    {
        "PATH",
        "PATHEXT",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "TERM",
        "COMSPEC",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "USERNAME",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMDATA",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
    }
)


def _load_dispatch_hooks() -> dict[str, Any]:
    """讀 dispatch_hooks.json 白名單。缺檔/壞檔/非 dict 回 {} (fail-closed)."""
    try:
        data = json.loads(_HOOKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("dispatch_hooks.json unavailable/invalid (%s); auto-invoke disabled", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _build_child_env(mission_token: str, mission_id: str) -> dict[str, str]:
    """以 whitelist 淨化父環境 → 注入 mission 範圍憑證 (mission_token 非全域主金鑰)."""
    parent = dict(os.environ)  # noqa: ENV_SSOT_VIOLATION
    clean = {k: v for k, v in parent.items() if k.upper() in _CHILD_ENV_WHITELIST}
    clean["DISPATCH_TOKEN"] = mission_token  # 一次性 mission_token，非全域主金鑰
    clean["DISPATCH_BOARD"] = mission_id
    clean["DISPATCH_URL"] = f"http://localhost:{_DISPATCH_PORT}/mcp"
    return clean


def _invoke_external_ai(executor: str, mission_id: str, mission_token: str) -> str:
    """依 dispatch_hooks.json 啟動外部 AI (fire-and-forget).

    失敗只 log + 回提示字串，不拋例外、不回滾 (mission 已建)。command 全來自
    白名單 JSON list 直傳 + shell=False → 零指令注入面。
    """
    entry = _load_dispatch_hooks().get("on_mission_created", {}).get(executor)
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("command"), list)
        or not entry["command"]
    ):
        logger.warning("auto_invoke='%s' not in dispatch_hooks.json; skipping", executor)
        return f"⚠️ Auto-invoke skipped: '{executor}' not in dispatch_hooks.json whitelist."

    command = [str(a) for a in entry["command"]]
    child_env = _build_child_env(mission_token, mission_id)
    try:
        subprocess.Popen(  # noqa: S603  # fire-and-forget；command 來自白名單，shell=False
            command,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            cwd=str(Path(__file__).parent),
        )
    except (OSError, ValueError) as exc:  # OSError 含 FileNotFoundError（PATH 無 claude）
        logger.exception("Failed to launch '%s' (mission=%s)", executor, mission_id)
        return (
            f"⚠️ Auto-invoke failed to launch '{executor}': {exc}. Mission created; invoke manually."
        )

    logger.info("External AI '%s' launched for mission=%s (fire-and-forget)", executor, mission_id)
    return f"🚀 Auto-invoked '{executor}' (fire-and-forget; awaiting callback)."


# ---------------------------------------------------------------------------
# Internal helpers — ADR-0051 callback channel
# ---------------------------------------------------------------------------


async def _notify_touka_proxy(
    callback_token: str,
    task_id: str,
    status: str,
    result_summary: str,
) -> None:
    """任務終結時 POST 至 ToukaProxy /v1/internal/dispatch/callback（ADR-0051）.

    靜默失敗：回呼失敗不影響主流程，僅記錄 warning。
    """
    url = f"{_PROXY_URL}/v1/internal/dispatch/callback"
    payload = {
        "task_id": task_id,
        "status": status,
        "result_summary": result_summary,
    }
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:  # 禁令 22
            resp = await client.post(
                url,
                json=payload,
                headers={"X-Callback-Token": callback_token},
                timeout=10.0,
            )
            resp.raise_for_status()
            logger.info("Dispatch callback notified: task=%s status=%s", task_id, status)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Dispatch callback notification failed (non-critical): %s", exc)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


async def dispatch_handshake(
    dispatch_token: str,
    caller_name: str,
    caller_version: str = "",
    ctx: MCPContext | None = None,
) -> str:
    """Exchange DISPATCH_TOKEN for a short-lived session token (ADR-0050).

    Must be called before other tools. Subsequent calls use the returned session_token.

    Args:
        dispatch_token: The pre-shared DISPATCH_TOKEN secret.
        caller_name: Name of the calling tool or agent (audit trail only).
        caller_version: Version string (optional, audit trail only).
        ctx: MCP request context injected by the server.

    Returns:
        Success message containing the session token, or Unauthorized error.
    """
    caller_info = {"name": caller_name, "version": caller_version}
    transport, protocol_version = _request_facets(ctx)

    # Internal path: DISPATCH_TOKEN（主金鑰，不離開 Touka 邊界）
    if _verify_dispatch_token(dispatch_token):
        session_token = _session_mgr.create_session(
            caller_info,
            transport=transport,
        )
        _telemetry.record_handshake(
            caller_type="internal",
            transport=transport,
            protocol_version=protocol_version,
        )
        logger.info(
            "Dispatch handshake completed for caller=%s version=%s",
            caller_name,
            caller_version,
        )
        return f"✅ Handshake successful. Session token: {session_token}"

    # External path: mission_token（一次性，外部 AI 專用）
    credential = _session_mgr.consume_mission_credential(dispatch_token)
    if credential is not None:
        session_token = _session_mgr.create_external_session(
            credential.mission_id,
            caller_info,
            generation=credential.generation,
            transport=transport,
        )
        _telemetry.record_handshake(
            caller_type="external",
            transport=transport,
            protocol_version=protocol_version,
        )
        logger.info(
            "External dispatch handshake completed for caller=%s mission_id=%s",
            caller_name,
            credential.mission_id,
        )
        return f"✅ Handshake successful. Session token: {session_token}"

    return "Error: Unauthorized. Invalid or missing DISPATCH_TOKEN."


async def dispatch_initialize_mission(
    session_token: str,
    goal: str,
    tasks: list[dict[str, Any]] | None = None,
    auto_invoke: str | None = None,
    ctx: MCPContext | None = None,
) -> str:
    """Initialize a new mission board for DispatchAgent.

    Args:
        session_token: Session token obtained from dispatch_handshake.
        goal: Mission goal description.
        tasks: Initial task list.
        auto_invoke: Optional executor key (e.g. "claude-code"); if set, ToukaDispatch
            auto-launches the external AI via dispatch_hooks.json (ADR-0067).
        ctx: MCP request context injected by the server.
    """
    session = _validate_session_for_request(
        session_token,
        "dispatch_initialize_mission",
        ctx,
    )
    if session is None:
        return (
            "Error: Unauthorized. Invalid or expired session token. Call dispatch_handshake first."
        )
    identity = _get_dispatch_identity()
    manager = MissionManager()
    try:
        mission = await manager.initialize_mission(
            identity=identity,
            goal=goal,
            initial_tasks=tasks,
        )
        # 同一把鎖內升 generation 並撤銷舊 credential/session（ADR-0201）。
        mission_token = _session_mgr.rotate_mission_token(mission.mission_id)
        invoke_note = ""
        if auto_invoke:
            invoke_note = "\n" + _invoke_external_ai(
                executor=auto_invoke,
                mission_id=mission.mission_id,
                mission_token=mission_token,
            )
        return (
            f"✅ Mission Initialized: '{goal}' with {len(mission.tasks)} tasks.\n"
            f"Mission ID: {mission.mission_id}\n"
            f"Mission Token: {mission_token}{invoke_note}"
        )
    except PermissionError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception(
            "Error initializing mission for caller_type=%s",
            session.caller_type,
        )
        return f"Error: {e}"


async def dispatch_update_task(
    session_token: str,
    task_id: str,
    status: str,
    artifact_ref: str | None = None,
    artifact_refs: list[str] | None = None,
    test_log: str | None = None,
    blocker_detail: str | None = None,
    ctx: MCPContext | None = None,
) -> str:
    """Update task status on the DispatchAgent mission board.

    Args:
        session_token: Session token obtained from dispatch_handshake.
        task_id: Task ID to update.
        status: New status (TODO, DOING, DONE, FAILED, BLOCKED).
        artifact_ref: Single artifact relative path.
        artifact_refs: Multiple artifact relative paths.
        test_log: Test result log.
        blocker_detail: Required when status=BLOCKED.
        ctx: MCP request context injected by the server.
    """
    session = _validate_session_for_request(
        session_token,
        "dispatch_update_task",
        ctx,
    )
    if session is None:
        return (
            "Error: Unauthorized. Invalid or expired session token. Call dispatch_handshake first."
        )
    try:
        task_status = TaskStatus(status.upper())
    except ValueError:
        return f"Error: Invalid status '{status}'. Valid: TODO, DOING, DONE, FAILED, BLOCKED."
    identity = _get_dispatch_identity()
    manager = MissionManager()

    # Board 完整性檢查（ADR-0064 A3）：外部 session 只能寫入「被指派且仍為當前」的
    # mission，與 dispatch_get_board 對齊——防止陳舊外部 session（clear_mission_tokens
    # 不清 _sessions，故 session 仍在 TTL 內有效）變更已被替換掉的 mission 的 FSM。
    if session.is_external and session.bound_mission_id:
        current = await manager.get_current_mission(identity)
        if current is None or current.mission_id != session.bound_mission_id:
            return (
                "Error: Your assigned mission is no longer active."
                " The mission board has changed since your session was created."
            )

    # #11 fix: external-AI task text must pass the H3 content guard (truncate +
    # detect prompt-injection) before it reaches the supervising agent's LLM
    # context (board render + callback). Mirrors ToukaMemory's save_memory (H3).
    if test_log:
        test_log, _flagged = sanitize_memory_content(test_log)
        if _flagged:
            logger.warning("⚠️ [Dispatch] test_log sanitized by ContentGuard (task=%s).", task_id)
    if blocker_detail:
        blocker_detail, _flagged = sanitize_memory_content(blocker_detail)
        if _flagged:
            logger.warning(
                "⚠️ [Dispatch] blocker_detail sanitized by ContentGuard (task=%s).", task_id
            )

    try:
        await manager.update_task(
            identity=identity,
            task_id=task_id,
            status=task_status,
            artifact_ref=artifact_ref,
            artifact_refs=artifact_refs,
            test_log=test_log,
            blocker_detail=blocker_detail,
        )
    except PermissionError as e:
        return f"Error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception(
            "Error updating task %s for caller_type=%s",
            task_id,
            session.caller_type,
        )
        return f"Error: {e}"
    else:
        # ADR-0051：任務終結時觸發 ToukaProxy 回呼，解除 Alice 的等待
        if task_status in (TaskStatus.DONE, TaskStatus.FAILED):
            try:
                cb_identity = _get_dispatch_identity()
                cb_manager = MissionManager()
                mission = await cb_manager.get_current_mission(cb_identity)
                if mission:
                    callbacks: dict[str, str] = mission.metadata.get("dispatch_callbacks", {})
                    cb_token = callbacks.get(task_id)
                    if cb_token:
                        result_summary = test_log or ""
                        await _notify_touka_proxy(
                            cb_token, task_id, task_status.value, result_summary
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Callback check failed (non-critical): %s", exc)
        return f"✅ Task '{task_id}' updated to {status}."


async def dispatch_get_board(
    session_token: str,
    ctx: MCPContext | None = None,
) -> str:
    """Get the DispatchAgent mission board.

    Args:
        session_token: Session token obtained from dispatch_handshake.
        ctx: MCP request context injected by the server.
    """
    session = _validate_session_for_request(
        session_token,
        "dispatch_get_board",
        ctx,
    )
    if session is None:
        return (
            "Error: Unauthorized. Invalid or expired session token. Call dispatch_handshake first."
        )
    identity = _get_dispatch_identity()
    manager = MissionManager()
    try:
        mission = await manager.get_current_mission(identity)
        if not mission:
            return "No active mission board found. Use dispatch_initialize_mission to start one."

        # Board 完整性檢查：外部 session 只能存取被指派的 mission（ADR-0064）
        if (
            session.is_external
            and session.bound_mission_id
            and mission.mission_id != session.bound_mission_id
        ):
            return (
                "Error: Your assigned mission is no longer active."
                " The mission board has changed since your session was created."
            )

        res = [
            f"## 📋 Mission: {mission.goal}",
            f"**Overall Status:** {mission.status}",
            "\n**Tasks:**",
        ]
        for t in mission.tasks:
            icon = "✅" if t.status == "DONE" else "⏳" if t.status == "DOING" else "⬜"
            dep = f" (Needs: {', '.join(t.dependencies)})" if t.dependencies else ""
            art_list = []
            if t.artifact_ref:
                art_list.append(f"Art: {t.artifact_ref}")
            if getattr(t, "artifact_refs", None):
                art_list.append(f"Arts: {', '.join(t.artifact_refs)}")
            art_str = f" [{'; '.join(art_list)}]" if art_list else ""
            verification = (
                f" (Verify: {t.verification_cmd})" if getattr(t, "verification_cmd", None) else ""
            )
            t_log = f"\n   *Test Log:* {t.test_log}" if getattr(t, "test_log", None) else ""
            b_detail = (
                f"\n   *Blocker:* {t.blocker_detail}" if getattr(t, "blocker_detail", None) else ""
            )
            res.append(
                f"{icon} **{t.id}**: {t.description}{dep}{art_str}{verification}{t_log}{b_detail}"
            )
        return "\n".join(res)
    except PermissionError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception(
            "Error getting board for caller_type=%s",
            session.caller_type,
        )
        return f"Error: {e}"


_DISPATCH_TOOLS = (
    dispatch_handshake,
    dispatch_initialize_mission,
    dispatch_update_task,
    dispatch_get_board,
)


async def _require_modern_protocol(
    ctx: ServerRequestContext[Any, Any],
    call_next: CallNext,
) -> HandlerResult:
    """SDK public middleware 在握手與 handler 前拒絕舊協定，不重寫 wire。"""
    if ctx.method == "initialize" or ctx.protocol_version != types.LATEST_PROTOCOL_VERSION:
        raise MCPError(-32020, "ToukaDispatch requires MCP 2026-07-28.")
    return await call_next(ctx)


def _create_modern_server() -> MCPServer[Any]:
    """建立只接受 modern request 的 server，保留 SDK 的 SSE 生命週期。"""
    modern = MCPServer(
        "ToukaDispatch",
        version="0.1.0",
        lifespan=_modern_lifespan,
        middleware=[_require_modern_protocol],
    )
    for tool in _DISPATCH_TOOLS:
        modern.add_tool(tool)
    return modern


class DispatchTransportTelemetryMiddleware:
    """記錄匿名 transport 入口使用量；不解析或保存 MCP body。"""

    def __init__(
        self,
        app: ASGIApp,
        telemetry: DispatchTelemetry,
    ) -> None:
        self._app = app
        self._telemetry = telemetry

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """記錄入口後不改寫 scope/body，直接轉交下游 ASGI app。"""
        if scope["type"] == "http":
            method = scope.get("method")
            path = scope.get("path")
            if method == "POST" and path == "/mcp":
                self._telemetry.record_anonymous_connection("modern")
        await self._app(scope, receive, send)


async def _health(_request: Request) -> JSONResponse:
    """不含識別資訊的 readiness 與 modern telemetry。"""
    return JSONResponse(
        {
            "status": "ok",
            "primary_endpoint": "/mcp",
            "telemetry": _telemetry.snapshot(),
        }
    )


class _DispatchAppLifespan:
    """將 modern session manager lifespan 接到合併後的 Starlette app。"""

    def __init__(self, modern_app: Starlette) -> None:
        self._modern_app = modern_app

    @asynccontextmanager
    async def __call__(self, _app: Starlette) -> AsyncIterator[None]:
        async with self._modern_app.router.lifespan_context(self._modern_app):
            yield


def create_dispatch_app() -> Starlette:
    """只公開 modern `/mcp` 與不含識別資訊的 health。"""
    modern = _create_modern_server()
    host = _bind_host()
    modern_app = modern.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        host=host,
    )
    app = Starlette(
        routes=[
            Route("/health", endpoint=_health, methods=["GET"]),
            *modern_app.routes,
        ],
        middleware=[
            Middleware(
                DispatchTransportTelemetryMiddleware,
                telemetry=_telemetry,
            )
        ],
        lifespan=_DispatchAppLifespan(modern_app),
    )
    app.state.modern_mcp = modern
    return app


# Import-time introspection；production app 仍由 factory 取全新 server。
mcp = _create_modern_server()


def main() -> None:
    """啟動 Dispatch modern HTTP server。"""
    uvicorn.run(
        create_dispatch_app(),
        host=_bind_host(),
        port=_DISPATCH_PORT,
    )


if __name__ == "__main__":
    main()
