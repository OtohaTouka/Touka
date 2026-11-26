"""In-process 子代理派發（in-process sub-agent orchestration 提案 Phase 1 MVP）.

``dispatch_subagent``：主 agent 在 ReAct loop 內派「一個」in-process 子代理跑單一
自包含任務——任務不外派給外部 AI，整個在 Touka 內部完成。

設計要點（對應提案 §9 Phase 1 邊界）：
- **非同步 token 形**：立即回結構化成功（含 ``callback_token``），背景 asyncio task
  開子 loop；完成（或失敗）後 ``DispatchCallbackStore.deliver_result`` **in-process
  直呼**（繞過 HTTP 回呼路由），喚醒既有 ``mission_tools(action="await_dispatch_result")``
  ——等待工具零改動重用（ADR-0051 語義不變）。這一輪沒列 ``mission_tools`` 時沒有工具
  能收憑證，改成等子代理做完、直接帶回結果（ADR-0316 PR 2 補記）。
- **臨時身分不落盤**：``__subagent_{hex}__`` ＋ Subagent 角色（ADR-0245，工具照抄原
  Guest）；全路徑不呼 ``record_working_brain``／soul 寫入（提案 §7.2-3 污染風險）。
  Subagent 權限面天然不含 ``dispatch_subagent``（Gate 1 不廣告＋Gate 2 拒絕）＝遞迴
  根絕的權限閘。
- **組裝走真實 pipeline**（ADR-0133 context-snapshot 模式）：子代理拿到與其身分
  相符的工具面與系統塊；組裝後再剝除 ``dispatch_subagent``（遞迴根絕的能力閘）。
- **衍生 request_id** ``{parent}:sub{n}``：不與父迴圈共用 rhythm/slot 鍵（避免
  interference）；**無 session_id**：不碰 session 狀態與中斷旗標。
- 子 loop ctx 固定 ``is_toukasoul=False``；``enable_autonomy`` **沿父鏈繼承**
  （ADR-0146：heartbeat 派生的子代理同屬無人 run→工具輪有限〔保底 50〕；互動派生
  維持互動語意——不得硬編 False）。子代理不消耗每日自主配額、不觸發續命 nudge；
  工具執行（ReAct-on-tool_use）不受影響。
- 失敗也必 deliver（``FAILED``）：等待者不會空等到 TIMEOUT 才知道出事。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.adapters.schemas import AnthropicRequest
from src.handlers.adapters.retry import KNOWN_ERROR_TYPES
from src.models.advertised_tools import AdvertisedTools
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import tool_errors
from src.services.dispatch_callback_store import get_callback_store
from src.services.dispatchers.factory import get_stream_generator
from src.services.hooks.lifecycle import notify_subagent_stop, run_subagent_start_hooks
from src.services.hooks.stopping import stopped_since_start
from src.services.isolation.policy import session_project_id
from src.services.isolation.profiles import configured_projects_root
from src.services.isolation.project_space import structural_scope
from src.services.model_manager import model_manager
from src.services.parent_turn import ParentTurnEndedError, current_stream_id, turn_active
from src.services.resource_manager import resource_manager
from src.services.rhythm_service import HEADLESS_FALLBACK_ROUNDS
from src.services.subagent_registry import FinishedSubagent, RunningSubagent, subagent_registry
from src.services.subagent_report import REPORT_INSTRUCTION, parse_subagent_report
from src.tools.contracts import (
    MAX_SUBAGENT_GROUP,
    SUBAGENT_GROUP_CONCURRENCY,
    SUBAGENT_TIMEOUT_SECONDS,
    limit_minutes,
)
from src.tools.terminal import stop_agent_commands
from src.utils.auth_mode import enforce_incognito_manual
from src.utils.brain_persistence import get_active_brain
from src.utils.model_text import single_line
from src.utils.presence import (
    PARENT_IDENTITY,
    PARENT_SESSION_ID,
    PARENT_STREAM_ID,
    SUBAGENT_PROJECT_COMMANDS,
    is_human_present,
    subagent_project_commands,
)
from src.utils.secret_text import sanitize_error_text
from src.utils.task_utils import spawn_logged
from src.utils.time_utils import get_current_epoch
from src.utils.turn_notices import (
    NOTICE_REFUSAL,
    NOTICE_SAFETY,
    NOTICE_TOOL_HALLUCINATION,
    TURN_NOTICE_EVENT,
)
from src.utils.user_intent import subagent_intent

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Coroutine, Sequence

    from src.services.subagent_registry import SubagentOutcome

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常數
# --------------------------------------------------------------------------- #

_SUB_PREFIX = "__subagent_"
_SUB_SEQ = itertools.count(1)
_SUB_TIMEOUT_SECONDS = SUBAGENT_TIMEOUT_SECONDS
_RESULT_SUMMARY_MAX = 4000
# 結果太長時保留結尾（子代理的結論寫在最後），開頭標明省略了（ADR-0318）
OMITTED_HEAD_NOTE = "…（前段過長已省略）\n"
_PARTIAL_HEADER = "已做到的部分（沒做完，可能不完整）："
# 結果的三種結局：傳輸失敗或自己回報失敗都算失敗；沒附回報與部分完成另計（ADR-0318）
_OUTCOME_WORDS: dict[SubagentOutcome, str] = {
    "done": "完成",
    "partial": "部分完成或未確認",
    "failed": "失敗",
    "cancelled": "已取消",
}
# 子代理多久看一次主代理那段對話有沒有被按停止（停止沒有事件可掛，只有累計次數；ADR-0318）
STOP_CHECK_SECONDS = 0.5
# 背景派工的 task 本身被取消（Touka 停止或重啟）時交回的說法；拿不到已做到的部分
_SHUTDOWN_NOTE = "已取消：Touka 停止或重啟，子代理沒有做完。"
# 子代理這一輪被拒答、被安全過濾擋下、提到沒呼叫的工具：交回的結果寫明（ADR-0323）。
# 沒有文字時當成結果，有文字時另列系統附註；拒答原文沿用小提示的截短，再收成一行
SUBAGENT_REFUSED = "子代理被模型拒答"
SUBAGENT_SAFETY_BLOCKED = "子代理的回覆被廠商的安全過濾擋下"
SUBAGENT_TOOL_HALLUCINATION = "子代理的回覆提到它沒有呼叫的工具（{tools}），那部分不一定真的做過"
_SUBAGENT_NOTICE_KINDS = frozenset({NOTICE_REFUSAL, NOTICE_SAFETY, NOTICE_TOOL_HALLUCINATION})
_NO_TEXT = "(子代理無文字產出)"
# 父請求的目標標頭不屬於子代理：留著會讓 MonitorLayer 把父靈魂或父 session 認成目標
# （ADR-0245 修補）
_PARENT_TARGET_HEADERS = frozenset(
    {"x-touka-soul-id", "x-touka-session-id", "x-toukachat-session-id"}
)
# 子代理不可再派子代理或子代理群組（遞迴根絕的能力閘）：請求與清單一起拿掉
_SUBAGENT_STRIPPED_TOOLS = frozenset({"dispatch_subagent", "dispatch_subagent_group"})

# Phase 4：fan-out 硬界（防弱模型無限展開，提案 §6 邊界 5）。
_MAX_GROUP = MAX_SUBAGENT_GROUP
_DEFAULT_GROUP_CONCURRENCY = SUBAGENT_GROUP_CONCURRENCY

# 給子代理的固定提示（ADR-0309）：不放專案的真實路徑
PROJECT_NOTE = '這個任務可以用對話綁定的專案：檔案工具帶 scope="project"，路徑相對於專案根目錄。'
READ_ONLY_NOTE = "你只能查看，不能修改任何檔案；發現的問題請寫在回報裡。"
# 窄開關打開時（ADR-0333）：怎麼在專案裡跑指令、什麼情況會被拒
COMMANDS_NOTE = (
    "這個專案裡可以用 execute_command 跑指令：指令從專案根目錄開始，路徑用相對路徑，子資料夾用"
    "工具自己的參數（例如 pytest web/tests、npm --prefix web test）；前景指令最多 30 秒，跑測試"
    "這類長指令加 background=true，再用 get_command_result（可帶 wait_seconds）取結果。"
    "Auto 模式每條指令都先送審查員，被拒絕不要重試，寫進回報交給派你的主代理；"
    "不要用 $(...)、反引號、$變數、> 寫檔，也不要用 ;、|、&& 串接指令。"
    "派你的那一輪結束後就不能再跑指令。"
)
# 同步派工時告訴主代理為什麼等做完才回
_NO_BOARD_WAIT = "因為這一輪沒有任務看板工具（mission_tools），這次等子代理做完才回。"
_COMMANDS_WAIT = "因為這個子代理能在專案裡跑指令（你這一輪結束就會被收掉），這次等它做完才回。"

# --------------------------------------------------------------------------- #
# 內部：子代理請求組裝與子 loop 執行
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SubagentScope:
    """這個子代理碰得到什麼：派出時算一次，組裝與執行用同一份（ADR-0309）。

    ``project_commands``：能在專案裡跑指令（ADR-0333 的窄開關）；``parent_stream_id`` 是派它的
    那一輪的串流編號，派工工具一進入就記下。
    """

    project_id: str | None = None
    has_project: bool = False
    read_only: bool = False
    project_commands: bool = False
    parent_stream_id: str | None = None


_NO_SCOPE = SubagentScope()


@dataclass(frozen=True)
class _TurnCommands:
    """派它的那一輪結束後要收掉的指令（ADR-0333）：子代理身分與那一輪的對話、串流編號。"""

    identity: AgentIdentity
    session_id: str
    stream_id: str

    def turn_over(self) -> bool:
        return not turn_active(self.session_id, self.stream_id)

    async def collect(self) -> None:
        await stop_agent_commands(self.identity, error=ParentTurnEndedError())


def _is_read_only(value: Any) -> bool:
    """沒給、null 或明確的 false 才算可寫；看不懂的值一律當只能看（往安全的方向錯）。"""
    return value is not None and value is not False


class SubagentCancelledError(Exception):
    """主代理那段對話被按了停止，子代理跟著停下；``partial`` 是已經收到的文字（ADR-0318）。"""

    _MESSAGE = "The subagent was stopped because its conversation was stopped."

    def __init__(self, partial: str = "") -> None:
        super().__init__(self._MESSAGE)
        self.partial = partial


class SubagentStoppedError(SubagentCancelledError):
    """等 SubagentStart hook 的期間使用者按了停止：不送模型（禁令 67 ⑦）；結果也是已取消。"""

    _MESSAGE = "The user stopped this run before the subagent started."


class SubagentTimeoutError(TimeoutError):
    """超過時限被中止；``partial`` 是已經收到的文字（ADR-0318）。"""

    def __init__(self, partial: str = "") -> None:
        super().__init__(f"The subagent was stopped after {_SUB_TIMEOUT_SECONDS:g} seconds.")
        self.partial = partial


class SubagentModelError(Exception):
    """子代理的模型呼叫失敗：只帶固定的種類與失敗前寫下的文字，不帶廠商原文（ADR-0318）。"""

    def __init__(self, error_type: Any, partial: str = "") -> None:
        known = isinstance(error_type, str) and error_type in KNOWN_ERROR_TYPES
        kind = error_type if known else "unknown"
        super().__init__(f"The subagent's model call failed ({kind}).")
        self.error_type = kind
        self.partial = partial


def _limit_minutes() -> int:
    return limit_minutes(_SUB_TIMEOUT_SECONDS)


def _dispatch_situation(base_ctx: Any, parent_label: str) -> str:
    """派工情境（ADR-0316 PR 2 補記）：誰派的、主代理那邊的情境、時限與工具輪。

    子代理自己一律沒人在場（``_mark_subagent_run``），說法和 ``<approval_context>`` 一致；
    模式由那個區塊說，這裡不重複。只讀父 ctx 的情境標記與常數，不讀設定檔。
    """
    if base_ctx.get("heartbeat_run"):
        parent_side = "主代理是心跳排程叫醒的，沒有人在場"
    elif is_human_present(base_ctx):
        parent_side = "主代理正在和人對話，但那個人看不到你"
    else:
        parent_side = "主代理這一輪沒有人在場"
    if base_ctx.get("enable_autonomy"):  # 心跳派生：工具輪有限（ADR-0146）
        rounds = f"有上限，沒另外設定時最多 {HEADLESS_FALLBACK_ROUNDS} 輪，用完要收尾"
    else:
        rounds = "照系統設定（預設不限）"
    return (
        "【派工情境】\n"
        f"- 派你的是 {single_line(parent_label)}；{parent_side}。\n"
        "- 你這裡沒有人能回答問題或核准；需要核准的動作寫進回報，交給派你的主代理。\n"
        f"- 時限約 {_limit_minutes()} 分鐘，超過就中止。工具輪：{rounds}。\n"
        "- 只回文字、不再呼叫工具，就算做完。\n"
    )


def _subagent_user_prompt(
    goal: str, scope: SubagentScope = _NO_SCOPE, situation: str = ""
) -> str:
    """子代理的自包含任務指示（子代理看不到主對話上下文）。"""
    notes = [
        *([PROJECT_NOTE] if scope.has_project else []),
        *([READ_ONLY_NOTE] if scope.read_only else []),
        *([COMMANDS_NOTE] if scope.project_commands else []),
    ]
    limits = "".join(f"{note}\n" for note in notes)
    return (
        "你是 Touka 的 in-process 子代理，負責完成以下單一任務。\n"
        "完成後請直接輸出最終結果全文（會原樣回傳給主代理），"
        "不要提問、不要等待後續指示。\n"
        f"{limits}\n"
        f"{situation}"
        f"【任務】\n{goal.strip()}\n\n"
        f"{REPORT_INSTRUCTION}"
    )


def _derive_context(base_ctx: Any) -> PipelineContext:
    """從父 ctx 衍生子 loop ctx（淺複製共享 client 連線，覆寫身分/會話欄位）。"""
    if isinstance(base_ctx, PipelineContext):
        return base_ctx.model_copy()
    return PipelineContext(**dict(base_ctx or {}))


def _mark_subagent_run(
    ctx: PipelineContext, base_ctx: Any, goal: str, scope: SubagentScope
) -> None:
    """子代理一律無人在場（U2c-2）。

    子 loop 只收文字、審批卡送不到人：要人核准的動作立刻拒絕或等人確認，不等 180 秒
    逾時。原意只帶父代理的人類原意；派工目標是代理人轉述，只留在 ctx、不進審查員的輸入
    （R-2c ③ #4）。
    """
    ctx["subagent_run"] = True
    ctx["dispatch_run"] = True
    ctx["subagent_goal"] = goal
    ctx["user_intent"] = subagent_intent(base_ctx.get("user_intent"))
    # 專案只來自對話的綁定，不收模型填的值；工具清單（組裝）與權限判定（執行）都讀這裡
    ctx["project_id"] = scope.project_id
    ctx["subagent_read_only"] = scope.read_only
    # 窄開關（ADR-0333）：每次都覆寫，不從父 ctx 繼承；記帳身分只收真的身分物件
    parent_identity = base_ctx.get("identity")
    ctx[SUBAGENT_PROJECT_COMMANDS] = scope.project_commands
    ctx[PARENT_SESSION_ID] = base_ctx.get("session_id")
    ctx[PARENT_STREAM_ID] = scope.parent_stream_id
    ctx[PARENT_IDENTITY] = parent_identity if isinstance(parent_identity, AgentIdentity) else None
    # 子代理不是網頁對話，失敗記號也從零開始：三種主代理派的子代理走同一條失敗路徑（ADR-0318）
    ctx["ui_authorized"] = False
    ctx["model_failed"] = False
    ctx["model_error_type"] = None
    # 主代理的看板不給子代理：組裝時 ContextLayer 會把這個欄位注入（子代理的任務已寫在派工情境裡）
    ctx["mission_awareness"] = None


async def _assemble_subagent_request(
    *,
    goal: str,
    sub_identity: AgentIdentity,
    base_ctx: Any,
    model: str | None,
    scope: SubagentScope = _NO_SCOPE,
    parent_label: str = "unknown",
) -> AnthropicRequest:
    """跑真實 ProxyPipeline 為子代理組裝請求（ADR-0133 模式），後剝除遞迴能力。

    pipeline 依 ``sub_identity``（Subagent）注入相符的工具面與系統塊；組裝完成後
    （a）若指定 ``model`` 則強制之（不依賴未知臨時身分的 brain 解析），
    （b）剝除 ``_SUBAGENT_STRIPPED_TOOLS``——子代理不可再派子代理或群組（能力閘）。
    """
    from src.services.proxy_service import pipeline  # noqa: LOCAL_IMPORT_BAN

    # hook 的補充說明是資料，只進第一則訊息；審查員只看 user_intent（禁令 64 ③）
    started = await run_subagent_start_hooks(base_ctx, sub_identity)
    if started.stopped:
        raise SubagentStoppedError

    # 不寫 max_tokens：Claude 用 AnthropicRequest 的預設 8192，其他家不帶上限。
    # convert_to_openai 會轉送呼叫端明確給的值（O20，給 IDE 的 Anthropic 入口；ADR-0254）。
    prompt = _subagent_user_prompt(goal, scope, _dispatch_situation(base_ctx, parent_label))
    request = AnthropicRequest(
        model=(model or "").strip() or sub_identity.agent_id,
        messages=[{"role": "user", "content": prompt + started.text}],
    )

    ctx = _derive_context(base_ctx)
    ctx.identity = sub_identity
    ctx.agent_id = sub_identity.agent_id
    ctx.session_id = None
    parent_headers = base_ctx.get("headers") or {}
    ctx["headers"] = {k: v for k, v in parent_headers.items() if k not in _PARENT_TARGET_HEADERS}
    # H1（決策 3）：兩階段一致——組裝（Gate-1 廣告面）與執行（Gate-2）都採父 session
    # 的 auth_level（人類派工當下的 AuthSwitcher 選擇＝事先決定；子代理只繼承、不可
    # 自升）。原本此處硬編 "Manual" 造成廣告面與執行面用不同等級求值。
    ctx.auth_level = base_ctx.get("auth_level") or "Manual"
    ctx.request_id = f"assemble:{sub_identity.agent_id}"
    ctx["is_toukasoul"] = False
    ctx["enable_autonomy"] = False
    _mark_subagent_run(ctx, base_ctx, goal, scope)

    await pipeline.execute(request, ctx)

    if model:
        request.model = model
    request.tools = [
        t
        for t in (request.tools or [])
        if not (
            isinstance(t, dict)
            and (t.get("function", {}) or {}).get("name") in _SUBAGENT_STRIPPED_TOOLS
        )
    ]
    return request


async def _drain_text(
    stream: AsyncGenerator[str],
    parts: list[str] | None = None,
    notices: list[dict[str, Any]] | None = None,
) -> str:
    """抽乾子 loop SSE 串流，累積文字 delta（OpenAI／Anthropic 雙形，仿 group.py）。

    呼叫端傳入 ``parts`` 時，逾時或失敗也拿得到已經收到的片段（ADR-0318）；傳入 ``notices`` 時
    收下拒答、安全過濾、工具幻覺這三種小提示（ADR-0323）。
    """
    parts = [] if parts is None else parts
    async with contextlib.aclosing(stream):
        async for chunk in stream:
            if not isinstance(chunk, str) or not chunk.startswith("data: "):
                continue
            data_str = chunk[6:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                data = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == TURN_NOTICE_EVENT:
                if notices is not None and data.get("kind") in _SUBAGENT_NOTICE_KINDS:
                    notices.append(data)
                continue
            text_delta = ""
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                text_delta = (choices[0].get("delta") or {}).get("content") or ""
            elif data.get("type") == "content_block_delta":
                text_delta = (data.get("delta") or {}).get("text") or ""
            if text_delta:
                parts.append(text_delta)
    return "".join(parts).strip()


async def _run_subagent(
    *,
    goal: str,
    parent_identity: AgentIdentity | None,
    parent_request_id: str,
    base_ctx: Any,
    model: str | None,
    read_only: bool = False,
    label: str = "subagent",
    notices: list[dict[str, Any]] | None = None,
    parent_stream_id: str | None = None,
) -> str:
    """開一個 in-process 子 ReAct loop 跑到完成，回傳最終文字產出（``notices`` 收拒答等小提示）。

    時限包住整段（SubagentStart hook、組裝、模型）。超過時丟 ``SubagentTimeoutError``、模型失敗丟
    ``SubagentModelError``、主代理那段對話被按停止丟 ``SubagentCancelledError``，都帶著已經收到的
    文字，SubagentStop hook 也拿到它（ADR-0318）。跑的期間登記在 ``subagent_registry``；結束時
    （做完或沒做完）它開的指令行程一起收掉（ADR-0333）。``parent_stream_id`` 是派它的那一輪的
    串流編號（派工工具一進入就記下）；能在專案裡跑指令時，那一輪結束後一直收它的指令。
    """
    seq = next(_SUB_SEQ)
    sub_identity = AgentIdentity.for_role(
        agent_id=f"{_SUB_PREFIX}{uuid.uuid4().hex[:8]}__", role="Subagent"
    )
    sub_request_id = f"{parent_request_id}:sub{seq}"
    parent_label = parent_identity.agent_id if parent_identity else "unknown"
    # 沒指定、執行器也沒注入模型（父這一輪 context 沒有模型）時才用父代理的大腦（ADR-0245 修補；
    # 3b-U4b-2 起各路由都寫 ctx model）；只取模型名，不帶父靈魂的人設與家對話
    if not model and parent_identity is not None:
        brain = get_active_brain(parent_identity)  # 和 SoulLayer 同一個入口（ADR-0250 A5）
        model = model_manager.request_model_name(brain) if brain else None
    logger.info(
        "in-process subagent start: %s (parent=%s, request_id=%s)",
        sub_identity.agent_id,
        parent_label,
        sub_request_id,
    )

    parts: list[str] = []
    reply = ""
    deadline = asyncio.timeout(_SUB_TIMEOUT_SECONDS)
    key = subagent_registry.add(
        RunningSubagent(label, parent_label, base_ctx.get("session_id"), get_current_epoch())
    )
    try:
        async with deadline:
            scope = await _subagent_scope(base_ctx, sub_identity, read_only, parent_stream_id)
            reply = await _unless_parent_stops(
                _assemble_and_run(
                    goal=goal,
                    sub_identity=sub_identity,
                    sub_request_id=sub_request_id,
                    base_ctx=base_ctx,
                    model=model,
                    scope=scope,
                    parent_label=parent_label,
                    parts=parts,
                    notices=notices,
                ),
                base_ctx,
                _turn_commands(sub_identity, base_ctx, scope),
            )
    except TimeoutError as exc:
        if not deadline.expired():  # 子代理裡別的逾時照原樣往外
            raise
        reply = "".join(parts).strip()
        raise SubagentTimeoutError(reply) from exc
    except SubagentCancelledError as exc:
        exc.partial = "".join(parts).strip()
        reply = exc.partial
        raise
    except SubagentModelError as exc:
        reply = exc.partial
        raise
    finally:
        subagent_registry.finish(key)
        # 不論做完沒有，它的指令（含背景、待審）一起收掉：子代理身分之後不再用（ADR-0318、0333）
        spawn_logged(stop_agent_commands(sub_identity), name="subagent-stop-commands")
        notify_subagent_stop(base_ctx, sub_identity, reply)
    return reply


async def _subagent_scope(
    base_ctx: Any, identity: AgentIdentity | None, read_only: bool, parent_stream_id: str | None
) -> SubagentScope:
    """派出時算一次這個子代理碰得到什麼（ADR-0309、ADR-0333）。"""
    project_id = await session_project_id(base_ctx, base_ctx.get("session_id"))
    # 對話的 project_id 可能只是佔位值（心跳建的家是 "default"）：解析得到路徑才算有專案
    root = await resource_manager.get_project_path(project_id, identity) if project_id else None
    # 專用空間裡被拒的形狀（路上有連結、第二層以下…）不打開：每條指令都會被拒
    usable = bool(root) and structural_scope(Path(root), configured_projects_root()).kind == "ok"
    commands = subagent_project_commands(
        base_ctx, dedicated=usable, read_only=read_only, stream_id=parent_stream_id
    )
    return SubagentScope(project_id, bool(root), read_only, commands, parent_stream_id)


def _turn_commands(
    sub_identity: AgentIdentity, base_ctx: Any, scope: SubagentScope
) -> _TurnCommands | None:
    """能在專案裡跑指令的子代理才要監看派它的那一輪（ADR-0333）。"""
    session_id = base_ctx.get("session_id")
    if not (scope.project_commands and session_id and scope.parent_stream_id):
        return None
    return _TurnCommands(sub_identity, str(session_id), scope.parent_stream_id)


async def _assemble_and_run(
    *,
    goal: str,
    sub_identity: AgentIdentity,
    sub_request_id: str,
    base_ctx: Any,
    model: str | None,
    scope: SubagentScope,
    parent_label: str,
    parts: list[str],
    notices: list[dict[str, Any]] | None = None,
) -> str:
    """組裝（含 SubagentStart hook）、執行；``parts`` 收已經收到的文字。"""
    request = await _assemble_subagent_request(
        goal=goal,
        sub_identity=sub_identity,
        base_ctx=base_ctx,
        model=model,
        scope=scope,
        parent_label=parent_label,
    )
    return await _run_assembled(
        request, sub_identity, sub_request_id, base_ctx, goal, scope, parts, notices
    )


async def _unless_parent_stops(
    work: Coroutine[Any, Any, str], base_ctx: Any, turn: _TurnCommands | None = None
) -> str:
    """子代理在自己的 task 裡跑，旁邊有停止監看（ADR-0318）。

    主代理那段對話被按停止（``stopped_since_start``）就只取消子代理的 task，丟一般例外
    ``SubagentCancelledError``：主代理的工具 task 不被取消，照常收到「已取消」。主代理這一輪自己被
    取消時，取消照樣往外傳（等待中的子代理 task 一起被取消）。``turn`` 不是 None 時，派它的那一輪
    結束後監看一直收它的指令（ADR-0333）。
    """
    task = asyncio.create_task(work)
    stopped = asyncio.Event()
    watch = spawn_logged(
        _cancel_when_parent_stops(base_ctx, task, stopped, turn), name="subagent-stop-watch"
    )
    try:
        return await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if stopped.is_set() and (current is None or current.cancelling() == 0):
            raise SubagentCancelledError from None
        raise
    finally:
        watch.cancel()


async def _cancel_when_parent_stops(
    base_ctx: Any, task: asyncio.Task, stopped: asyncio.Event, turn: _TurnCommands | None = None
) -> None:
    """停止只有累計次數、沒有事件，所以每 ``STOP_CHECK_SECONDS`` 看一次；沒有對話（IDE）就不看。

    ``turn``：派它的那一輪結束後，每次都收一次它的指令（政策檢查之後、等審查員期間才開的也收得到），
    子代理本身不取消，還能把報告寫完（ADR-0333）。
    """
    if not base_ctx.get("session_id"):
        return
    while not task.done():
        if stopped_since_start(base_ctx):
            stopped.set()
            task.cancel()
            return
        if turn is not None and turn.turn_over():
            try:
                await turn.collect()
            except Exception:  # 這次沒收到下次再收；監看不能停（停止鈕也靠它）
                logger.exception("Collecting a sub-agent's commands after its turn failed")
        await asyncio.sleep(STOP_CHECK_SECONDS)


async def _run_assembled(
    request: AnthropicRequest,
    sub_identity: AgentIdentity,
    sub_request_id: str,
    base_ctx: Any,
    goal: str,
    scope: SubagentScope,
    parts: list[str],
    notices: list[dict[str, Any]] | None = None,
) -> str:
    """跑組裝好的子代理請求，回傳最終文字；模型失敗時丟 ``SubagentModelError``。"""
    ctx = _derive_context(base_ctx)
    ctx.session_id = None
    # 執行 ctx 不經 ToolLayer：父代理是無痕就一律 Manual（與組裝一致，ADR-0246）
    ctx.auth_level = enforce_incognito_manual(ctx.auth_level, incognito=ctx.incognito)
    ctx.request_id = sub_request_id
    ctx["is_toukasoul"] = False
    # ADR-0146：headless 標記沿父鏈繼承——heartbeat 派生的子代理同屬無人 run，
    # 工具輪一律有限（保底 50）；互動派生的子代理維持互動語意（預設不限）。
    # 子代理仍不扣每日配額（配額只在 heartbeat pulse 派發時計一次）。
    ctx["enable_autonomy"] = bool(base_ctx.get("enable_autonomy"))
    ctx["pulse_rounds"] = None
    _mark_subagent_run(ctx, base_ctx, goal, scope)
    # 子代理只用自己組裝出的清單（已剝除派工工具），不沿用父代理的
    ctx.advertised_tools = AdvertisedTools.from_schemas(request.tools)

    stream = await get_stream_generator(
        request.model,
        request,
        sub_identity,
        sub_request_id,
        get_current_epoch(),
        ctx,
    )
    text = await _drain_text(stream, parts, notices)
    if ctx.get("model_failed"):
        raise SubagentModelError(ctx.get("model_error_type"), text)
    return text


def _keep_tail(text: str) -> str:
    """太長時保留結尾：子代理的結論寫在最後（ADR-0318）。"""
    if len(text) <= _RESULT_SUMMARY_MAX:
        return text
    return OMITTED_HEAD_NOTE + text[-_RESULT_SUMMARY_MAX:].lstrip()


def _notice_notes(notices: Sequence[dict[str, Any]]) -> list[str]:
    """拒答、安全過濾、工具幻覺小提示換成固定說法（參數在小提示裡已截短）；重複的只留一句。"""
    notes: list[str] = []
    for notice in notices:
        kind, params = notice.get("kind"), notice.get("params")
        params = params if isinstance(params, dict) else {}
        if kind == NOTICE_REFUSAL:
            detail = single_line(str(params.get("detail") or "")).strip()
            notes.append(f"{SUBAGENT_REFUSED}：「{detail}」" if detail else SUBAGENT_REFUSED)
        elif kind == NOTICE_SAFETY:
            notes.append(SUBAGENT_SAFETY_BLOCKED)
        elif kind == NOTICE_TOOL_HALLUCINATION:
            tools = single_line(str(params.get("tools") or "?"))
            notes.append(SUBAGENT_TOOL_HALLUCINATION.format(tools=tools))
    return list(dict.fromkeys(notes))


def _result_from_text(
    text: str, label: str, notices: Sequence[dict[str, Any]] = ()
) -> dict[str, Any]:
    """把子代理最終文字（含 Phase 2 回報塊）收斂成結構化結果 payload（DONE）。

    傳輸層 status（子 loop 是否跑完）與自我宣稱層 report_status 分開承載。拒答等小提示：沒有文字時
    寫成結果，有文字時放 ``system_notes``（不混進子代理自己的話；ADR-0323）。
    """
    report = parse_subagent_report(text)
    summary = _keep_tail(report.summary)
    notes = _notice_notes(notices)
    payload: dict[str, Any] = {
        "task_id": label,
        "status": "DONE",
        "result_summary": summary or "；".join(notes) or _NO_TEXT,
        "report_status": report.status,
        "claims": report.claims,
        "artifact_refs": report.artifact_refs,
        "confidence": report.confidence,
        "structured": report.structured,
    }
    if summary and notes:
        payload["system_notes"] = notes
    return payload


def _result_from_error(exc: BaseException, label: str) -> dict[str, Any]:
    """子代理沒做完的結構化結果（FAILED）。

    逾時與模型錯誤用固定說法，附上已做到的部分（ADR-0318）；其他例外攜遮罩後的例外摘要。
    """
    status = "FAILED"
    if isinstance(exc, SubagentTimeoutError):
        reason = f"逾時：超過約 {_limit_minutes()} 分鐘沒做完，已中止。"
    elif isinstance(exc, SubagentModelError):
        reason = f"模型錯誤（{exc.error_type}）：子代理沒有做完。"
    elif isinstance(exc, SubagentCancelledError):
        status = "CANCELLED"
        reason = "已取消：這段對話被按了停止，子代理跟著停下。"
    else:
        return {
            "task_id": label,
            "status": "FAILED",
            "result_summary": sanitize_error_text(f"{type(exc).__name__}: {exc}"),
        }
    partial = _keep_tail(parse_subagent_report(exc.partial).summary)
    return {
        "task_id": label,
        "status": status,
        "result_summary": f"{reason}\n{_PARTIAL_HEADER}\n{partial}" if partial else reason,
    }


def _outcome(payload: dict[str, Any]) -> SubagentOutcome:
    """``_OUTCOME_WORDS`` 的鍵：被停算 cancelled；沒跑完或自評失敗算 failed，回報 done 才 done。"""
    if payload.get("status") == "CANCELLED":
        return "cancelled"
    if payload.get("status") != "DONE" or payload.get("report_status") == "failed":
        return "failed"
    return "done" if payload.get("report_status") == "done" else "partial"


async def _run_and_deliver(
    *,
    token: str,
    goal: str,
    parent_identity: AgentIdentity | None,
    parent_request_id: str,
    base_ctx: Any,
    model: str | None,
    task_id: str | None,
    read_only: bool = False,
    parent_stream_id: str | None = None,
) -> None:
    """背景執行子 loop，無論成敗一律 deliver（in-process 直呼，等待者絕不空等）。

    Phase 4（需求 3）：deliver 後**雙寫耐久落盤**（requester vault），使結果跨重啟
    仍可重讀——記憶體 Event 重啟即失、但結果不失（``await_dispatch_result`` 耐久優先）。
    """
    label = task_id or "subagent"
    notices: list[dict[str, Any]] = []
    try:
        text = await _run_subagent(
            goal=goal,
            parent_identity=parent_identity,
            parent_request_id=parent_request_id,
            base_ctx=base_ctx,
            model=model,
            read_only=read_only,
            label=label,
            notices=notices,
            parent_stream_id=parent_stream_id,
        )
        payload = _result_from_text(text, label, notices)
    except asyncio.CancelledError:
        # 整個背景 task 被取消（例如 Touka 關機）：照樣交回「已取消」，等的一方不空等（ADR-0318）
        _deliver(
            token,
            parent_identity,
            {"task_id": label, "status": "CANCELLED", "result_summary": _SHUTDOWN_NOTE},
        )
        raise
    except Exception as exc:
        _log_failure(exc, "in-process subagent (token=%s…)", token[:8])
        payload = _result_from_error(exc, label)
    session_id = base_ctx.get("session_id")
    # 下一輪列「最近結束」；沒有對話（IDE）分不出是哪個視窗，同「還在跑」不列；無痕不留痕
    if session_id and not base_ctx.get("incognito"):
        parent = parent_identity.agent_id if parent_identity else "unknown"
        finished_at = get_current_epoch()
        subagent_registry.record_finished(
            FinishedSubagent(label, parent, session_id, finished_at, _outcome(payload), token)
        )
    _deliver(token, parent_identity, payload)


def _log_failure(exc: Exception, what: str, *args: Any) -> None:
    """被停是使用者的決定，只記一行；其他失敗留完整追蹤。"""
    if isinstance(exc, SubagentCancelledError):
        logger.info(what + " stopped with its conversation", *args)
    else:
        logger.exception(what + " failed", *args)


def _deliver(token: str, parent_identity: AgentIdentity | None, payload: dict[str, Any]) -> None:
    get_callback_store().deliver_result(token, payload)
    _persist_durable(parent_identity, token, payload)


def _persist_durable(
    parent_identity: AgentIdentity | None, token: str, payload: dict[str, Any]
) -> None:
    """耐久落盤（best-effort）：身分缺失或落盤失敗都不得中斷交付主流程。"""
    if parent_identity is None:
        return
    try:
        from src.services.subagent_results import persist_result  # noqa: LOCAL_IMPORT_BAN

        persist_result(parent_identity, token, payload)
    except Exception:  # noqa: BLE001 — 耐久是韌性加值，失敗不得沉掉已交付的結果
        logger.warning("durable dispatch-result persist failed (token=%s…)", token[:8])


# --------------------------------------------------------------------------- #
# Phase 4：多 subagent 並行 fan-out orchestrator（需求 1 完整）
# --------------------------------------------------------------------------- #


async def run_subagent_group(
    *,
    subtasks: list[dict[str, Any]],
    parent_identity: AgentIdentity | None,
    parent_request_id: str,
    base_ctx: Any,
    model: str | None,
    concurrency: int = _DEFAULT_GROUP_CONCURRENCY,
    parent_stream_id: str | None = None,
) -> list[dict[str, Any]]:
    """同時派多個 in-process 子代理、並發上限內各跑各的、匯總結構化結果.

    並發以 ``asyncio.Semaphore`` 封頂（防資源爆炸）；單一子代理崩潰不沉全組
    （其結果標 FAILED）。重用 Phase 1 ``_run_subagent``＋Phase 2 回報解析。
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(index: int, spec: dict[str, Any]) -> dict[str, Any]:
        label = str(spec.get("task_id") or f"sub{index + 1}")
        notices: list[dict[str, Any]] = []
        async with sem:
            try:
                text = await _run_subagent(
                    goal=str(spec.get("goal", "")),
                    parent_identity=parent_identity,
                    parent_request_id=f"{parent_request_id}:grp{index + 1}",
                    base_ctx=base_ctx,
                    model=(spec.get("sub_model") or "").strip() or model,
                    read_only=_is_read_only(spec.get("read_only")),
                    label=label,
                    notices=notices,
                    parent_stream_id=parent_stream_id,
                )
                return _result_from_text(text, label, notices)
            except Exception as exc:  # noqa: BLE001 — 隔離：一子失敗不沉全組
                _log_failure(exc, "group subagent '%s'", label)
                return _result_from_error(exc, label)

    return await asyncio.gather(*(_one(i, spec) for i, spec in enumerate(subtasks)))


def _can_await_later(ctx: Any) -> bool:
    """這一輪列了 mission_tools 才收得到憑證；沒有工具清單（除錯執行器、舊測試）照舊回憑證。"""
    advertised = ctx.get("advertised_tools")
    if not isinstance(advertised, AdvertisedTools):
        return True
    return "mission_tools" in advertised.native


async def _needs_turn(
    ctx: Any, identity: AgentIdentity | None, read_only: bool, stream_id: str | None
) -> bool:
    """能在專案裡跑指令的子代理一律等它做完：先回憑證的話，那一輪一結束指令就被收掉（ADR-0333）."""
    if not subagent_project_commands(ctx, dedicated=True, read_only=read_only, stream_id=stream_id):
        return False  # 不必查專案
    return (await _subagent_scope(ctx, identity, read_only, stream_id)).project_commands


async def _dispatch_and_wait(
    *,
    goal: str,
    identity: AgentIdentity | None,
    request_id: str,
    base_ctx: Any,
    model: str | None,
    task_id: str | None,
    read_only: bool,
    parent_stream_id: str | None = None,
    why: str = _NO_BOARD_WAIT,
) -> str:
    """沒有看板工具時：等子代理做完，直接把結果放進工具回覆（ADR-0316 PR 2 補記）。

    和 ``run_subagent_group`` 同一條路（同一個 ``_run_subagent``）；不產生憑證、不寫耐久結果。
    """
    label = task_id or "subagent"
    notices: list[dict[str, Any]] = []
    try:
        text = await _run_subagent(
            goal=goal,
            parent_identity=identity,
            parent_request_id=str(request_id or "req"),
            base_ctx=base_ctx,
            model=model,
            read_only=read_only,
            label=label,
            notices=notices,
            parent_stream_id=parent_stream_id,
        )
        payload = _result_from_text(text, label, notices)
    except Exception as exc:
        # 和非同步分支一樣，失敗也交回結構化結果（FAILED）
        _log_failure(exc, "in-process subagent (sync dispatch)")
        payload = _result_from_error(exc, label)
    return tool_errors.build_tool_success(
        f"子代理（{label}）已結束：{_OUTCOME_WORDS[_outcome(payload)]}。{why}",
        result=payload,
        dispatch_mode="in-process-sync",
    )


# --------------------------------------------------------------------------- #
# 工具本體
# --------------------------------------------------------------------------- #


async def dispatch_subagent(
    goal: str = "",
    sub_model: str | None = None,
    task_id: str | None = None,
    identity: AgentIdentity | None = None,
    request_id: str = "",
    model: str | None = None,
    _ctx: Any = None,
    read_only: Any = None,
    **_kwargs: Any,
) -> str:
    """派出一個 in-process 子代理非同步執行單一自包含任務.

    立即回傳 ``callback_token``；主 agent 之後以
    ``mission_tools(action="await_dispatch_result", callback_token=...)`` 等待結果。
    這一輪沒列 ``mission_tools`` 時改成等子代理做完、直接帶回結果（``_dispatch_and_wait``）。

    Args:
        goal: 子任務目標與完整指示（子代理只看得到這段，必須自包含）。
        sub_model: （選）子代理底層模型；預設沿用主 agent 目前的模型。
        task_id: （選）對應 mission board 的 task ID，僅作結果標記。
        identity: 主 agent 身分（executor 注入）。
        request_id: 父請求 ID（executor 注入）；子 loop 用衍生 ``{parent}:sub{n}``。
        model: 主 agent 目前模型（executor 注入）＝ ``sub_model`` 未指定時的預設。
        _ctx: 父 PipelineContext（executor 注入）；缺失即回結構化 internal_error。
        read_only: （選）true＝子代理只能查看，不能修改任何檔案。
        **_kwargs: 其餘 executor 注入參數（不使用）。
    """
    if not str(goal or "").strip():
        return tool_errors.build_tool_error(
            tool_errors.VALIDATION_ERROR,
            "dispatch_subagent requires a non-empty 'goal'.",
            suggestion="以自包含的任務指示重呼（子代理看不到你的對話上下文）。",
        )
    if _ctx is None:
        return tool_errors.build_tool_error(
            tool_errors.INTERNAL_ERROR,
            "dispatch_subagent did not receive the pipeline context (_ctx).",
            suggestion="回報使用者：tool executor 未注入 _ctx，無法開子代理。",
        )
    # 派它的那一輪：在第一個 await 之前記下（ADR-0333）
    parent_stream_id = current_stream_id(_ctx)

    board = _can_await_later(_ctx)
    commands = board and await _needs_turn(
        _ctx, identity, _is_read_only(read_only), parent_stream_id
    )
    if commands or not board:
        return await _dispatch_and_wait(
            why=_COMMANDS_WAIT if commands else _NO_BOARD_WAIT,
            goal=str(goal),
            identity=identity,
            request_id=request_id,
            base_ctx=_ctx,
            model=(sub_model or "").strip() or (model or "").strip() or None,
            task_id=task_id,
            read_only=_is_read_only(read_only),
            parent_stream_id=parent_stream_id,
        )

    store = get_callback_store()
    token = store.generate_token()
    # 站內憑證：只收 Touka 內部交回的結果，逾時不作廢（ADR-0318）
    store.register(token, in_process=True)

    # 經 spawn_logged：正常關機時 cancel_all_background_tasks 會取消它，交回「已取消」（ADR-0323）
    spawn_logged(
        _run_and_deliver(
            token=token,
            goal=str(goal),
            parent_identity=identity,
            parent_request_id=str(request_id or "req"),
            base_ctx=_ctx,
            model=(sub_model or "").strip() or (model or "").strip() or None,
            task_id=task_id,
            read_only=_is_read_only(read_only),
            parent_stream_id=parent_stream_id,
        ),
        name=f"subagent:{token[:8]}",
    )

    label = task_id or "subagent"
    return tool_errors.build_tool_success(
        f"已在 Touka 內部派出子代理（{label}）執行中。",
        callback_token=token,
        dispatch_mode="in-process",
        next_step=(
            "用 mission_tools(action='await_dispatch_result', "
            "callback_token='<token>') 等待結果；期間可先做其他事。"
        ),
    )


async def dispatch_subagent_group(
    subtasks: list[dict[str, Any]] | None = None,
    identity: AgentIdentity | None = None,
    request_id: str = "",
    model: str | None = None,
    _ctx: Any = None,
    **_kwargs: Any,
) -> str:
    """同時派多個 in-process 子代理各跑各的，**阻塞至全部完成**後匯總回報.

    適合把一個大任務拆成數個彼此獨立的子任務並行完成再收斂。並發有上限、單次
    子任務數有上限（防無限展開）；單一子代理失敗不沉全組。需要「先派了先做別的」
    的非阻塞語義時，改用 ``dispatch_subagent``（逐一 token）。

    Args:
        subtasks: 子任務清單，每項 ``{goal, task_id?, sub_model?, read_only?}``；goal 必須自包含。
        identity: 主 agent 身分（executor 注入）。
        request_id: 父請求 ID（executor 注入）。
        model: 主 agent 目前模型（executor 注入）＝子任務未指定 sub_model 時的預設。
        _ctx: 父 PipelineContext（executor 注入）；缺失即回結構化 internal_error。
        **_kwargs: 其餘 executor 注入參數（不使用）。
    """
    specs = subtasks or []
    if not specs or not all(str((s or {}).get("goal", "")).strip() for s in specs):
        return tool_errors.build_tool_error(
            tool_errors.VALIDATION_ERROR,
            "dispatch_subagent_group requires a non-empty 'subtasks', each with a 'goal'.",
            suggestion="每個子任務給自包含的 goal（子代理看不到你的對話上下文）。",
        )
    if len(specs) > _MAX_GROUP:
        return tool_errors.build_tool_error(
            tool_errors.VALIDATION_ERROR,
            f"too many subtasks ({len(specs)} > {_MAX_GROUP}); split into smaller groups.",
            suggestion=f"單次最多 {_MAX_GROUP} 個並行子任務；超出請分批。",
        )
    if _ctx is None:
        return tool_errors.build_tool_error(
            tool_errors.INTERNAL_ERROR,
            "dispatch_subagent_group did not receive the pipeline context (_ctx).",
            suggestion="回報使用者：tool executor 未注入 _ctx，無法開子代理。",
        )

    results = await run_subagent_group(
        subtasks=specs,
        parent_identity=identity,
        parent_request_id=str(request_id or "req"),
        base_ctx=_ctx,
        model=(model or "").strip() or None,
        concurrency=_DEFAULT_GROUP_CONCURRENCY,
        parent_stream_id=current_stream_id(_ctx),  # 等名額之前就記下派它的那一輪
    )
    counts = Counter(_outcome(r) for r in results)
    tally = "／".join(f"{counts[key]} {word}" for key, word in _OUTCOME_WORDS.items())
    return tool_errors.build_tool_success(
        f"並行子代理結束：{tally}（共 {len(results)}）。",
        completed=counts["done"],
        partial=counts["partial"],
        failed=counts["failed"],
        cancelled=counts["cancelled"],
        results=results,
    )
