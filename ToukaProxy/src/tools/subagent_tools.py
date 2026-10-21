"""In-process 子代理派發（in-process sub-agent orchestration 提案 Phase 1 MVP）.

``dispatch_subagent``：主 agent 在 ReAct loop 內派「一個」in-process 子代理跑單一
自包含任務——任務不外派給外部 AI，整個在 Touka 內部完成。

設計要點（對應提案 §9 Phase 1 邊界）：
- **非同步 token 形**：立即回結構化成功（含 ``callback_token``），背景 asyncio task
  開子 loop；完成（或失敗）後 ``DispatchCallbackStore.deliver_result`` **in-process
  直呼**（繞過 HTTP 回呼路由），喚醒既有 ``mission_tools(action="await_dispatch_result")``
  ——等待工具零改動重用（ADR-0051 語義不變）。
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
import itertools
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from src.adapters.schemas import AnthropicRequest
from src.models.advertised_tools import AdvertisedTools
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import tool_errors
from src.services.dispatch_callback_store import get_callback_store
from src.services.dispatchers.factory import get_stream_generator
from src.services.model_manager import model_manager
from src.services.subagent_report import REPORT_INSTRUCTION, parse_subagent_report
from src.tools.contracts import MAX_SUBAGENT_GROUP, SUBAGENT_GROUP_CONCURRENCY
from src.utils.auth_mode import enforce_incognito_manual
from src.utils.brain_persistence import get_active_brain
from src.utils.time_utils import get_current_epoch
from src.utils.user_intent import subagent_intent

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常數
# --------------------------------------------------------------------------- #

_SUB_PREFIX = "__subagent_"
_SUB_SEQ = itertools.count(1)
_SUB_TIMEOUT_SECONDS = 600.0
_RESULT_SUMMARY_MAX = 4000
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

# 背景任務強引用：僅存 Task 物件防 GC 中途取消（asyncio 官方建議模式）。
_BACKGROUND_TASKS: set[asyncio.Task] = set()


# --------------------------------------------------------------------------- #
# 內部：子代理請求組裝與子 loop 執行
# --------------------------------------------------------------------------- #


def _subagent_user_prompt(goal: str) -> str:
    """子代理的自包含任務指示（子代理看不到主對話上下文）。"""
    return (
        "你是 Touka 的 in-process 子代理，負責完成以下單一任務。\n"
        "完成後請直接輸出最終結果全文（會原樣回傳給主代理），"
        "不要提問、不要等待後續指示。\n\n"
        f"【任務】\n{goal.strip()}\n\n"
        f"{REPORT_INSTRUCTION}"
    )


def _derive_context(base_ctx: Any) -> PipelineContext:
    """從父 ctx 衍生子 loop ctx（淺複製共享 client 連線，覆寫身分/會話欄位）。"""
    if isinstance(base_ctx, PipelineContext):
        return base_ctx.model_copy()
    return PipelineContext(**dict(base_ctx or {}))


def _mark_subagent_run(ctx: PipelineContext, base_ctx: Any, goal: str) -> None:
    """子代理一律無人在場（U2c-2）。

    子 loop 只收文字、審批卡送不到人：要人核准的動作立刻拒絕或等人確認，不等 180 秒
    逾時。原意只帶父代理的人類原意；派工目標是代理人轉述，只留在 ctx、不進審查員的輸入
    （R-2c ③ #4）。
    """
    ctx["subagent_run"] = True
    ctx["dispatch_run"] = True
    ctx["subagent_goal"] = goal
    ctx["user_intent"] = subagent_intent(base_ctx.get("user_intent"))


async def _assemble_subagent_request(
    *,
    goal: str,
    sub_identity: AgentIdentity,
    base_ctx: Any,
    model: str | None,
) -> AnthropicRequest:
    """跑真實 ProxyPipeline 為子代理組裝請求（ADR-0133 模式），後剝除遞迴能力。

    pipeline 依 ``sub_identity``（Subagent）注入相符的工具面與系統塊；組裝完成後
    （a）若指定 ``model`` 則強制之（不依賴未知臨時身分的 brain 解析），
    （b）剝除 ``_SUBAGENT_STRIPPED_TOOLS``——子代理不可再派子代理或群組（能力閘）。
    """
    from src.services.proxy_service import pipeline  # noqa: LOCAL_IMPORT_BAN

    # 不寫 max_tokens：Claude 用 AnthropicRequest 的預設 8192，其他家不帶上限。
    # convert_to_openai 會轉送呼叫端明確給的值（O20，給 IDE 的 Anthropic 入口；ADR-0254）。
    request = AnthropicRequest(
        model=(model or "").strip() or sub_identity.agent_id,
        messages=[{"role": "user", "content": _subagent_user_prompt(goal)}],
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
    _mark_subagent_run(ctx, base_ctx, goal)

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


async def _drain_text(stream: AsyncGenerator[str]) -> str:
    """抽乾子 loop SSE 串流，累積文字 delta（OpenAI／Anthropic 雙形，仿 group.py）。"""
    parts: list[str] = []
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
) -> str:
    """開一個 in-process 子 ReAct loop 跑到完成，回傳最終文字產出。"""
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

    request = await _assemble_subagent_request(
        goal=goal, sub_identity=sub_identity, base_ctx=base_ctx, model=model
    )

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
    _mark_subagent_run(ctx, base_ctx, goal)
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
    return await asyncio.wait_for(_drain_text(stream), timeout=_SUB_TIMEOUT_SECONDS)


def _result_from_text(text: str, label: str) -> dict[str, Any]:
    """把子代理最終文字（含 Phase 2 回報塊）收斂成結構化結果 payload（DONE）。

    傳輸層 status（子 loop 是否跑完）與自我宣稱層 report_status 分開承載。
    """
    report = parse_subagent_report(text)
    summary = report.summary[:_RESULT_SUMMARY_MAX]
    if len(report.summary) > _RESULT_SUMMARY_MAX:
        summary += "\n…（結果過長已截斷）"
    return {
        "task_id": label,
        "status": "DONE",
        "result_summary": summary or "(子代理無文字產出)",
        "report_status": report.status,
        "claims": report.claims,
        "artifact_refs": report.artifact_refs,
        "confidence": report.confidence,
        "structured": report.structured,
    }


def _result_from_error(exc: BaseException, label: str) -> dict[str, Any]:
    """子 loop 崩潰的結構化結果 payload（FAILED，攜例外摘要）。"""
    return {
        "task_id": label,
        "status": "FAILED",
        "result_summary": f"{type(exc).__name__}: {exc}",
    }


async def _run_and_deliver(
    *,
    token: str,
    goal: str,
    parent_identity: AgentIdentity | None,
    parent_request_id: str,
    base_ctx: Any,
    model: str | None,
    task_id: str | None,
) -> None:
    """背景執行子 loop，無論成敗一律 deliver（in-process 直呼，等待者絕不空等）。

    Phase 4（需求 3）：deliver 後**雙寫耐久落盤**（requester vault），使結果跨重啟
    仍可重讀——記憶體 Event 重啟即失、但結果不失（``await_dispatch_result`` 耐久優先）。
    """
    label = task_id or "subagent"
    try:
        text = await _run_subagent(
            goal=goal,
            parent_identity=parent_identity,
            parent_request_id=parent_request_id,
            base_ctx=base_ctx,
            model=model,
        )
        payload = _result_from_text(text, label)
    except Exception as exc:
        logger.exception("in-process subagent failed (token=%s…)", token[:8])
        payload = _result_from_error(exc, label)

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
) -> list[dict[str, Any]]:
    """同時派多個 in-process 子代理、並發上限內各跑各的、匯總結構化結果.

    並發以 ``asyncio.Semaphore`` 封頂（防資源爆炸）；單一子代理崩潰不沉全組
    （其結果標 FAILED）。重用 Phase 1 ``_run_subagent``＋Phase 2 回報解析。
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(index: int, spec: dict[str, Any]) -> dict[str, Any]:
        label = str(spec.get("task_id") or f"sub{index + 1}")
        async with sem:
            try:
                text = await _run_subagent(
                    goal=str(spec.get("goal", "")),
                    parent_identity=parent_identity,
                    parent_request_id=f"{parent_request_id}:grp{index + 1}",
                    base_ctx=base_ctx,
                    model=(spec.get("sub_model") or "").strip() or model,
                )
                return _result_from_text(text, label)
            except Exception as exc:  # noqa: BLE001 — 隔離：一子失敗不沉全組
                logger.exception("group subagent '%s' failed", label)
                return _result_from_error(exc, label)

    return await asyncio.gather(*(_one(i, spec) for i, spec in enumerate(subtasks)))


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
    **_kwargs: Any,
) -> str:
    """派出一個 in-process 子代理非同步執行單一自包含任務.

    立即回傳 ``callback_token``；主 agent 之後以
    ``mission_tools(action="await_dispatch_result", callback_token=...)`` 等待結果。

    Args:
        goal: 子任務目標與完整指示（子代理只看得到這段，必須自包含）。
        sub_model: （選）子代理底層模型；預設沿用主 agent 目前的模型。
        task_id: （選）對應 mission board 的 task ID，僅作結果標記。
        identity: 主 agent 身分（executor 注入）。
        request_id: 父請求 ID（executor 注入）；子 loop 用衍生 ``{parent}:sub{n}``。
        model: 主 agent 目前模型（executor 注入）＝ ``sub_model`` 未指定時的預設。
        _ctx: 父 PipelineContext（executor 注入）；缺失即回結構化 internal_error。
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

    store = get_callback_store()
    token = store.generate_token()
    store.register(token)

    task = asyncio.create_task(
        _run_and_deliver(
            token=token,
            goal=str(goal),
            parent_identity=identity,
            parent_request_id=str(request_id or "req"),
            base_ctx=_ctx,
            model=(sub_model or "").strip() or (model or "").strip() or None,
            task_id=task_id,
        ),
        name=f"subagent:{token[:8]}",
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

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
        subtasks: 子任務清單，每項 ``{goal, task_id?, sub_model?}``；goal 必須自包含。
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
    )
    completed = sum(1 for r in results if r.get("status") == "DONE")
    failed = len(results) - completed
    return tool_errors.build_tool_success(
        f"並行子代理完成：{completed} 成功／{failed} 失敗（共 {len(results)}）。",
        completed=completed,
        failed=failed,
        results=results,
    )
