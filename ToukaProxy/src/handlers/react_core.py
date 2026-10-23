"""ReAct 迴圈核心（react-core-extraction；單一實作＝SSOT）。

曾在三 handler 各養一份的狀態機（ADR-0155 修掉六個漂移 bug 的病灶）收攏於此：
身分回填／ADR-0146 工具輪保險絲／雙中斷閘（**刻意不對稱**：呼叫前中斷不發遙測、
回合後中斷發遙測）／react_slots 進度／工具 fan-out／guard 呼叫／C4 計時／遙測收尾
／**唯一一份 SSE 方言**（ADR-0081 重播＋Guard #38 封套＋ADR-0152 reasoning 頻道）。

供應商差異一律經 ``ProviderAdapter``（`src.handlers.adapters.base`）進出；
行為凍結由特徵化 golden 套件把關（tests/proxy/characterization）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from src.models.interjection import Interjection
    from src.services.interjection_queue import InterjectionQueue

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.session_persistence import UiGenerationStopped
from src.services.autonomy import autonomy_service
from src.services.context_breakdown import compute_context_breakdown
from src.services.guard_service import guard_service
from src.services.interjection_queue import (
    interjection_queue,
    round_interrupted_by_user_event,
)
from src.services.permission_service import permission_service
from src.services.rhythm_service import rhythm_service
from src.services.stream_buffer_service import stream_buffer_service
from src.services.telemetry import (
    accumulate_token_usage,
    calculate_emergence_index,
    is_long_prompt,
    log_usage_audit,
    run_cost_usd,
)
from src.services.tool_executor import ToolExecutor
from src.utils.progress_events import react_slots_event, token_usage_event
from src.utils.sse import sse_line
from src.utils.time_utils import get_current_epoch
from src.utils.tool_usage import ToolUsage

from .adapters.base import ProviderAdapter, RoundError, Turn  # noqa: TC001
from .adapters.usage import PartialUsage
from .interjections import InterjectionRound

logger = logging.getLogger(__name__)

_KEEPALIVE_S = 5.0  # 上游未回應時的 SSE 心跳間隔（ADR-0157 K1：原 claude-only 升三家）
# 3b-U5a 主迴圈重試（附註 3）：同一模型最多送幾次（四家共用；人工 live probe 設 1＝不重試）。
MAX_SEND_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0  # 第 n 次重試前等 n×2 秒（沿用 Gemini 舊階梯）；廠商給 retry-after 就照它
# 下游終端 chunk 的 finish_reason（停止原因 error＝缺欄或未知值，維持 stop）。
_FINISH_REASONS = {"max_tokens": "length", "safety": "content_filter", "refusal": "content_filter"}
_SAFETY_NOTICE = "\n\n⚠️ 回應被廠商的安全過濾擋下。"
_REFUSAL_NOTICE = "\n\n⚠️ 模型拒絕回答。"


async def _retry_sleep(seconds: float) -> None:
    """重試前的等待（測試替換點）；取消照常往外傳。"""
    await asyncio.sleep(seconds)


async def _backoff(seconds: float) -> AsyncGenerator[Any]:
    """退避等待包成沒有項目的一輪交給 pump，等待期間照樣發心跳（ADR-0157 K1；R-3b ② L1）。"""
    await _retry_sleep(seconds)
    for _beat in ():  # 沒有項目，只為了讓這個函式是 async generator
        yield _beat


@dataclass
class _RunUsage:
    """整次 run 的用量帳（ADR-0244）：四欄逐輪加總＋最後一輪的總輸入（context 佔用）。

    ``long_totals``＝落在長提示檔那幾輪的加總（r55）：整次金額每輪照自己的檔位算再相加。
    """

    model: str | None = None
    totals: dict[str, int] = field(default_factory=dict)
    long_totals: dict[str, int] = field(default_factory=dict)
    last_input: int = 0
    estimated: bool = False  # 有任何一輪是估的（3b 裁定 6；標記只進畫面事件與日誌）

    def add(self, usage: dict[str, int], total_input: int) -> None:
        long = is_long_prompt(usage, model=self.model)
        for key, value in usage.items():
            self.totals[key] = self.totals.get(key, 0) + value
            if long:
                self.long_totals[key] = self.long_totals.get(key, 0) + value
        self.last_input = total_input

    @property
    def cost_usd(self) -> float:
        """整次 run 金額（畫面事件與稽核 CSV 共用）。"""
        return run_cost_usd(self.totals, self.long_totals, model=self.model)

    @property
    def total_tokens(self) -> int:
        """畫面事件的 token 數＝未快取輸入＋快取讀＋快取寫＋輸出（四欄相加）。"""
        return sum(self.totals.values())


@dataclass(frozen=True)
class RoundInterrupted:
    """pump 的最後一項：Ctrl+Enter 打斷這一輪（第 4b 段）；``started``＝已經送出。"""

    started: bool


async def pump_send_round(
    round_gen: Any, urgent: asyncio.Event | None = None
) -> AsyncGenerator[Any]:
    """抽乾 adapter 的 send_round 並於上游久候時發 SSE 心跳（K1＝核心單一實作）。

    每 ``_KEEPALIVE_S`` 秒無新項即 yield 一行 SSE heartbeat comment（防前端
    超時斷線）；其餘項目原樣透傳。原為 claude adapter 內建（wait_for+shield
    輪詢），推廣三家時**升核心**而非複製三份——三家的慢上游（openai 長 POST/
    gemini 503 退避/claude SDK 呼叫）自此同享一份實作。收流或被取消時先等那一輪結束（上游
    已關）再 aclose round_gen（ADR-0248 D3）。

    ``urgent``（ADR-0249 第 4b 段）：收到本輪結果之前 set＝使用者 Ctrl+Enter，yield
    ``RoundInterrupted`` 就結束，收流時走上面同一條路（取消這一輪、關上游、不回半輪）。
    本輪結果到了之後不再打斷；set 之後又被收回（clear）的不算。
    """
    started = False
    try:
        while True:
            if urgent is not None and urgent.is_set():  # 還沒送出就先到：這一輪不送
                yield RoundInterrupted(started=started)
                return
            started = True
            next_task = asyncio.ensure_future(round_gen.__anext__())
            wake = None if urgent is None else asyncio.ensure_future(urgent.wait())
            try:
                while not next_task.done():
                    watch = {next_task} if wake is None else {next_task, wake}
                    done, _pending = await asyncio.wait(
                        watch, timeout=_KEEPALIVE_S, return_when=asyncio.FIRST_COMPLETED
                    )
                    if next_task in done:
                        break
                    if urgent is not None and urgent.is_set():  # Ctrl+Enter 先到
                        yield RoundInterrupted(started=True)
                        return
                    if urgent is not None and wake is not None and wake.done():
                        wake = asyncio.ensure_future(urgent.wait())  # 被收回了：繼續等
                    elif not done:
                        yield ": heartbeat\n\n"
            finally:
                # 消費端提前收流（GeneratorExit）或被取消時不遺留孤兒任務
                leftovers = {t for t in (next_task, wake) if t is not None and not t.done()}
                for task in leftovers:
                    task.cancel()
                if leftovers:
                    await asyncio.wait(leftovers)
            try:
                item = next_task.result()
            except StopAsyncIteration:
                return
            if not isinstance(item, str):
                urgent = None  # 本輪結果已到：之後不再打斷
            yield item
    finally:
        await round_gen.aclose()


async def run_react_loop(  # noqa: PLR0915
    adapter: ProviderAdapter,
    request: Any,
    context: dict[str, Any],
    *,
    interjections: InterjectionQueue = interjection_queue,
    stream_state: Callable[[str], dict[str, Any] | None] = stream_buffer_service.get_state,
) -> AsyncGenerator[str]:
    """以 provider adapter 驅動統一 ReAct 迴圈（自 openai_handler 移植，行為凍結）。

    ``interjections``／``stream_state``：插話佇列與串流查詢（ADR-0249 第 4a 段；測試替換點）。
    第 4b 段：接收上游時 Ctrl+Enter 打斷這一輪（見 ``pump_send_round``）。
    """
    protocol_manager = context.get("protocol_manager")
    tool_registry = context.get("tool_registry")
    memory_tool_registry = context.get("memory_tool_registry")
    plugin_registry = context.get("plugin_registry")
    request_id = context.get("request_id")
    t0 = context.get("t0", get_current_epoch())
    agent_id = context.get("agent_id")
    identity = context.get("identity")

    # 確保 identity 物件存在 (Identity SSOT)
    if not identity and agent_id:
        role = permission_service.get_role_key(AgentIdentity(agent_id=agent_id))
        identity = AgentIdentity.for_role(agent_id, role)

    # 預設 fail-closed（ADR-0155 C5/D8；生產 ctx＝PipelineContext 欄位恆存在）。
    is_toukasoul = context.get("is_toukasoul", False)
    enable_autonomy = context.get("enable_autonomy", False)

    # 只要是靈魂或者是獲得授權的自主任務，都視為允許執行 ReAct
    allow_react = is_toukasoul or enable_autonomy
    tool_executor = ToolExecutor(
        protocol_manager,
        tool_registry,
        memory_tool_registry,
        plugin_registry,
        enforce_advertised=True,
    )
    # 模型迴圈只執行這一輪列給模型的工具：開關沒開就在送出上游之前停下（不用 assert）。
    # 用真假值判斷，不寫 `is not True`：既有測試會把執行器換成 MagicMock。
    if not tool_executor.enforce_advertised:
        logger.error("Model-loop ToolExecutor built without advertised-tool enforcement")
        raise ToukaException(ErrorCode.SYSTEM_ERROR)

    session_id = context.get("session_id")

    # --- [SSOT: 初始化工具輪預算（ADR-0146）] ---
    # 每個 run 都初始化：互動預設不限（0）、無人（enable_autonomy＝heartbeat 鏈路）
    # 一律有限（排程 pulse_rounds＞全域保險絲＞保底 50）。每日配額改由 heartbeat
    # 在 pulse 派發時扣（互動聊天不再消耗自主配額）。
    _headless = bool(context.get("enable_autonomy"))
    _rounds = rhythm_service.get_effective_rounds(
        autonomy_service.config, context.get("pulse_rounds"), headless=_headless
    )
    rhythm_service.initialize_session(request_id, _rounds)

    state = await adapter.prepare_request(request, context)
    # 遙測/計費標籤歸 adapter（claude＝ingress 轉換後的實名，餘家＝request.model）；
    # prepare 後即固定，每輪入帳與 run 結束共用。
    provider = adapter.resolve_provider_label(state)
    usage_model = adapter.telemetry_model(state, request)
    run_usage = _RunUsage(model=usage_model)
    # ADR-0249 第 4a 段：插話只在 UI 對話取出；佇列空的時候什麼都不送（golden 零差異）
    interjection_round = InterjectionRound.begin(context, interjections, stream_state)
    restore_tool_choice: Callable[[], None] | None = None  # urgent 那一輪之後還原
    tool_step = 0

    def interjection_finish() -> list[str]:
        """回合結束時還沒送達的插話（只送一次）。"""
        return interjection_round.finish() if interjection_round is not None else []

    # 第 4b 段：接收上游時同時等 Ctrl+Enter（只有 UI 對話拿得到）
    urgent = interjection_round.urgent_signal() if interjection_round is not None else None

    def deliver(items: list[Interjection], *, interrupted: bool = False) -> None:
        """取出的插話接進 state；有 urgent 就讓下一輪 tool_choice=none（已經壓著就不再壓）。"""
        nonlocal restore_tool_choice
        if not items:
            return
        adapter.append_interjection(state, items, interrupted=interrupted)
        if restore_tool_choice is None and any(item.urgent for item in items):
            restore_tool_choice = adapter.hold_tool_choice(state, "none")

    all_thinking_and_tool_logs = ""
    actually_used_tools = ToolUsage()  # 試過／執行過／存成功由執行器記（T3）
    final_raw: Any = {}
    turn = Turn()
    wrapping_up = False  # ADR-0146：額度耗盡後的「最後一次無工具收尾呼叫」旗標
    t1 = get_current_epoch()
    # C4（ADR-0155 D5，拍板 B）：pure_gen_ms＝各輪上游呼叫耗時累加（送出→回應到達，
    # 不含工具執行/本地處理）——三家同語意可比。
    upstream_gen_s = 0.0

    def yield_delta(content: str) -> str:
        """將內容片段包裝為 OpenAI 相容的 SSE chunk 格式（下游 wire 凍結）。"""
        chunk = {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion.chunk",
            "created": int(get_current_epoch()),
            "model": state.chunk_model,
            "choices": [{"delta": {"content": content}, "index": 0, "finish_reason": None}],
        }
        return sse_line(chunk)

    def yield_reasoning_delta(reasoning: str) -> str:
        """思考片段走獨立頻道 ``delta.reasoning_content``（ADR-0152 思考分頻）。

        沿用 DeepSeek 串流慣例的欄位名——OpenAI 相容 chunk 形狀不變（additive），
        舊前端忽略未知 delta 欄位＝零破壞；ToukaChat 據此摺疊顯示而非混入正文。
        """
        chunk = {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion.chunk",
            "created": int(get_current_epoch()),
            "model": state.chunk_model,
            "choices": [
                {"delta": {"reasoning_content": reasoning}, "index": 0, "finish_reason": None}
            ],
        }
        return sse_line(chunk)

    def record_partial_usage() -> PartialUsage | None:
        """沒完成的一輪照記（3b 裁定 6；估計標記只進日誌與畫面事件）；失敗不蓋掉取消。"""
        try:
            partial = adapter.partial_usage(state)
            if not isinstance(partial, PartialUsage):
                return None
            accumulate_token_usage(identity, provider, partial.usage, model=usage_model)
            run_usage.add(partial.usage, partial.total_input)
            run_usage.estimated = run_usage.estimated or partial.estimated
        except Exception:
            logger.exception("Unfinished round usage could not be recorded")
            return None
        logger.warning("Unfinished or unreported round usage for %s (estimated=%s): %s",
                       usage_model, partial.estimated, partial.usage)
        return partial

    try:
        while True:
            # 硬中斷：使用者按停 → 在呼叫 LLM 前就停（**刻意不發遙測**，凍結語意）
            if session_id and rhythm_service.is_interrupted(session_id):
                for _line in interjection_finish():
                    yield _line
                if context.get("ui_authorized") is True:
                    raise UiGenerationStopped("cancelled")
                yield yield_delta("\n\n⏹️ 已由使用者中斷。")
                yield "data: [DONE]\n\n"
                return

            t_send = get_current_epoch()
            # send_round＝async generator：str 項＝SSE 事件即時轉發（pump 注入的 keepalive
            # 心跳）；最後的非 str 項＝本輪結果（raw 或 RoundError）。adapter 標成可重試的
            # RoundError 在這裡退避重送（3b-U5a）：被放棄的那次照裁定 6 記帳，等待時被取消
            # 照常往外傳（不再記一次）。
            attempt, stopped = 1, False
            interrupted: RoundInterrupted | None = None
            urgent_items: list[Interjection] = []
            while True:
                raw = None
                try:
                    send = pump_send_round(adapter.send_round(state), urgent)
                    async with aclosing(send) as rounds:
                        async for _item in rounds:
                            if isinstance(_item, str):
                                yield _item
                            elif (isinstance(_item, RoundInterrupted)
                                  and interjection_round is not None):
                                # 同一步取出（中間沒有 await）：之後才到的收回一律 409
                                interrupted = _item
                                urgent_items = interjection_round.drain(
                                    step=tool_step, urgent_first=True)
                                break
                            else:
                                raw = _item
                except (asyncio.CancelledError, GeneratorExit):
                    record_partial_usage()
                    raise
                if interrupted is not None or not (
                        isinstance(raw, RoundError) and raw.retry_note is not None
                        and attempt < MAX_SEND_ATTEMPTS):
                    break
                record_partial_usage()
                note = f"{raw.retry_note}, retrying... (attempt {attempt})"
                logger.warning("Retrying %s round (%s)", usage_model, note)
                yield sse_line({"type": "touka_status", "status": note})
                wait = RETRY_BACKOFF_S * attempt if raw.retry_after is None else raw.retry_after
                async with aclosing(pump_send_round(_backoff(wait))) as beats:
                    async for _beat in beats:
                        yield _beat
                attempt += 1
                # 等完先看使用者有沒有按停（R-3b ② L2）：按了就交給迴圈開頭的硬中斷處理
                if session_id and rhythm_service.is_interrupted(session_id):
                    stopped = True
                    break
            if stopped:
                continue
            if interrupted is not None:
                # 第 4b 段：不是錯誤也不是按停——不重試、不拋 UiGenerationStopped、不算一步；
                # 半截內容（沒有解析）全部丟掉，取出的插話接上去馬上送下一輪
                if interrupted.started:
                    record_partial_usage()
                    yield sse_line(round_interrupted_by_user_event(step=tool_step))
                if not urgent_items:  # 訊號和佇列對不上：這一回合不再理會訊號（防空轉）
                    urgent = None
                deliver(urgent_items, interrupted=True)  # 打斷的範本（不是工具執行期間）
                continue
            if isinstance(raw, RoundError):
                partial = record_partial_usage()
                if partial is not None and adapter.emits_usage_event(state):
                    yield sse_line(token_usage_event(run_usage.total_tokens, run_usage.cost_usd,
                                                     estimated=partial.estimated))
                for _line in interjection_finish():
                    yield _line
                if context.get("ui_authorized") is True:
                    raise UiGenerationStopped("error")
                yield yield_delta(raw.user_message)
                yield "data: [DONE]\n\n"
                return
            upstream_gen_s += get_current_epoch() - t_send  # C4：本輪上游耗時入帳
            # 插話：下一輪送出成功才算送達；urgent 的 tool_choice=none 只維持這一輪
            if interjection_round is not None:
                for _line in interjection_round.delivered():
                    yield _line
            if restore_tool_choice is not None:
                restore_tool_choice()
                restore_tool_choice = None

            turn = adapter.parse_round(state, raw)
            # 每輪 parse 後就入帳（ADR-0244）：下方成本閘看得到本次 run 正在花的錢。
            # 同步寫檔＝讀改寫之間不讓出事件迴圈，同行程的並行 run 不互蓋。
            round_usage, round_input, _ = adapter.extract_usage(state, raw)
            if round_usage:
                accumulate_token_usage(identity, provider, round_usage, model=usage_model)
                run_usage.add(round_usage, round_input)
            else:  # 串流完成卻沒回報用量：照裁定 6 估算，不靜默不入帳（裁定 5）
                record_partial_usage()
            if turn.empty:
                final_raw = raw
                break

            # 思考先於正文發射（時序＝模型實際產出順序，ADR-0152）；逐段＝保真
            # claude 多 thinking block 的 chunk 邊界（openai 單段、gemini 無）。
            for _reasoning_part in turn.reasoning_parts:
                yield yield_reasoning_delta(_reasoning_part)

            # 即時輸出本輪的文字內容（逐段＝保真各方言 chunk 邊界）
            for _text_part in turn.text_parts:
                yield yield_delta(_text_part)

            # 中斷：使用者按停 → 不再執行工具、不再續呼下一輪（發遙測收尾）
            if session_id and rhythm_service.is_interrupted(session_id):
                for _line in interjection_finish():
                    yield _line
                if context.get("ui_authorized") is True:
                    raise UiGenerationStopped("cancelled")
                yield yield_delta("\n\n⏹️ 已由使用者中斷。")
                final_raw = raw
                break

            if not turn.tool_calls:
                # 方言介入點（gemini Honesty Guard，拍板 K2＝維持 gemini-only）：
                # 回 MidLoopAction＝yield 警告後重入迴圈（注入矯正輪）。
                hook = adapter.mid_loop_hook(state, turn, actually_used_tools, allow_react)
                if hook is not None:
                    for _d in hook.deltas:
                        yield yield_delta(_d)
                    continue
                # ADR-0146：純文字＝回合結束（nudge 自主續圈已移除——與主流
                # harness 一致；心跳的多回合由 pulse_count 與模型自覺承擔）。
                final_raw = raw
                break

            # ADR-0146：收尾輪（tool_choice=none）若模型仍吐出工具調用，
            # 一律忽略並以本輪文字作結（防禦性，杜絕收尾迴圈）。
            if wrapping_up:
                final_raw = raw
                break

            # 工具輪預算閘：每輪（一次含工具的模型回合）扣 1；成本閘同點檢查。
            # 耗盡/超額＝本輪工具零執行（fail-closed），改走最後一次無工具收尾呼叫。
            over_budget = autonomy_service.is_over_budget(identity)
            if over_budget or not rhythm_service.consume_round(request_id):
                reason = (
                    "已達總量上限（累計成本）"
                    if over_budget
                    else f"工具輪額度已盡（{rhythm_service.get_total_rounds(request_id)} 輪）"
                )
                yield yield_delta(f"\n\n⚠️ {reason}，進行最後總結。\n")
                adapter.build_denied_turn(state, turn, reason)
                adapter.set_tool_choice(state, "none")
                wrapping_up = True
                continue

            # 有限 run 的結構化進度事件（wire 形狀沿用；不限 run 不發）
            total_rounds = rhythm_service.get_total_rounds(request_id)
            if total_rounds > 0:
                yield sse_line(
                    react_slots_event(
                        current=rhythm_service.get_used_rounds(request_id),
                        total=total_rounds,
                    )
                )

            adapter.append_assistant_turn(state, turn)

            event_queue: asyncio.Queue = asyncio.Queue()
            execution_tasks = []
            for tc in turn.tool_calls:
                task = asyncio.create_task(
                    tool_executor.execute_to_queue(
                        tc.name,
                        tc.args,
                        request_id,
                        context,  # 傳遞完整的 context
                        event_queue,
                        call_id=tc.id,
                        actually_used_tools=actually_used_tools,
                    )
                )
                execution_tasks.append((tc.id, tc.name, task))

            # 監聽事件佇列並轉發
            completed_count = 0
            while completed_count < len(turn.tool_calls):
                event = await event_queue.get()
                if event is None:
                    completed_count += 1
                    continue
                if event["type"] == "log":
                    all_thinking_and_tool_logs += event["content"]
                    yield yield_delta(event["content"])
                else:
                    yield sse_line(event)

            # 收集結果並回報給模型
            results = []
            for call_id, target_name, task in execution_tasks:
                res_text = await task
                results.append((call_id, target_name, res_text))
            await adapter.append_tool_results(state, results)
            # 取出點（ADR-0249 裁定 4）：工具結果寫回之後、進下一輪之前；不在迴圈頂端、
            # 不在重試迴圈裡（重試會重複附加）
            tool_step += 1
            if interjection_round is not None:  # urgent 的 tool_choice 見 deliver（裁定 5）
                deliver(interjection_round.drain(step=tool_step))
            continue
    finally:
        await adapter.close(state)

    for _line in interjection_finish():  # 回合結束：還沒送達的回給前端（[DONE] 之前）
        yield _line

    # 3b-U5a：截斷、拒答、安全過濾照實回報；拒答與安全過濾另給一則可讀提示。
    try:
        stop = adapter.stop_reason(state, final_raw)
    except Exception:
        logger.exception("Stop reason could not be read; finish_reason stays stop")
        stop = "error"
    if stop == "refusal":
        detail = turn.refusal_text.strip()
        yield yield_delta(f"{_REFUSAL_NOTICE[:-1]}：{detail}" if detail else _REFUSAL_NOTICE)
    elif stop == "safety":
        yield yield_delta(_SAFETY_NOTICE)

    # === [防幻覺打臉機制 (Guard Service)] ===
    if allow_react:
        response_text = adapter.extract_final_text(state, final_raw)
        # 已知工具集走 guard_service SSOT——涵蓋動態 MCP 工具（禁令 59(c)）。
        all_known_tools = await guard_service.collect_known_tool_names(
            tool_registry, memory_tool_registry, plugin_registry, protocol_manager,
            request_tools=getattr(request, "tools", None),
        )
        hallucination_warnings = guard_service.detect_tool_hallucinations(
            response_text, actually_used_tools, all_known_tools
        )
        if hallucination_warnings:
            yield yield_delta(
                guard_service.get_hallucination_warning_message(hallucination_warnings)
            )
        if guard_service.check_memory_hallucination(response_text, actually_used_tools):
            yield yield_delta(guard_service.get_memory_warning_message())

    t3 = get_current_epoch()
    gen_time = upstream_gen_s  # C4：Σ上游耗時（e_index 與 pure_gen_ms 同源）
    last_msg = request.messages[-1].content if request.messages else ""
    # 檢索訊號讀執行器記的限定名（ADR-0155 C2）；T3 起只看「執行過」，被擋下的 query 不算。
    e_index = calculate_emergence_index(
        5 if "memory_tools.query" in actually_used_tools.executed else 0,
        0.7,
        gen_time,
        str(last_msg),
    )

    latency_info = {
        "request_id": request_id,
        "total_ms": int((t3 - t0) * 1000),
        "local_ms": int((t1 - t0) * 1000),
        "pure_gen_ms": int(gen_time * 1000),
        "e_index": e_index,
    }

    if run_usage.totals:
        # 用量已逐輪入帳；E-Index 歷史整次 run 只記一筆（空用量＝不重複累計）。
        accumulate_token_usage(identity, provider, {}, e_index, model=usage_model)
        # 稽核 CSV 每次 run 一列、欄位不變；token 與 cost_usd 為整次 run 加總（含快取，A5）。
        log_usage_audit(identity, request_id, provider, latency_info, run_usage.totals,
                        model=usage_model, cost_usd=run_usage.cost_usd)
        if adapter.emits_usage_event(state):
            # P3：整次 run 的 token 用量＋估算成本（含快取；活動列即時顯示資源耗費）。
            # ADR-0092 Phase B：context 占用用最後一輪的總輸入（分解 v1 僅 Claude＝該家
            # 事件另附完整 context_breakdown，餘家事件形狀凍結不變）。
            _bd = compute_context_breakdown(
                *adapter.context_breakdown_args(state), usage_model, run_usage.last_input
            )
            _usage_extras: dict[str, Any] = {"usage_pct": _bd["usage_pct"]}
            if adapter.emits_context_breakdown:
                _usage_extras["context_breakdown"] = _bd
            yield sse_line(
                token_usage_event(
                    run_usage.total_tokens,
                    run_usage.cost_usd,
                    **_usage_extras,
                    estimated=run_usage.estimated,
                )
            )

    # 發送遙測
    yield sse_line({"type": "touka_status", "telemetry": latency_info})

    terminal = adapter.finalize(state, final_raw, request_id, latency_info)
    if terminal.get("choices"):  # OpenAI 形狀才有；Claude native egress 不動
        terminal["choices"][0]["finish_reason"] = _FINISH_REASONS.get(stop, "stop")
    yield sse_line(terminal)
    yield "data: [DONE]\n\n"
