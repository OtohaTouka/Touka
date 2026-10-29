"""統一人類審批流程（hitl-unification H2 抽出；ADR-0137 2026-10-12 補記）.

登記 → 推審批卡進聊天 SSE → 阻塞等人類決定 → 推解算事件。Gate-2 的工具審批
（``ToolExecutor._await_human_approval``）與互審 ``verification_cmd`` 的逐條審批（B2）
共用這一份；呼叫端只給事件出口 ``emit``，不需要知道佇列或前端事件形狀。

隔離（iso-c2）：政策時在同意帳本記過層級的呼叫，卡上帶 ``isolation``；層級是 none 時核准
請求必須回傳卡片上的降級去處（``payload.isolation_ack``），對得上才記同意，否則不算核准。
去處是未隔離（direct）時，每個參數值完整上卡到 64 KiB；仍有截斷、或要執行的程式不在參數裡，
卡片標 ``complete: false``，伺服器端不算核准、不記同意（C2-3 必做 ③）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from src.core.exceptions import ErrorCode
from src.services import tool_errors
from src.services.approval_service import (
    APPROVAL_VERDICTS,
    DEFAULT_TIMEOUT_S,
    approval_service,
)
from src.services.isolation.consent import ConsentKey, consent_ledger, consent_subject
from src.services.isolation.policy import policy_level
from src.services.isolation.profiles import authorize_derived_path, current_layout
from src.services.isolation.status import isolation_monitor
from src.utils.isolation_policy import ISOLATION_UNWIRED_TOOLS, RUNNER_NOT_WIRED, VERIFY_ACTION
from src.utils.tool_events import (
    ToolCallEvent,
    tool_approval_request_event,
    tool_approval_resolved_event,
    tool_start_event,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from src.models.identity import AgentIdentity
    from src.services.isolation.consent import ExecutionGrant, Fallback
    from src.services.isolation.status import IsolationSnapshot
    from src.utils.isolation_policy import IsolationLevel

    EventSink = Callable[[dict[str, Any]], Awaitable[None]]

APPROVAL_PREVIEW_LIMIT = 200  # 一般參數值截斷（大載荷如 write.content 不全量上卡）
APPROVAL_COMMAND_PREVIEW_LIMIT = 8 * 1024  # command 完整上卡（A7），只在超過 8 KB 時截斷
# 未隔離卡的每個值（含 command）完整上卡的上限；仍超過就不能在卡上核准
APPROVAL_UNISOLATED_PREVIEW_LIMIT = 64 * 1024
# 要執行的程式不在參數裡（卡上看不到）：未隔離時一律不能在卡上核准
UNISOLATED_UNSHOWABLE_TOOLS: frozenset[str] = frozenset({"verify_mcp_locally"})
ISOLATION_ACK_FIELD = "isolation_ack"
ISOLATION_UNACKNOWLEDGED = "isolation_unacknowledged"
ISOLATION_INCOMPLETE = "isolation_incomplete"


@dataclass(frozen=True)
class CardIsolation:
    """審批卡上的隔離標示：政策時的層級與部署預期；none 時附核准後的降級去處與它的 features.

    卡片文案要的其他事實（沒有值就不送）：``net`` 降級去處那一層的網路設定問題、
    ``block_ports_issue`` 封鎖埠擋不全、``local_issue`` 讓降級只能「未隔離」的本機原因。
    ``complete`` 假＝去處是未隔離、卡上卻沒有完整顯示要執行的內容（只在假的時候送）。
    """

    level: IsolationLevel
    fallback: Fallback | None
    features: frozenset[str]
    reason: str | None
    reason_detail: str | None
    expected: IsolationLevel
    net: str | None = None
    block_ports_issue: str | None = None
    local_issue: str | None = None
    complete: bool = True

    def event_field(self) -> dict[str, Any]:
        """事件的 ``isolation``：features 送名稱清單，fallback 只在層級 none 時送。"""
        field: dict[str, Any] = {"level": self.level, "features": sorted(self.features)}
        if self.fallback is not None:
            field["fallback"] = self.fallback
        facts = {
            "net": self.net,
            "block_ports_issue": self.block_ports_issue,
            "local_issue": self.local_issue,
        }
        return {
            **field,
            "reason": self.reason,
            "reason_detail": self.reason_detail,
            "expected": self.expected,
            **{name: value for name, value in facts.items() if value},
            **({} if self.complete else {"complete": False}),
        }

    def ack_schema(self) -> dict[str, Any] | None:
        """層級 none：核准端點只收 ``isolation_ack`` 等於卡片 fallback 的請求。"""
        if self.fallback is None:
            return None
        ack = {ISOLATION_ACK_FIELD: {"const": self.fallback}}
        return {"type": "object", "required": [ISOLATION_ACK_FIELD], "properties": ack}


async def card_isolation(key: ConsentKey, tool: str) -> CardIsolation | None:
    """政策時記過層級的呼叫才標示；none 時照專案能不能授權算降級去處。

    沒接 runner 的工具不經 runner：一律「未隔離」（帳本沒有可用紀錄時核准記不到同意）。
    """
    record = consent_ledger.pending(key)
    snap = isolation_monitor.snapshot()
    if tool in ISOLATION_UNWIRED_TOOLS:
        reason = record.reason if record else RUNNER_NOT_WIRED
        return CardIsolation("none", "direct", frozenset(), reason, None, snap.expected)
    if record is None:
        return None
    if record.level != "none":
        features = (
            isolation_monitor.local_decision().features if tool == "scaffold_mcp" else snap.features
        )
        return CardIsolation(record.level, None, features, None, None, snap.expected)
    root = record.project_root
    blocked = root is not None and await authorize_derived_path(root, current_layout(), key.owner)
    plan = isolation_monitor.fallback_for(
        profile_complete=not blocked, executor_eligible=record.executor_eligible
    )
    detail = snap.detail if snap.reason == record.reason else None
    direct = plan.kind == "direct"
    return CardIsolation(
        "none", plan.kind, plan.features, record.reason, detail, snap.expected,
        net=_fallback_net(plan.kind, snap),
        block_ports_issue=None if direct else snap.block_ports_issue,
        local_issue=snap.local_issue if direct else None,
    )


def _fallback_net(kind: str, snap: IsolationSnapshot) -> str | None:
    """降級去處那一層的網路設定問題（卡片的結尾與補救那行用）；未隔離時沒有意義。"""
    if kind == "executor":
        return snap.executor.net if snap.executor else None
    return snap.local.net if kind == "landlock" else None


def _isolation_verdict(
    key: ConsentKey, isolation: CardIsolation, payload: dict[str, Any] | None
) -> str:
    """核准回傳的 ``isolation_ack`` 要等於卡片的 fallback，才在帳本記同意。

    未隔離卡沒有完整顯示要執行的內容時，一律不算核准、不記同意（不看前端有沒有擋）。
    """
    fallback = isolation.fallback
    if fallback is None:
        return "approved"
    if fallback == "direct" and not isolation.complete:
        return ISOLATION_INCOMPLETE
    acked = (payload or {}).get(ISOLATION_ACK_FIELD) == fallback
    if acked and consent_ledger.grant(key, fallback=fallback, features=isolation.features):
        return "approved"
    return ISOLATION_UNACKNOWLEDGED


def _preview_limit(key: str, *, unisolated: bool = False) -> int:
    if unisolated:
        return APPROVAL_UNISOLATED_PREVIEW_LIMIT
    return APPROVAL_COMMAND_PREVIEW_LIMIT if key == "command" else APPROVAL_PREVIEW_LIMIT


def approval_preview_truncated(args: dict[str, Any], *, unisolated: bool = False) -> bool:
    """審批卡上有沒有參數被截斷（ADR-0246：截斷時不產生 IDE「由人核准」標記）。

    IDE 標記只用預設上限（``tool_executor`` 不帶 ``unisolated``）：未隔離卡放寬的上限不影響它。
    """
    return any(
        len(value if isinstance(value, str) else str(value))
        > _preview_limit(key, unisolated=unisolated)
        for key, value in args.items()
        if not (value is None or isinstance(value, (bool, int, float)))
    )


def _unisolated_card_complete(tool: str, args: dict[str, Any]) -> bool:
    """未隔離卡有沒有完整顯示要執行的內容：程式不在參數裡的工具不算；有值超過上限也不算。"""
    return tool not in UNISOLATED_UNSHOWABLE_TOOLS and not approval_preview_truncated(
        args, unisolated=True
    )


def approval_args_preview(args: dict[str, Any], *, unisolated: bool = False) -> dict[str, Any]:
    """審批卡用參數預覽（純函式）：``command`` 保留到 8 KB，其他長值截到 200 字.

    ``unisolated``（去處是未隔離）：每個值都保留到 64 KiB。
    """
    preview: dict[str, Any] = {}
    for key, value in args.items():
        if value is None or isinstance(value, (bool, int, float)):
            preview[key] = value
            continue
        text = value if isinstance(value, str) else str(value)
        is_command = key == "command"
        limit = _preview_limit(key, unisolated=unisolated)
        if len(text) > limit:
            overflow = len(text) - limit
            marker = (
                f"\n…[truncated: {overflow} more chars not shown]"
                if is_command
                else f"…(+{overflow} chars)"
            )
            text = text[:limit] + marker
        preview[key] = text
    return preview


def approval_denied_error(name: str, verdict: str) -> str:
    """審批未通過的模型面結構化錯誤（verdict ∈ rejected/timeout/queue_full/isolation_*）。"""
    detail = {
        "rejected": "The user rejected this tool call.",
        "timeout": "The approval request timed out with no human decision.",
        "queue_full": "The approval queue is full; the call was auto-rejected.",
        ISOLATION_UNACKNOWLEDGED: (
            "The approval did not confirm the isolation shown on the card, so it does not "
            "count as consent."
        ),
        ISOLATION_INCOMPLETE: (
            "It would run without isolation, and the card cannot show everything that would "
            "run (an argument over 64 KiB, or code that is not in the arguments), so it cannot "
            "be approved."
        ),
    }.get(verdict, "Approval was not granted.")
    return tool_errors.build_tool_error(
        tool_errors.PERMISSION_DENIED,
        f"Tool '{name}' was not approved: {detail}",
        suggestion=(
            "Do not retry the same call; explain the situation to the user "
            "or propose a safer alternative."
        ),
        denied_tool=name,
        reason=f"approval_{verdict}",
    )


def human_only_unavailable_error(name: str, reason: str | None) -> str:
    """HUMAN_ONLY（硬地板，ADR-0246）無人在場的模型面錯誤：不能靠切模式或審查員放行。"""
    return tool_errors.build_tool_error(
        tool_errors.PERMISSION_DENIED,
        f"{reason or 'Hard floor'}. Tool '{name}' cannot run without a human, and no human "
        "is present in this session.",
        suggestion=(
            "Do not retry or work around this call; ask the user to do it themselves "
            "or to approve it in an interactive chat."
        ),
        denied_tool=name,
        reason="human_only",
    )


def auto_review_denied_error(name: str, reason: str) -> str:
    """Auto 審查員判 deny（ADR-0246 U2c-3）：不執行、不跳卡。"""
    return tool_errors.build_tool_error(
        tool_errors.PERMISSION_DENIED,
        f"Tool '{name}' was refused by the Auto reviewer: {reason}",
        suggestion="Do not retry or work around this call; explain the refusal to the user.",
        denied_tool=name,
        reason="auto_review_denied",
        touka_error_code=ErrorCode.AUTO_REVIEW_DENIED.value,
    )


def auto_review_no_human_error(name: str, reason: str, *, headless_terminal: bool) -> str:
    """要審的動作沒被審查員放行（轉人），但這次沒有人在場：拒絕。"""
    code = (
        ErrorCode.AUTO_REVIEW_HEADLESS_TERMINAL
        if headless_terminal
        else ErrorCode.AUTO_REVIEW_UNAVAILABLE
    )
    return tool_errors.build_tool_error(
        tool_errors.PERMISSION_DENIED,
        f"{reason} Tool '{name}' needs a human decision, and no human is present in this "
        "session.",
        suggestion=(
            "Do not retry or work around this call; ask the user to run it themselves "
            "or to approve it in an interactive chat."
        ),
        denied_tool=name,
        reason="auto_review_headless_terminal" if headless_terminal else "auto_review_escalated",
        touka_error_code=code.value,
    )


async def await_human_approval(
    *,
    name: str,
    args: dict[str, Any],
    call_id: str,
    agent_id: str,
    session_id: str | None,
    risk: str,
    emit: EventSink,
    review_reason: str | None = None,
    hook_reason: str | None = None,
    human_only: bool = False,
) -> str:
    """回 "approved"|"rejected"|"timeout"|"queue_full"。

    先登記後推卡（避免極速 approve 撞 404）；解算事件一律回推（含逾時），卡片狀態可收斂。
    卡上帶 Auto 審查員轉人的理由與「只有人能核准」標記（有值才帶，ADR-0246）；hook 要求人核准時
    另帶 hook 的理由（可為空字串，ADR-0292）。
    """
    exec_id = f"appr_{uuid.uuid4().hex[:10]}"
    key = ConsentKey(agent_id, session_id or "", call_id)
    isolation = await card_isolation(key, name)
    unisolated = isolation is not None and isolation.fallback == "direct"
    preview = approval_args_preview(args, unisolated=unisolated)
    if isolation is not None and unisolated:
        isolation = replace(isolation, complete=_unisolated_card_complete(name, args))
    registered = await approval_service.register(
        exec_id,
        tool_name=name,
        agent_id=agent_id,
        session_id=session_id,
        arguments_preview=preview,
        interaction_schema=isolation.ack_schema() if isolation else None,
    )
    if not registered:
        return "queue_full"

    await emit(
        tool_approval_request_event(
            exec_id=exec_id,
            call_id=call_id,
            tool=name,
            args_preview=preview,
            agent_id=agent_id,
            risk=risk,
            timeout_s=DEFAULT_TIMEOUT_S,
            review_reason=review_reason,
            hook_reason=hook_reason,
            human_only=human_only,
            isolation=isolation.event_field() if isolation else None,
        )
    )
    await emit({"type": "touka_status", "status": f"awaiting_approval: {name}"})

    resolution = await approval_service.wait_for_resolution(exec_id)
    verdict = APPROVAL_VERDICTS[resolution.decision]
    if verdict == "approved" and isolation is not None:
        verdict = _isolation_verdict(key, isolation, resolution.response_payload)
    await emit(
        tool_approval_resolved_event(exec_id=exec_id, call_id=call_id, tool=name, decision=verdict)
    )
    return verdict


class CommandApprover:
    """互審 ``verification_cmd`` 的逐條人類審批（B2）：每條需要核准的指令各跳一張卡.

    依賴反轉：``evidence.policy_command_gate`` 只看到 ``await approver(cmd) -> 審批結果``。
    每次問人用自己的步驟 id ``<request_review id>:verify:<n>``（n 依問人順序從 1 起），
    前端各自成一步：核准 → ``tool_start``，跑完由 ``finish`` 收尾；拒絕／逾時／佇列滿 →
    該步驟以 permission_denied 結束，指令不執行。
    """

    # 卡片與稽核的工具名：驗證指令與 execute_command 走同一套終端政策（SP1）
    tool_name = "execute_command"

    def __init__(
        self,
        *,
        emit: EventSink,
        parent_call_id: str,
        agent_id: str,
        session_id: str | None,
        identity: AgentIdentity | None = None,
        ctx: Mapping[str, Any] | None = None,
    ) -> None:
        """綁定本次 request_review 的事件出口與步驟 id 前綴；有身分才記隔離層級與同意。"""
        self._emit = emit
        self._parent_call_id = parent_call_id
        self._agent_id = agent_id
        self._session_id = session_id
        self._identity = identity
        self._ctx = ctx or {}
        self._asked = 0
        self._open: list[tuple[str, str]] = []  # 已核准、尚未收尾的（步驟 id, 指令）
        self._consent_keys: dict[str, ConsentKey] = {}  # 指令 → 它最近一張卡的帳本鍵

    def _result(
        self, call_id: str, cmd: str, error_class: str | None, text: str
    ) -> dict[str, Any]:
        return ToolCallEvent(
            call_id=call_id,
            tool_name=self.tool_name,
            arguments={"command": cmd},
            status="error" if error_class else "ok",
            error_class=error_class,
            result_text=text,
        ).to_result_sse()

    async def __call__(
        self, cmd: str, *, review_reason: str | None = None, human_only: bool = False
    ) -> str:
        """為這一條指令跳卡並等人類決定；回審批結果（"approved"|"rejected"|"timeout"|…）。

        卡上帶 Auto 審查員轉人的理由與底線的「只有人能核准」（和 executor 的終端卡一致）。
        """
        self._asked += 1
        call_id = f"{self._parent_call_id}:verify:{self._asked}"
        args = {"command": cmd}
        if self._identity is not None:
            # 政策時：記下這條指令的層級與 subject（卡片據此標示隔離，核准時記同意）
            await policy_level(
                self._identity, self._ctx, self._session_id, call_id, tool=VERIFY_ACTION,
                args=args,
            )
            self._consent_keys[cmd] = ConsentKey.of(self._identity, self._session_id, call_id)
        verdict = await await_human_approval(
            name=self.tool_name,
            args=args,
            call_id=call_id,
            agent_id=self._agent_id,
            session_id=self._session_id,
            risk="dangerous",  # 只有政策判 CONFIRM／REVIEW／HUMAN_ONLY 的指令會問人
            emit=self._emit,
            review_reason=review_reason,
            human_only=human_only,
        )
        if verdict == "approved":
            self._open.append((call_id, cmd))
            await self._emit(tool_start_event(call_id=call_id, tool=self.tool_name, args=args))
            return verdict
        denied = approval_denied_error(self.tool_name, verdict)
        await self._emit(self._result(call_id, cmd, tool_errors.PERMISSION_DENIED, denied))
        return verdict

    def take_consent(self, cmd: str) -> ExecutionGrant | None:
        """執行前取回這條指令的 grant（subject 綁指令本身；只能一次）。"""
        key = self._consent_keys.pop(cmd, None)
        if key is None:
            return None
        return consent_ledger.take(key, subject=consent_subject(VERIFY_ACTION, {"command": cmd}))

    async def finish(self, summary: str | None) -> None:
        """為每個已核准、還沒收尾的步驟推結果；``summary`` 為 None＝審查中途失敗。"""
        failed = tool_errors.build_tool_error(
            tool_errors.INTERNAL_ERROR,
            "The review stopped before this verification step finished.",
            suggestion="Tell the user the review did not complete; re-run request_review.",
        )
        for call_id, cmd in self._open:
            if summary is None:
                await self._emit(self._result(call_id, cmd, tool_errors.INTERNAL_ERROR, failed))
            else:
                await self._emit(self._result(call_id, cmd, None, summary))
        self._open.clear()
