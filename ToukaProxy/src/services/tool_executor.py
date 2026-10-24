"""工具執行引擎 (ToolExecutor) 模組.

負責 Touka 系統中所有工具的尋址、授權驗證、非同步執行、SSE 事件推送
以及針對終端指令的自動修復邏輯 (Autonomy). 這是 ReAct 循環的物理執行層.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from src.models.advertised_tools import AdvertisedTools
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext
from src.protocols.manager import McpToolArgumentRefusal, McpToolCatalogRefusal, ToolDisabledError
from src.protocols.mcp.canonical import (
    McpCompleteOutcome,
    McpErrorKind,
    McpOperationException,
)
from src.protocols.mcp.error_mapping import map_mcp_error
from src.protocols.mcp.interaction import (
    McpInteractionSink,
    bind_mcp_interaction,
)
from src.protocols.mcp.mrtr import McpMrtrAuthorizationError
from src.services import approval_flow, mcp_vault_jail, tool_errors
from src.services.auto_reviewer import CODE_HEADLESS_TERMINAL, ReviewRequest, auto_reviewer
from src.services.autonomy import autonomy_service
from src.services.isolation.policy import policy_level, reviewer_level
from src.services.permission_service import permission_service
from src.services.soul_service import soul_service
from src.tools.builtin import BUILTIN_EXECUTION_BINDINGS, LEGACY_ADVERTISED_SUCCESSORS
from src.tools.contracts import PUBLIC_TOOL_CONTRACTS, validate_native_arguments
from src.tools.execution_context import APPROVED_BY_AUTO_REVIEWER, build_execution_context_kwargs
from src.tools.execution_policy import ToolExecutionClass, classify_registered_tool
from src.utils.async_db import session_service
from src.utils.command_safety import effective_shell
from src.utils.hard_floor import floor_tool_action
from src.utils.mcp_request_identity import mcp_request_context as build_mcp_request_context
from src.utils.mcp_request_identity import user_scoped_mcp_allowed
from src.utils.presence import is_human_present
from src.utils.progress_events import repair_event
from src.utils.time_utils import (
    get_current_epoch,
    get_monotonic_ms,
    get_taipei_time_info,
    inject_time_to_result,
)
from src.utils.tool_events import (
    ToolCallEvent,
    tool_error_event,
    tool_start_event,
)
from src.utils.tool_usage import record_tool_executed, record_tool_succeeded, record_tool_usage

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from src.services.auto_reviewer import ReviewOutcome

logger = logging.getLogger(__name__)

# 空參數即拒（validation_error）的工具名單——這些工具無參數呼叫必然無意義。
# 模組級常數：供測試與擴充引用（Tier 3 統一動詞與專屬寫入/發送一併納入）。
CRITICAL_TOOLS = frozenset(
    {
        "memory_tools",
        "vault_tools",
        "project_tools",
        "update_my_soul",
        "connect_mcp_server",
        # Tier 3：統一檔案動詞（皆至少需 scope）＋專屬寫入／發送
        "read",
        "list",
        "find",
        "grep",
        "write",
        "edit",
        "insert",
        "undo",
        "delete",
        "edit_soul",
        "send_message",
        # 非同步指令執行（ADR-0119 遞延項）：皆有必填參數
        "bash",
        "execute_command",
        "get_command_result",
        "stop_command",  # P2b：長指令 during 監控（execution_id 必填）
        # 演化管線 4→2：皆有必填參數
        "evolution_define",
        "evolution_implement_and_verify",
    }
)

# 終端自我修復（結果裡的 [EXIT_CODE: N]）：現在廣告的 execute_command、背景指令的結果
# get_command_result，以及舊名稱。只附提示文字給模型，下一次執行仍照常過政策。
TERMINAL_REPAIR_TOOLS = frozenset(
    {"execute_command", "get_command_result", "bash", "terminal__bash", "execute_terminal"}
)
# get_command_result 對同一個 execution_id 每次都回同一份結果：只計第一次。記住已計過的
# (session, execution_id)，超過上限淘汰最舊的（行程記憶體、不落地）。
MAX_TRACKED_REPAIR_RESULTS = 1024
_counted_repair_results: OrderedDict[tuple[str, str], None] = OrderedDict()


def _first_repair_report(session_id: str, name: str, args: dict[str, Any]) -> bool:
    """這份終端結果是否第一次被計入修復次數（只有 get_command_result 會重複回同一份）。"""
    execution_id = args.get("execution_id") if name == "get_command_result" else None
    if not isinstance(execution_id, str):
        return True
    key = (session_id, execution_id)
    if key in _counted_repair_results:
        return False
    _counted_repair_results[key] = None
    if len(_counted_repair_results) > MAX_TRACKED_REPAIR_RESULTS:
        _counted_repair_results.popitem(last=False)
    return True


# ADR-0148：自帶審批 UI 的工具——本體有比 broker 更豐富的審批卡（如
# request_skill_install 的 EvolutionModal 收 env 輸入）。這類 CONFIRM 不經 broker
# 雙重審批（否則 Manual/Auto 下上下各一張卡＝混淆／逾時空等）；由工具本體自行
# gate（互動→自帶卡、headless→fail-closed，據 _interactive kwarg）。
#
# ADR-0149：演化部署的 request_mcp_creation 自帶 skill_creation_approval_request 審批卡
# （呈現 source_code 供審），可被模型獨立調用（雙軌 shim），本體 headless fail-closed。
# evolution_implement_and_verify 原本也在本名單；ADR-0254 起改為一般分類（建 venv、
# pip、匯入代理人寫的 server.py 先過一般審批卡，無人在場由 executor 拒絕），
# submit_for_deploy=True 時仍委派 request_mcp_creation 的部署卡。名單只在
# tools/builtin/__init__.py 的 BUILTIN_EXECUTION_BINDINGS 註冊（ADR-0229）。
# Compatibility export for existing callers; the executor uses handler bindings.
_SELF_APPROVING_TOOLS = frozenset(
    name
    for name, binding in BUILTIN_EXECUTION_BINDINGS.items()
    if binding.kind == ToolExecutionClass.SELF_APPROVING
)


def _unknown_tool_error(name: str) -> str:
    """任何 registry／MCP 皆查無此工具 → resource_not_found（與政策拒絕明確分流）."""
    return tool_errors.build_tool_error(
        tool_errors.RESOURCE_NOT_FOUND,
        f"Unknown tool '{name}' — it is not registered in any tool registry.",
        suggestion="Only call tools from the provided tools list.",
    )


def _render_mcp_complete(outcome: McpCompleteOutcome) -> str:
    """Canonical complete → 模型可讀文字；projection 只發生在 ToolExecutor 最末端。"""
    texts = [
        item.get("text", "")
        for item in outcome.content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    parts = [text for text in texts if text]
    if outcome.structured_content_present or outcome.structured_content is not None:
        parts.append(json.dumps(outcome.structured_content, ensure_ascii=False))
    if parts:
        return "\n".join(parts)
    return json.dumps(outcome.content, ensure_ascii=False)


def _mcp_error_result(
    *,
    message: str,
    source_protocol_version: str,
    kind: McpErrorKind,
) -> str:
    """Canonical MCP error → 模型面結構化錯誤；ErrorCode 對映只走單一 mapper。"""
    mapped = map_mcp_error(
        kind,
        message,
        source_protocol_version=source_protocol_version,
    )
    return tool_errors.build_tool_error(
        mapped.tool_error_class,
        f"MCP operation failed: {mapped.message}",
        suggestion=(
            "Correct the tool input if possible; otherwise report the upstream "
            "failure to the user instead of treating it as success."
        ),
        touka_error_code=mapped.touka_error_code,
        mcp_error_kind=mapped.kind.value,
    )


class ToolExecutor:
    """統一工具執行中樞.

    協調不同來源的工具 (Builtin, Memory, Projects, MCP), 並在執行前強制
    通過 Gate 2 安全攔截與身分主權鎖定.
    """

    def __init__(
        self,
        protocol_manager: Any,
        tool_registry: dict[str, Any],
        memory_tool_registry: dict[str, Any],
        plugin_registry: dict[str, Any],
        *,
        enforce_advertised: bool = False,
    ) -> None:
        """初始化執行器並掛載工具註冊表.

        Args:
            protocol_manager: MCP 協議管理者.
            tool_registry: 核心工具註冊表.
            memory_tool_registry: 記憶工具註冊表.
            plugin_registry: 外掛工具註冊表.
            enforce_advertised: 只執行這一輪列給模型的工具（模型迴圈必開；人按的
                ``/debug/execute`` 關）；建立後不能改.
        """
        self.protocol_manager = protocol_manager
        self.tool_registry = tool_registry
        self.memory_tool_registry = memory_tool_registry
        self.plugin_registry = plugin_registry
        self._enforce_advertised = enforce_advertised

    @property
    def enforce_advertised(self) -> bool:
        """是否核對這一輪列給模型的工具（唯讀）."""
        return self._enforce_advertised

    def _offer_denial(
        self, name: str, ctx: Any, handler: str | None, server: str | None, tool: str | None
    ) -> str | None:
        """核對這一輪的清單；放行回 None，否則回結構化錯誤（不進 PaC／審查員／審批卡）."""
        advertised = ctx.get("advertised_tools")
        available = isinstance(advertised, AdvertisedTools)
        if handler is None:  # 不是 builtin／memory／plugin：MCP 核對 (伺服器, 裸工具名)
            if available and server and advertised.permits_mcp(server, tool or ""):
                return None
            registry = getattr(self.protocol_manager, "registry", None)
            known = bool(server) and (
                (registry is not None and registry.get_server(server) is not None)
                or (available and advertised.has_server(server))
            )
            if not known:
                return _unknown_tool_error(name)
        if not available:  # 接線問題：一律擋下，和「沒列出」分開回報
            logger.error("Advertised tool list unavailable; blocked %s", name)
            return tool_errors.build_tool_error(
                tool_errors.INTERNAL_ERROR,
                "This turn's tool list is unavailable; tool calls are blocked.",
                suggestion="Report this to the user; do not retry.",
                denied_tool=name,
                reason="advertised_tools_unavailable",
            )
        if handler is not None and advertised.permits_native(name, LEGACY_ADVERTISED_SUCCESSORS):
            return None
        return tool_errors.build_tool_error(
            tool_errors.PERMISSION_DENIED,
            f"Tool '{name}' was not offered to you in this turn.",
            suggestion="Only call tools from this turn's tool list; tools of a server connected "
            "via connect_mcp_server become available on the next turn.",
            denied_tool=name,
            reason="not_advertised",
        )

    def _user_scope_denial(self, name: str, ctx: dict[str, Any], server: str | None) -> str | None:
        """使用者各自授權的 MCP 只給人在場、和靈魂的前景對話（ToolLayer 之外的第二層防線）."""
        registry = getattr(self.protocol_manager, "registry", None)
        config = registry.get_server(server) if server and registry is not None else None
        if getattr(config, "auth_mode", None) != "oauth_user" or user_scoped_mcp_allowed(ctx):
            return None
        return tool_errors.build_tool_error(
            tool_errors.PERMISSION_DENIED,
            f"Tool '{name}' uses the user's own authorization, which only works in a foreground "
            "conversation with a soul while the user is present.",
            suggestion="Do not retry this tool in this run.",
            denied_tool=name,
            reason="user_scoped_mcp_unavailable",
        )

    async def _await_human_approval(
        self,
        *,
        name: str,
        args: dict[str, Any],
        call_id: str,
        agent_id: str,
        session_id: str | None,
        tool_action: str | None,
        queue: asyncio.Queue[dict[str, Any] | None],
        review_reason: str | None = None,
        human_only: bool = False,
    ) -> str:
        """統一審批 broker（H2）：委派 ``approval_flow.await_human_approval``。

        回 "approved"|"rejected"|"timeout"|"queue_full"。風險章在此依本模組的政策 SSOT 求出。
        """
        risk = "dangerous"
        if permission_service.policies:
            risk = permission_service.policies.tool_risk.class_for(name, tool_action)
        return await approval_flow.await_human_approval(
            name=name,
            args=args,
            call_id=call_id,
            agent_id=agent_id,
            session_id=session_id,
            risk=risk,
            emit=queue.put,
            review_reason=review_reason,
            human_only=human_only,
        )

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        request_id: str,
        ctx: dict[str, Any],
        call_id: str | None = None,
        actually_used_tools: set[str] | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        """執行單一工具並產生 SSE 事件流.

        Args:
            name: 工具名稱.
            args: 調用參數.
            request_id: 請求追蹤 ID.
            ctx: 執行上下文.
            call_id: 調用 ID.
            actually_used_tools: 工具追蹤.

        Yields:
            dict[str, Any]: SSE 事件.
        """
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        task = asyncio.create_task(
            self.execute_to_queue(name, args, request_id, ctx, queue, call_id, actually_used_tools)
        )

        try:
            while True:
                event = await queue.get()
                if event is None:  # Sentinel
                    break
                yield event
        finally:
            await task

    async def execute_to_queue(
        self,
        name: str,
        args: dict[str, Any],
        request_id: str,
        ctx: dict[str, Any],
        queue: asyncio.Queue[dict[str, Any] | None],
        call_id: str | None = None,
        actually_used_tools: set[str] | None = None,
    ) -> str:
        """執行工具並推送到隊列.

        Args:
            name: 工具名稱.
            args: 參數.
            request_id: 請求 ID.
            ctx: 上下文.
            queue: 隊列.
            call_id: 調用 ID.
            actually_used_tools: 工具追蹤.

        Returns:
            str: 最終回傳文字.
        """
        record_tool_usage(actually_used_tools, name, args)

        call_id = call_id or f"call_{int(get_current_epoch() * 1000)}"

        # 提取身分實體 (Identity SSOT)
        identity: AgentIdentity | None = ctx.get("identity")
        if not isinstance(identity, AgentIdentity):
            # 門禁失敗：若無身分物件，嚴禁執行
            logger.error("🚫 Identity Missing in ToolExecutor for request %s", request_id)
            result_text = tool_errors.build_tool_error(
                tool_errors.INTERNAL_ERROR,
                "Tool execution requires a verified agent identity, but none was provided.",
                suggestion="Report this to the user; the session context is missing identity.",
            )
            await queue.put(
                ToolCallEvent(
                    call_id=call_id,
                    tool_name=name,
                    arguments=args,
                    status="error",
                    error_class=tool_errors.extract_error_class(result_text),
                    result_text=result_text,
                ).to_result_sse()
            )
            await queue.put(None)
            return result_text

        agent_id = identity.agent_id
        session_id = ctx.get("session_id")
        model = ctx.get("model")

        # Resolve the same native branch used below (builtin wins duplicates).
        # Advertised schemas and PaC risk levels never substitute for role grants.
        if name in self.tool_registry:
            handler_source, resolved_handler = "builtin", self.tool_registry[name]
        elif name in self.memory_tool_registry:
            handler_source, resolved_handler = "memory", self.memory_tool_registry[name]
        elif name in self.plugin_registry:
            handler_source, resolved_handler = "plugin", self.plugin_registry[name]
        else:
            handler_source, resolved_handler = None, None
        is_native = handler_source in {"builtin", "memory"}
        if is_native and not permission_service.resolve_grants(identity.agent_role).tools.allows(
            name
        ):
            result_text = tool_errors.build_tool_error(
                tool_errors.PERMISSION_DENIED,
                "Native tool is not granted to the verified role.",
                suggestion="Use a tool granted to this identity.",
                denied_tool=name,
                reason="native_tool_rbac",
            )
            await queue.put(
                ToolCallEvent(
                    call_id=call_id,
                    tool_name=name,
                    arguments={},
                    status="error",
                    error_class=tool_errors.PERMISSION_DENIED,
                    result_text=result_text,
                ).to_result_sse()
            )
            await queue.put(None)
            return result_text

        # 0. 執行前身份與政策核對 (Gate 2 - Execution Layer Guard)
        mcp_server = None
        mcp_tool_name = None
        if (
            name not in self.tool_registry
            and name not in self.memory_tool_registry
            and name not in self.plugin_registry
            and self.protocol_manager
            and hasattr(self.protocol_manager, "resolve_tool")
        ):
            mcp_server, mcp_tool_name = self.protocol_manager.resolve_tool(name)

        # 這一輪清單核對（模型迴圈）：角色權限之後、軸 B／PaC／審查員／審批卡之前；使用者各自授權
        # 的 MCP 另外再核一次在場條件（ADR-0281 T1）
        offer_denial = (
            (
                self._offer_denial(name, ctx, handler_source, mcp_server, mcp_tool_name)
                or self._user_scope_denial(name, ctx, mcp_server)
            )
            if self._enforce_advertised
            else None
        )
        if offer_denial is not None:
            await queue.put(
                ToolCallEvent(
                    call_id=call_id,
                    tool_name=name,
                    arguments={},
                    status="error",
                    error_class=tool_errors.extract_error_class(offer_denial),
                    result_text=offer_denial,
                ).to_result_sse()
            )
            await queue.put(None)
            return offer_denial

        # Gate 2a（ADR-0195 軸 B）：MCP 工具面 RBAC。模型迴圈裡廣告面（這一輪的清單）也是
        # 執行條件（上面的核對），但列出不等於有權限：角色授權仍在這裡獨立核對，執行面
        # 才是執法。
        mcp_rbac_denied = bool(mcp_server) and not permission_service.is_mcp_tool_allowed(
            mcp_server, mcp_tool_name or name, identity
        )

        execution_class = (
            classify_registered_tool(name, resolved_handler, BUILTIN_EXECUTION_BINDINGS)
            if handler_source == "builtin"
            else ToolExecutionClass.ORDINARY
        )
        if execution_class == ToolExecutionClass.MISMATCH:
            result_text = tool_errors.build_tool_error(
                tool_errors.PERMISSION_DENIED,
                "Registered handler does not match its trusted execution classification.",
                suggestion="Report the registration mismatch; do not retry this call.",
                denied_tool=name,
                reason="execution_classification_mismatch",
            )
            await queue.put(
                ToolCallEvent(
                    call_id=call_id,
                    tool_name=name,
                    arguments={},
                    status="error",
                    error_class=tool_errors.PERMISSION_DENIED,
                    result_text=result_text,
                ).to_result_sse()
            )
            await queue.put(None)
            return result_text
        is_terminal = execution_class == ToolExecutionClass.TERMINAL

        # 構建 PaC 上下文（聚合工具抽動詞進 tool_action——審計只記動詞、不記整包 args）
        action_param = permission_service.get_tool_action_param(name)
        policy_action = args.get(action_param) if action_param else None
        if is_native and name in PUBLIC_TOOL_CONTRACTS and not isinstance(policy_action, str):
            # Malformed model actions use the existing conservative unknown risk;
            # the public validator then returns a sanitized validation error.
            policy_action = None
        # 隔離層級只由身分、ctx／session 的專案與 monitor 決定（args 只用來算同意 subject）
        isolation = await policy_level(
            identity, ctx, session_id, call_id, tool=name, args=args,
            builtin=handler_source == "builtin",
        )
        policy_context = PolicyContext(
            actor=identity,
            resource=args.get("command", name) if is_terminal else name,
            action="terminal_execute" if is_terminal else "tool_call",
            environment={
                "auth_level": ctx.get("auth_level", "Manual"),
                "session_id": session_id,
                "mcp_server": mcp_server,
                "tool_action": policy_action,
                "floor_action": floor_tool_action(name, args),  # F5 動詞，不看設定檔
                "isolation": isolation.level,
                "human_present": is_human_present(ctx),
            },
        )

        # 執行策略評估
        decision = permission_service.evaluate_policy(policy_context)

        if mcp_rbac_denied or decision.effect == "DENY":
            deny_reason = "mcp_tool_rbac" if mcp_rbac_denied else (decision.reason or "policy")
            result_text = tool_errors.build_tool_error(
                tool_errors.PERMISSION_DENIED,
                f"Tool '{name}' was denied by policy for role '{identity.display_role}'.",
                suggestion=(
                    "Do not retry this tool; explain the restriction to the user "
                    "or use an allowed alternative."
                ),
                denied_tool=name,
                reason=deny_reason,
            )
            logger.warning("🛡️ Security Block: %s -> %s | Reason: %s", agent_id, name, deny_reason)
            await queue.put(
                ToolCallEvent(
                    call_id=call_id,
                    tool_name=name,
                    arguments=args,
                    status="error",
                    error_class=tool_errors.extract_error_class(result_text),
                    result_text=result_text,
                ).to_result_sse()
            )
            await queue.put(
                {
                    "type": "log",
                    "content": f"🚫 攔截未授權調用: {name}\n",
                    "result_text": result_text,
                }
            )
            await queue.put(None)
            return result_text

        # H2（hitl-unification）：CONFIRM 的統一 HITL 處置。
        # - 互動會話（resumable_stream＝UI 聊天）：審批卡走**當前聊天 SSE**（統一
        #   broker），人類 approve/reject 後才續行——「未決不執行」的 H1 錨永遠成立。
        # - headless（heartbeat／派工／SNS 等）：無人可問 → fail-closed 拒絕（H1 行為
        #   保持）；終端例外——維持 terminal.py 內部遺留通道（ToukaUI 訂閱者或
        #   ui_unavailable fail-closed），互動會話則同走 broker 並以 _hitl_preapproved
        #   告知 terminal 跳過自帶審批。
        # HUMAN_ONLY（硬地板，ADR-0246）：同一張人類審批卡；無人在場一律拒絕，終端也不
        # 落到遺留通道；自帶審批工具也先過這張卡（審查員與模式都不能代替人）。
        # 在場判斷（U2c-2）：子代理、派工、心跳都算無人在場——子代理的卡只會被子 loop 吞掉。
        # REVIEW（Auto 審查員，U2c-3）：allow 以可信 kwarg _approved_by 交給工具（terminal 只
        # 重驗地板、不再審）；escalate 跳同一張人類卡、卡上帶審查理由（無人在場拒絕，終端也
        # 不落到遺留通道）；deny 直接拒絕。審查員的 allow 只滿足 REVIEW，不能代替人核准。
        hitl_preapproved = False
        card_complete = False  # 審批卡沒截斷任何參數（IDE「由人核准」標記的前提）
        approved_by: str | None = None
        human_only = decision.effect == "HUMAN_ONLY"
        needs_human = human_only or (
            decision.effect == "CONFIRM" and execution_class != ToolExecutionClass.SELF_APPROVING
        )
        interactive = is_human_present(ctx)
        review: ReviewOutcome | None = None
        denied_text: str | None = None
        if decision.effect == "REVIEW":
            review = await auto_reviewer.review(
                identity,
                ReviewRequest(
                    tool_name=name,
                    arguments=args,
                    is_command=is_terminal,
                    # 和 terminal._execute_core 共用：非 Windows 一律 bash
                    shell_kind=effective_shell(args.get("env")) if is_terminal else None,
                    human_present=interactive,
                    user_intent=ctx.get("user_intent"),
                    session_id=session_id,
                    conversation_key=session_id or request_id,
                    isolation=reviewer_level(name, isolation, builtin=handler_source == "builtin"),
                ),
            )
            if review.verdict == "allow":
                approved_by = APPROVED_BY_AUTO_REVIEWER
            elif review.verdict == "deny":
                denied_text = approval_flow.auto_review_denied_error(name, review.reason)
            else:
                needs_human = True
        if needs_human:
            if interactive:
                verdict = await self._await_human_approval(
                    name=name,
                    args=args,
                    call_id=call_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    tool_action=policy_context.environment.get("tool_action"),
                    queue=queue,
                    review_reason=review.reason if review is not None else None,
                    human_only=human_only,
                )
                if verdict != "approved":
                    result_text = approval_flow.approval_denied_error(name, verdict)
                    logger.warning("🖐️ HITL %s: %s -> %s", verdict, agent_id, name)
                    await queue.put(
                        ToolCallEvent(
                            call_id=call_id,
                            tool_name=name,
                            arguments=args,
                            status="error",
                            error_class=tool_errors.extract_error_class(result_text),
                            result_text=result_text,
                        ).to_result_sse()
                    )
                    await queue.put(None)
                    return result_text
                hitl_preapproved = True
                card_complete = not approval_flow.approval_preview_truncated(args)
            elif human_only:
                # H1 錨（headless fail-closed）：HUMAN_ONLY 連終端也直接拒絕
                denied_text = approval_flow.human_only_unavailable_error(name, decision.reason)
            elif review is not None:
                denied_text = approval_flow.auto_review_no_human_error(
                    name, review.reason, headless_terminal=review.code == CODE_HEADLESS_TERMINAL
                )
            elif not is_terminal:
                # 非終端工具無審批通道＝零執行（終端維持 terminal.py 遺留通道）
                denied_text = tool_errors.build_tool_error(
                    tool_errors.PERMISSION_DENIED,
                    f"Tool '{name}' requires human approval under auth level "
                    f"'{ctx.get('auth_level', 'Manual')}', and no approval channel is "
                    "available in this session.",
                    suggestion=(
                        "Do not retry this tool call; tell the user it needs approval — "
                        "they can switch the session to Auto or YOLO mode to auto-allow "
                        "more actions, or approve it in an interactive chat session."
                    ),
                    denied_tool=name,
                    reason="approval_required",
                )
        if denied_text is not None:
            logger.warning(
                "🖐️ HITL Hold (fail-closed): %s -> %s | Reason: %s",
                agent_id,
                name,
                review.code if review is not None else decision.reason,
            )
            await queue.put(
                ToolCallEvent(
                    call_id=call_id,
                    tool_name=name,
                    arguments=args,
                    status="error",
                    error_class=tool_errors.extract_error_class(denied_text),
                    result_text=denied_text,
                ).to_result_sse()
            )
            await queue.put(
                {
                    "type": "log",
                    "content": f"🖐️ 待審批攔截（fail-closed）: {name}\n",
                    "result_text": denied_text,
                }
            )
            await queue.put(None)
            return denied_text

        # Validate only selected native public calls after policy/approval, before
        # project fallback or trusted metadata injection can consume model values.
        if name in PUBLIC_TOOL_CONTRACTS and (
            name in self.tool_registry or name in self.memory_tool_registry
        ):
            args, validation_error = validate_native_arguments(name, args)
            if validation_error is not None:
                await queue.put(
                    ToolCallEvent(
                        call_id=call_id,
                        tool_name=name,
                        arguments={},
                        status="error",
                        error_class=tool_errors.VALIDATION_ERROR,
                        result_text=validation_error,
                    ).to_result_sse()
                )
                await queue.put(None)
                return validation_error

        # 1. 發送 tool_start 事件
        await queue.put(tool_start_event(call_id=call_id, tool=name, args=args))
        await queue.put({"type": "touka_status", "status": f"tool_use: {name}"})

        result_text = ""
        is_success = False
        duration_ms: float | None = None  # 僅在工具實際執行的路徑設值（早退不設）

        try:
            # --- [參數安全檢查]（名單見模組級 CRITICAL_TOOLS）---
            if (
                name in CRITICAL_TOOLS
                and (not args or len(args) == 0)
                and name != "connect_mcp_server"
            ):
                result_text = tool_errors.build_tool_error(
                    tool_errors.VALIDATION_ERROR,
                    f"Tool '{name}' was called with no arguments; "
                    "it requires a JSON object of parameters.",
                    suggestion="Retry with the tool's required parameters.",
                )
            else:
                # 取得執行參數與上下文鎖定
                is_toukasoul = soul_service.is_toukasoul(identity)

                # 嘗試自動補全 project_id (SSOT 輔助)
                project_id = ctx.get("project_id") or args.get("project_id")
                if not project_id and session_id:
                    session = await session_service.get_session_by_id(session_id)
                    if session:
                        project_id = session.get("project_id")

                async def _emit_event(event: dict[str, Any]) -> None:
                    """供工具把結構化事件（tool_stream / 進度）推到本次 SSE 佇列。"""
                    await queue.put(event)

                execution_context_kwargs = build_execution_context_kwargs(
                    request_id=request_id,
                    identity=identity,
                    session_id=session_id,
                    project_id=project_id,
                    model=model,
                    is_toukasoul=is_toukasoul,
                    auth_level=ctx.get("auth_level", "Manual"),
                    # H2：統一 broker 已核准（互動會話 CONFIRM）——終端據此跳過自帶
                    # 審批（遺留 ToukaUI 通道）；其他工具照例忽略本 kwarg。request_review
                    # 不採用：verification_cmd 逐條跳卡（B2，approval_flow.CommandApprover）。
                    hitl_preapproved=hitl_preapproved,
                    # U2c-3：Auto 審查員放行 REVIEW 的可信標記（模型自帶的同名參數會被覆寫）
                    approved_by=approved_by,
                    # ADR-0148：本 run 是否為互動會話（有人在場可回答審批）——自帶審批
                    # 的工具（request_skill_install）據此決定 headless fail-closed。
                    interactive=is_human_present(ctx),
                    # P2b/P2d：可選的事件 emit 通道（工具以 **kwargs 取用、不需要者忽略）
                    emit_event=_emit_event,
                    call_id=call_id,
                    # Phase 1（in-process 子代理）：父 PipelineContext——dispatch_subagent
                    # 據此衍生子 loop ctx（同慣例：不需要者忽略）
                    context=ctx,
                )
                call_kwargs = {
                    **args,
                    **execution_context_kwargs,
                    "identity": identity,
                }

                # --- [工具尋址與執行 - SSOT via ProtocolManager] ---
                # 真正交給 handler 才算「執行過」（T3）；前面任何一關擋下都不算
                record_tool_executed(actually_used_tools, name, args)
                t0_monotonic = get_monotonic_ms()  # 單調時鐘：量測純執行耗時
                if is_native:
                    res_data = await resolved_handler(**call_kwargs)
                    result_text = str(res_data)
                elif handler_source == "plugin":
                    func = resolved_handler
                    res_data = (
                        await func(**call_kwargs)
                        if asyncio.iscoroutinefunction(func)
                        else func(**call_kwargs)
                    )
                    result_text = str(res_data)
                elif self.protocol_manager and hasattr(self.protocol_manager, "resolve_tool"):
                    server_id, actual_tool = mcp_server, mcp_tool_name
                    if server_id:
                        args["agent_id"] = identity.agent_id
                        # vault-jailed 伺服器（如 mcp-docgen）：依身分把其根目錄參數覆寫成當下
                        # agent 自己的 vault（per-agent 隔離；模型給的根目錄被蓋掉）。
                        mcp_vault_jail.apply(
                            self.protocol_manager.registry, server_id, args, identity
                        )
                        human_present = is_human_present(ctx)
                        mcp_request_context = build_mcp_request_context(
                            ctx,
                            identity,
                            request_id=request_id,
                            installation_id=server_id,
                            policy_context=policy_context,
                            # U2c-7：只有人類卡片核准、且卡上參數沒被截斷；審查員與 YOLO 放行不帶
                            human_approved=hitl_preapproved and card_complete,
                        )
                        try:
                            interaction_scope = (
                                bind_mcp_interaction(
                                    McpInteractionSink(
                                        queue=queue,
                                        call_id=call_id,
                                        tool_name=name,
                                        agent_id=agent_id,
                                        session_id=session_id,
                                    )
                                )
                                if human_present
                                else nullcontext()
                            )
                            with interaction_scope:
                                res_data = await self.protocol_manager.call_tool_on_server(
                                    server_id,
                                    actual_tool,
                                    args,
                                    request_context=mcp_request_context,
                                )
                        except (ToolDisabledError, McpMrtrAuthorizationError) as disabled:
                            # 軸 A（ADR-0195）：供給面把這個工具關掉了。回結構化
                            # PERMISSION_DENIED 而非讓它掉進泛用例外處理——模型才分得出
                            # 「政策關閉」與「伺服器故障」，不會換個名字重試。
                            permission_revoked = isinstance(
                                disabled,
                                McpMrtrAuthorizationError,
                            )
                            result_text = tool_errors.build_tool_error(
                                tool_errors.PERMISSION_DENIED,
                                str(disabled),
                                suggestion=(
                                    "Authorization changed while waiting for human "
                                    "input. Do not retry without a new user request."
                                    if permission_revoked
                                    else "This tool is switched off for this server "
                                    "installation. Do not retry; use another tool "
                                    "or tell the user to re-enable it in settings."
                                ),
                                denied_tool=name,
                                reason=(
                                    "mcp_permission_revoked"
                                    if permission_revoked
                                    else "mcp_tool_disabled"
                                ),
                            )
                            logger.warning("🛡️ Install policy block: %s -> %s", agent_id, name)
                        except McpToolArgumentRefusal as refusal:
                            # 參數放進標頭後超過上限：模型縮短參數即可重試，不是上游故障。
                            result_text = tool_errors.build_tool_error(
                                tool_errors.VALIDATION_ERROR,
                                str(refusal),
                                suggestion="These arguments are sent as HTTP headers; non-ASCII "
                                "text is base64-encoded (about 4 bytes per CJK character). "
                                "Shorten the argument values and retry.",
                                denied_tool=name,
                                reason=refusal.reason,
                            )
                        except McpToolCatalogRefusal as refusal:
                            # ADR-0203 dep1：伺服器當下清單不允許（隔離／不在清單），不是上游故障。
                            result_text = tool_errors.build_tool_error(
                                tool_errors.PERMISSION_DENIED
                                if refusal.quarantined
                                else tool_errors.RESOURCE_NOT_FOUND,
                                str(refusal),
                                suggestion="Do not retry this tool; tell the user the MCP "
                                "server's current tool list does not allow it.",
                                denied_tool=name,
                                reason=refusal.reason,
                            )
                        except McpOperationException as exc:
                            result_text = tool_errors.build_tool_error(
                                exc.error.tool_error_class,
                                f"MCP operation failed: {exc.error.message}",
                                suggestion=(
                                    "Report the upstream failure to the user; retry only "
                                    "when the operation is known to be safe."
                                ),
                                touka_error_code=exc.error.touka_error_code,
                                mcp_error_kind=exc.error.kind.value,
                                protocol_code=exc.error.protocol_code,
                            )
                        else:
                            if not isinstance(res_data, McpCompleteOutcome):
                                msg = "MCP manager returned a non-canonical tool outcome."
                                raise TypeError(msg)
                            if res_data.is_error:
                                upstream_message = _render_mcp_complete(res_data)
                                result_text = _mcp_error_result(
                                    message=upstream_message,
                                    source_protocol_version=(res_data.source_protocol_version),
                                    kind=McpErrorKind.TOOL_RESULT,
                                )
                            else:
                                result_text = _render_mcp_complete(res_data)
                    else:
                        result_text = _unknown_tool_error(name)
                else:
                    result_text = _unknown_tool_error(name)

                duration_ms = get_monotonic_ms() - t0_monotonic  # 純執行耗時（additive）

                # 結構化 payload（錯誤或狀態 JSON，如 approval_pending）不注入
                # 時間尾綴——保持 JSON 可解析。
                if not tool_errors.is_structured_payload(result_text):
                    result_text = inject_time_to_result(result_text, name)
                # P2a-2：結構化成功為權威判定（回傳內容含 ❌／Error: 也不誤判）；
                # 舊子字串啟發保留為未結構化 legacy 工具的後備，避免回歸。
                is_success = tool_errors.is_tool_success(result_text) or (
                    not tool_errors.is_tool_error(result_text)
                    and "Error:" not in result_text
                    and "❌" not in result_text
                )

            # --- [終端機自動修復邏輯] ---
            if name in TERMINAL_REPAIR_TOOLS:
                exit_code_match = re.search(r"\[EXIT_CODE: (-?\d+)\]", result_text)
                if (
                    exit_code_match
                    and session_id
                    and _first_repair_report(session_id, name, args)
                ):
                    exit_code = int(exit_code_match.group(1))
                    if exit_code != 0 and session_id:
                        repair_instr = autonomy_service.get_repair_instruction(
                            session_id, exit_code
                        )
                        if repair_instr:
                            result_text += repair_instr
                            # P1 結構化進度：終端自我修復第 attempt/max 次
                            attempt, max_attempts = autonomy_service.get_repair_state(session_id)
                            await queue.put(
                                repair_event(attempt=attempt, max_attempts=max_attempts)
                            )
                    elif exit_code == 0 and session_id:
                        autonomy_service.reset_repair_attempts(session_id)

            # 2. 發送 tool_result 事件（封套 SSOT：success/error 投影 ＋ additive
            #    duration_ms／error_class；失敗才抬 error_class、成功不帶）
            if is_success:
                record_tool_succeeded(actually_used_tools, name, args)
            await queue.put(
                ToolCallEvent(
                    call_id=call_id,
                    tool_name=name,
                    arguments=args,
                    status="ok" if is_success else "error",
                    error_class=(
                        None if is_success else tool_errors.extract_error_class(result_text)
                    ),
                    duration_ms=duration_ms,
                    result_text=result_text,
                ).to_result_sse()
            )

            # 3. 發送 Log 用的文字
            time_str, weekday = get_taipei_time_info()
            finish_log = (
                f"✅ `時間記錄：{time_str} ({weekday})`\n" if is_success else "❌ `工具執行失敗`\n"
            )
            await queue.put({"type": "log", "content": finish_log, "result_text": result_text})

        except Exception as e:
            logger.exception("Error executing tool %s", name)
            # P5b：把真正的錯誤類別＋訊息（截斷）帶到前端，取代籠統 "Execution failed"
            detail = f"{type(e).__name__}: {str(e)[:200]}"
            await queue.put(tool_error_event(call_id=call_id, message=f"工具執行失敗：{detail}"))
            await queue.put(
                {"type": "log", "content": f"❌ 執行發生例外：{detail}\n", "result_text": "Error"}
            )
            result_text = tool_errors.from_exception(e, detail)

        finally:
            await queue.put(None)

        return result_text
