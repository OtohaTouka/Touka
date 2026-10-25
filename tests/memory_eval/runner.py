"""記憶評估多輪 driver。

每輪照前端的樣子送：UI 形狀的請求（帶 session、目前分支的歷史＋這一輪），身分是合成
人類擁有者（Monitor 依前景規則代打成合成靈魂），session 的模式是 Auto（審查員模型沒設，
dangerous 直接拒、不觸發審查呼叫）。送出點 ``send_turn`` 可注入：pytest 用假串流，人工
probe 用 ``pipeline_send_turn``（真 pipeline）。每輪結束把 user／assistant 寫回沙盒對話樹，
下一輪的歷史與 ``<continuity>`` 都從那裡來。任何一輪送出失敗（含 429／5xx、預算用完）
整批停下，不重試。

每個情境用一個新的合成靈魂（``seeding.soul_id_for``）：記憶、舊對話、家對話、對話紀錄與時間軸
都只屬於那個情境，前一個情境留下的東西帶不到下一個。開始前先交給可注入的 ``prepare_soul``
（人工 probe 在這裡寫靈魂檔、核對 Owner、設主要模型）。

重跑（``run_batch(..., repeats=N)``）先把整批跑完一輪再跑下一輪，每一次都是新的靈魂。報告的每題
k/n 只算跑完（沒出錯）的次數；整批只算每題都跑完的輪。

記憶衝突這類要看記憶庫的判定（``judges.STATE_RULES``），在預放後與那一輪結束後各經可注入的
``read_facts`` 查一次有效記憶；長對話的舊對話經可注入的 ``archive_history`` 歸檔（人工 probe 用
``legacy_archiver``：產品的歸檔函式，目標必須在沙盒根之下）。

``activity_record=True``（時間軸套件）時，整批打開活動記錄器（和 Proxy 啟動時同一個開關）；
每個情境開始前等背景寫入做完、清掉合成靈魂的事件、寫入這個情境的時間點（都經產品的讀寫函式）。
整批結束時關記錄器、讓已排的回呼跑完、等背景寫入做完（有逾時），才交回呼叫端。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memory_server.config_utils import get_resource_proxy
from memory_server.tools import get_manager
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.models.context import PipelineContext
from src.services import continuity_events
from src.services.proxy_service import process_proxy_request
from src.services.tool_executor import ToolExecutor
from src.utils import task_utils
from src.utils.async_db import session_service
from src.utils.continuity_events_db import insert_event, purge_agent_events
from src.utils.history_manager import TreeMessageManager
from src.utils.time_utils import get_current_epoch
from src.utils.tool_events import TOOL_RESULT, TOOL_START

from tests.memory_eval import seeding
from tests.memory_eval.judges import STATE_RULES, ToolCallRecord, TurnObservation, judge
from tests.memory_eval.live_bounds import redact_excerpt
from tests.memory_eval.scenarios import CANARY, persona_text
from tests.memory_eval.stats import wilson

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator

    from src.models.identity import AgentIdentity

    from tests.memory_eval.judges import Verdict
    from tests.memory_eval.scenarios import Scenario

    SendTurn = Callable[[Any, PipelineContext], Awaitable[list[str]]]
    SaveFact = Callable[[AgentIdentity, str], Awaitable[Any]]
    PrepareSoul = Callable[[str, str], object]
    ReadFacts = Callable[[AgentIdentity], Awaitable[list[tuple[str, str]]]]
    ArchiveHistory = Callable[[AgentIdentity, dict[str, Any]], Awaitable[None]]

logger = logging.getLogger(__name__)

EVAL_AUTH_LEVEL = "Auto"
# handler 在連線／認證／4xx／5xx 失敗時把錯誤當文字吐出（主迴圈只送 1 次時 429／5xx 也是）：
# 四家 adapter 的 RoundError 只有這五種開頭，react_core 把它當成獨立的一段文字送出（前面不一定有
# 換行）。只看每一段文字的開頭；模型正常回答裡提到「連線錯誤」不算。
_PROVIDER_NAME = r"[A-Za-z][\w.-]*(?: [A-Za-z][\w.-]*){0,2}"  # 廠商顯示名稱：英文 1～3 個字
PROVIDER_ERROR_START = re.compile(
    rf"❌ \*\*API 發生錯誤\*\*"  # Claude
    rf"|❌ \*\*{_PROVIDER_NAME} 連線錯誤\*\*"  # OpenAI 相容、Responses 連不上
    rf"|(?:{_PROVIDER_NAME} )?API Error \((?:\d{{3}}|Timeout)\)"  # Gemini 沒有廠商名稱
    rf"|{_PROVIDER_NAME} Access Denied \(401\)"  # 缺金鑰
)
NO_REPLY = "no assistant text and no tool call"
BUDGET_REACHED = "budget soft cap reached"
NO_STATE_READER = "memory state reader not configured"
STATE_READ_FAILED = "memory state read failed"
ACTIVITY_DRAIN_TIMEOUT_S = 30.0


class MissingSeedHook(RuntimeError):
    """情境要的植入方式沒有接上（例如沒給歸檔舊對話的函式）：不送、不當成通過。"""

    def __init__(self, hook: str, scenario_id: str) -> None:
        super().__init__(f"scenario {scenario_id} needs the {hook} hook")


class SeedFailed(RuntimeError):
    """情境開始前的準備（建靈魂、植入、歸檔、讀預放記憶）失敗：這個情境不送、整批停下，報告照印。

    只帶例外的類別名稱：原文可能含本機路徑。
    """

    def __init__(self, scenario_id: str, cause: BaseException) -> None:
        super().__init__(f"seeding {scenario_id} failed: {type(cause).__name__}")


class SeededFactsMismatch(RuntimeError):
    """預放後查到的有效記憶筆數和情境預放的不同（例如被查重或消毒擋下）：記憶庫判定不可信。"""

    def __init__(self, expected: int, found: int) -> None:
        super().__init__(f"expected {expected} seeded facts, found {found}")


class ActivityWritesPending(RuntimeError):
    """背景寫入逾時還沒做完：時間軸可能混到別的情境，整批停下。"""

    def __init__(self, pending: int) -> None:
        super().__init__(f"{pending} background writes still pending after "
                         f"{ACTIVITY_DRAIN_TIMEOUT_S:g}s")


async def drain_activity_writes(timeout_s: float = ACTIVITY_DRAIN_TIMEOUT_S) -> int:
    """先讓已排的回呼跑一輪，再等這個迴圈的背景寫入（``spawn_logged``）做完；回逾時後還剩幾筆。

    記錄器從別的執行緒排回來的寫入是回呼，要先讓它跑才看得到它開的背景任務。
    """
    loop, current = asyncio.get_running_loop(), asyncio.current_task()
    deadline = loop.time() + timeout_s
    while True:
        await asyncio.sleep(0)
        pending = {task for task in list(task_utils._background_tasks)
                   if task.get_loop() is loop and not task.done() and task is not current}
        remaining = deadline - loop.time()
        if not pending or remaining <= 0:
            return len(pending)
        await asyncio.wait(pending, timeout=remaining)


def _tool_status(wire_status: str, error_class: str | None) -> str:
    if wire_status == "success":
        return "ok"
    return "denied" if error_class == "permission_denied" else "error"


def _payloads(chunks: Iterable[str]) -> Iterator[dict[str, Any]]:
    for chunk in chunks:
        for raw_line in str(chunk).splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            body = line[len("data:"):].strip()
            if not body or body == "[DONE]":
                continue
            try:
                data = json.loads(body)
            except ValueError:
                continue
            if isinstance(data, dict):
                yield data


_EXECUTE_SIGNATURE = inspect.signature(ToolExecutor.execute_to_queue)


@contextmanager
def record_tool_requests() -> Iterator[dict[str, list[tuple[str, dict[str, Any]]]]]:
    """這一輪模型要求執行的工具（呼叫 id → 依序的名稱與參數），連被擋下的也記。

    被政策擋下的呼叫只送 tool_result、不送帶名稱與參數的 tool_start，判定看不出它想做什麼
    （例如記憶衝突要看有沒有試著換掉預放那筆）。所以在執行器入口記下：同一個 id 依序存，沒有 id 的
    記在 ``""`` 底下照順序對應。紀錄本身出錯不影響工具執行（參數不是 mapping 就記成空的）。

    包的是類別方法，同一個程序裡所有 ToolExecutor 都會經過：不可重入，只能循序使用（評估本來就一輪
    一輪送）；只在評估送出這一輪時包住，結束（含失敗）就還原。
    """
    requested: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    original = ToolExecutor.execute_to_queue

    async def _recording(executor: ToolExecutor, *args: Any, **kwargs: Any) -> str:
        try:
            bound = _EXECUTE_SIGNATURE.bind(executor, *args, **kwargs).arguments
            raw = bound.get("args")
            entry = (str(bound.get("name", "")), dict(raw) if isinstance(raw, Mapping) else {})
            requested.setdefault(str(bound.get("call_id") or ""), []).append(entry)
        except Exception:  # 紀錄出任何錯都不記、照常執行（不能讓工具 task 失敗、主迴圈等不到結束）
            logger.debug("memory eval: could not record a tool request", exc_info=True)
        return await original(executor, *args, **kwargs)

    ToolExecutor.execute_to_queue = _recording  # type: ignore[method-assign]
    try:
        yield requested
    finally:
        ToolExecutor.execute_to_queue = original  # type: ignore[method-assign]


def _requested_call(
    requested: Mapping[str, list[tuple[str, dict[str, Any]]]], call_id: str
) -> tuple[str, dict[str, Any]] | None:
    """依序取出這個 id 的下一筆紀錄；沒有就取下一筆沒有 id 的。"""
    for key in (call_id, ""):
        entries = requested.get(key)
        if entries:
            return entries.pop(0)
    return None


def _discard_started(
    requested: Mapping[str, list[tuple[str, dict[str, Any]]]],
    call_id: str, name: str, args: dict[str, Any],
) -> None:
    """有 tool_start 的呼叫（被放行）不必補：把它在紀錄裡的那一筆拿掉，免得被擋下的呼叫取錯。

    先找同一個 id（沒有就找沒有 id 的）裡名稱與參數都相同的；找不到就拿掉第一筆同名的。
    """
    for key in (call_id, ""):
        entries = requested.get(key) or []
        for match_args in (True, False):
            for index, (entry_name, entry_args) in enumerate(entries):
                if entry_name == name and (entry_args == args or not match_args):
                    del entries[index]
                    return


def parse_sse(
    chunks: Iterable[str],
    requested: Mapping[str, list[tuple[str, dict[str, Any]]]] | None = None,
) -> TurnObservation:
    """SSE → 最後文字＋工具軌跡（tool_start／tool_result 以 id 配對）；認出 provider 錯誤。

    被擋下的呼叫沒有 tool_start：名稱與參數改從 ``requested``（``record_tool_requests``）補上。
    被放行的呼叫先從紀錄裡拿掉；被擋下的結果可能比同一輪放行的 tool_start 先到，整段讀完才補。
    """
    requested = {key: list(entries) for key, entries in (requested or {}).items()}
    texts: list[str] = []
    provider_error = ""
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    calls: list[ToolCallRecord | tuple[str, str, str]] = []  # 被擋下的先放（id, 工具名稱, 狀態）
    cost = 0.0
    stream_error = ""
    for data in _payloads(chunks):
        kind = data.get("type")
        if kind is None and data.get("error"):  # dispatcher 串流中途出錯的事件
            stream_error = str(data["error"])
        elif kind == "touka_progress" and data.get("kind") == "token_usage":
            cost += float(data.get("cost") or 0.0)
        elif kind == TOOL_START:
            call_id = str(data.get("id", ""))
            pending[call_id] = (str(data.get("tool", "")), data.get("args") or {})
            _discard_started(requested, call_id, *pending[call_id])
        elif kind == TOOL_RESULT:
            call_id = str(data.get("id", ""))
            status = _tool_status(str(data.get("status", "")), data.get("error_class"))
            if call_id in pending:
                name, args = pending.pop(call_id)
                calls.append(ToolCallRecord(name=name, args=args, status=status))
            else:
                calls.append((call_id, str(data.get("tool", "")), status))
        elif kind == "content_block_delta":
            texts.append((data.get("delta") or {}).get("text") or "")
        elif isinstance(data.get("choices"), list) and data["choices"]:
            texts.append(((data["choices"][0] or {}).get("delta") or {}).get("content") or "")
        if texts and not provider_error:
            found = PROVIDER_ERROR_START.match(texts[-1].lstrip())
            provider_error = found.group(0) if found else ""
    records: list[ToolCallRecord] = []
    for call in calls:
        if isinstance(call, ToolCallRecord):
            records.append(call)
            continue
        call_id, tool, status = call
        name, args = _requested_call(requested, call_id) or (tool, {})
        records.append(ToolCallRecord(name=name, args=args, status=status))
    text = "".join(texts).strip()
    error = stream_error or provider_error
    if not error and not text and not records:
        error = NO_REPLY
    return TurnObservation(text=text, tool_calls=tuple(records),
                           error=error and f"{error} {text}"[:300], cost_usd=cost)


async def pipeline_send_turn(request: Any, ctx: PipelineContext) -> list[str]:
    """真 pipeline：和前端聊天同一條路（Monitor→Context→…→Tool→dispatcher）。"""
    response = await process_proxy_request(request, ctx, is_anthropic=False)
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        body = getattr(response, "body", b"")
        return [body.decode("utf-8") if isinstance(body, bytes) else str(body)]
    return [c.decode("utf-8") if isinstance(c, bytes) else str(c) async for c in body_iterator]


async def memory_save_fact(identity: AgentIdentity, content: str) -> str:
    """植入事實走產品的存記憶路徑（消毒、查重、向量索引都照常）。"""
    return await get_manager().save_memory(identity, content)


async def memory_active_facts(identity: AgentIdentity) -> list[tuple[str, str]]:
    """這個靈魂的有效記憶（id, 內容）：產品的唯讀查詢，封存與被取代的不算。"""
    rows = get_manager().get_factual_store().query_facts(identity)
    return [(str(row["id"]), str(row["content"])) for row in rows]


def legacy_archiver(sandbox_root: Path) -> ArchiveHistory:
    """舊對話歸檔：和滑動視窗裁掉的舊對話同一個產品函式；舊對話庫的位置必須在沙盒根之下。

    回傳的筆數不用（它的語意另有修正），寫入失敗照常往上拋。
    """
    root = sandbox_root.resolve()

    async def _archive(identity: AgentIdentity, payload: dict[str, Any]) -> None:
        manager = get_manager()
        legacy_dir = Path(manager.get_legacy_dir(identity, resource_proxy=get_resource_proxy()))
        if not legacy_dir.resolve().is_relative_to(root):
            raise seeding.SeedOutsideSandbox
        manager.get_legacy_sync_service().archive_payload(
            identity, payload, f"{payload['id']}.json"
        )

    return _archive


@dataclass(frozen=True, slots=True)
class TurnResult:
    user: str
    observation: TurnObservation
    verdicts: tuple[Verdict, ...]

    @property
    def passed(self) -> bool:
        return not self.observation.error and all(v.passed for v in self.verdicts)


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    turns: tuple[TurnResult, ...]
    planned_turns: int
    soul_id: str = ""
    canary: str = field(default="", repr=False)  # 只拿來遮報告，不印出
    round_number: int = 1

    @property
    def error(self) -> str:
        return next((t.observation.error for t in self.turns if t.observation.error), "")

    @property
    def passed(self) -> bool:
        return len(self.turns) == self.planned_turns and all(t.passed for t in self.turns)

    def to_dict(self) -> dict[str, Any]:
        """報告：模型回的文字、錯誤與判定細節都先去敏再截 200 字（使用者句是合成資料）。"""
        return {
            "scenario": self.scenario_id,
            "round": self.round_number,
            "soul": self.soul_id,
            "passed": self.passed,
            "error": redact_excerpt(self.error, self.canary),
            "turns": [
                {
                    "user": t.user,
                    "text": redact_excerpt(t.observation.text, self.canary),
                    "tools": [(c.name, c.args.get("action"), c.status)
                              for c in t.observation.tool_calls],
                    "verdicts": [(v.rule, v.passed, redact_excerpt(v.detail, self.canary))
                                 for v in t.verdicts],
                }
                for t in self.turns
            ],
        }


@dataclass(slots=True)
class BatchReport:
    results: list[ScenarioResult] = field(default_factory=list)
    not_run: list[str] = field(default_factory=list)
    stopped_reason: str = ""
    spent_usd: float = 0.0
    canary: str = field(default="", repr=False)  # 只拿來遮報告，不印出
    repeats: int = 1
    scenario_ids: tuple[str, ...] = ()

    def _finished(self) -> list[ScenarioResult]:
        """跑完的次數：沒出錯（出錯、到軟上限都沒跑完，不算進 n）。"""
        return [r for r in self.results if not r.error]

    def _rounds_completed(self, finished: list[ScenarioResult]) -> int:
        """從第 1 輪起，每題都跑完的輪數（中途停下的那一輪不算）。"""
        ids = set(self.scenario_ids)
        rounds = 0
        while rounds < self.repeats and ids == {
                r.scenario_id for r in finished if r.round_number == rounds + 1}:
            rounds += 1
        return rounds

    def to_dict(self) -> dict[str, Any]:
        finished = self._finished()
        by_scenario = {sid: {"k": 0, "n": 0} for sid in self.scenario_ids}
        for result in finished:
            counts = by_scenario.setdefault(result.scenario_id, {"k": 0, "n": 0})
            counts["n"] += 1
            counts["k"] += result.passed
        rounds = self._rounds_completed(finished)
        in_rounds = [r for r in finished if r.round_number <= rounds]
        k, n = sum(r.passed for r in in_rounds), len(in_rounds)
        interval = wilson(k, n)
        return {
            "passed": sum(r.passed for r in self.results),
            "failed": sum(not r.passed for r in self.results),
            "spent_usd": round(self.spent_usd, 6),
            "stopped_reason": redact_excerpt(self.stopped_reason, self.canary),
            "not_run": list(self.not_run),
            "repeats": self.repeats,
            "rounds_completed": rounds,
            "by_scenario": by_scenario,
            "uneven_n": len({c["n"] for c in by_scenario.values()}) > 1,
            "batch": {"k": k, "n": n, "rounds": rounds,
                      "rate": round(k / n, 6) if n else None,
                      "wilson95": [round(x, 6) for x in interval] if interval else None},
            "results": [r.to_dict() for r in self.results],
        }


class MemoryEvalDriver:
    """把情境一輪一輪送出、判定、寫回沙盒對話樹。"""

    def __init__(
        self,
        *,
        base_ctx: dict[str, Any],
        send_turn: SendTurn,
        save_fact: SaveFact,
        max_tokens: int,
        now_ms: Callable[[], int] | None = None,
        auth_level: str = EVAL_AUTH_LEVEL,
        budget_usd: float | None = None,
        activity_record: bool = False,
        prepare_soul: PrepareSoul | None = None,
        read_facts: ReadFacts | None = None,
        archive_history: ArchiveHistory | None = None,
        canary: str = CANARY,
    ) -> None:
        self._base_ctx = base_ctx
        self._send_turn = send_turn
        self._save_fact = save_fact
        self._max_tokens = max_tokens
        self._now_ms = now_ms or (lambda: int(get_current_epoch() * 1000))
        self._auth_level = auth_level
        self._budget_usd = budget_usd
        self._activity_record = activity_record
        self._prepare_soul = prepare_soul
        self._read_facts = read_facts
        self._archive_history = archive_history
        self._canary = canary
        self._souls_used = 0
        self.spent_usd = 0.0

    def _over_budget(self) -> bool:
        """USD 軟上限（依串流回報的估計成本累計）；請求數硬上限在送出層另擋。"""
        return self._budget_usd is not None and self.spent_usd >= self._budget_usd

    def _new_soul(self) -> str:
        """這個情境專用的新靈魂 id；有 ``prepare_soul`` 就先交給它（人格含這次的口令）。"""
        self._souls_used += 1
        soul_id = seeding.soul_id_for(self._souls_used)
        if self._prepare_soul is not None:
            self._prepare_soul(soul_id, persona_text(self._canary))
        return soul_id

    async def _save_tree(
        self, session_id: str, tree: dict[str, Any], soul_id: str, **extra: Any
    ) -> None:
        await session_service.upsert_session(
            session_id, "memory eval", tree, identity=seeding.soul_identity(soul_id),
            auth_level=self._auth_level, **extra,
        )

    async def _reset_timeline(self, scenario: Scenario, soul_id: str) -> None:
        """等背景寫入做完 → 清掉合成靈魂的事件 → 寫入這個情境的時間點（產品的讀寫函式）。

        可信紀錄起點記成最早那筆預放事件的時間（三行都有紀錄時模型本來就看不到這行）。
        """
        pending = await drain_activity_writes()
        if pending:
            raise ActivityWritesPending(pending)
        rows = seeding.plan_timeline(scenario, self._now_ms(), soul_id)
        start = min((row.occurred_at for row in rows), default=self._now_ms())
        await purge_agent_events(seeding.soul_identity(soul_id), marker_ms=start)
        for row in rows:
            await insert_event(row)

    async def seed(self, scenario: Scenario, session_id: str, soul_id: str) -> dict[str, Any]:
        """植入情境資料（時間軸、目前 session、家對話、前情摘要、事實），回目前 session 的樹。"""
        if self._activity_record:
            await self._reset_timeline(scenario, soul_id)
        plan = seeding.plan_seed(scenario, self._now_ms())
        await self._save_tree(session_id, plan.session_tree, soul_id,
                              context_summary=plan.summary or None)
        if plan.home_tree["currentId"]:
            await self._save_tree(f"{session_id}-home", plan.home_tree, soul_id,
                                  is_home=True, home_for_agent=soul_id)
        for fact in plan.facts:
            await self._save_fact(seeding.soul_identity(soul_id), fact)
        if plan.archive is not None:
            if self._archive_history is None:
                raise MissingSeedHook("archive_history", scenario.id)
            await self._archive_history(seeding.soul_identity(soul_id), plan.archive)
        return plan.session_tree

    async def _seeded_fact_ids(self, scenario: Scenario, soul_id: str) -> tuple[str, ...]:
        """有看記憶庫的判定時，預放後先記下預放記憶的 id（新靈魂：現有的就是預放的）。"""
        needs_state = any(c.rule in STATE_RULES for t in scenario.turns for c in t.checks)
        if not needs_state or self._read_facts is None:
            return ()
        seeded = tuple(fact_id for fact_id, _content in
                       await self._read_facts(seeding.soul_identity(soul_id)))
        if len(seeded) != len(scenario.seed.facts):
            raise SeededFactsMismatch(len(scenario.seed.facts), len(seeded))
        return seeded

    async def _with_memory_state(
        self, observation: TurnObservation, soul_id: str, seeded: tuple[str, ...]
    ) -> TurnObservation:
        if self._read_facts is None:
            return replace(observation, error=NO_STATE_READER)
        try:
            facts = await self._read_facts(seeding.soul_identity(soul_id))
        except Exception as exc:  # 讀記憶庫失敗：記成這一輪的錯誤（整批停下、報告照印）
            return replace(observation, error=f"{STATE_READ_FAILED}: {type(exc).__name__}")
        return replace(observation, active_facts=tuple(facts), seeded_fact_ids=seeded)

    def _context(self, soul_id: str) -> PipelineContext:
        ctx = PipelineContext(**self._base_ctx)
        ctx.identity = seeding.driver_identity(soul_id)
        ctx.incognito = False
        return ctx

    async def _send(self, request: Any, soul_id: str) -> TurnObservation:
        try:
            with record_tool_requests() as requested:
                chunks = await self._send_turn(request, self._context(soul_id))
            return parse_sse(chunks, requested)
        except Exception as exc:  # 預算用完、閘門拒絕等：記成這一輪失敗，整批停下
            return TurnObservation(text="", error=f"{type(exc).__name__}: {exc}"[:300])

    async def run_scenario(self, scenario: Scenario, *, round_number: int = 1) -> ScenarioResult:
        session_id = f"memory-eval-{scenario.id}-{uuid.uuid4().hex[:8]}"
        try:
            soul_id = self._new_soul()
            tree = await self.seed(scenario, session_id, soul_id)
            seeded = await self._seeded_fact_ids(scenario, soul_id)
        except ActivityWritesPending:
            raise
        except Exception as exc:  # 準備失敗：不送，交給 _run_all 記成停止原因（報告照印）
            raise SeedFailed(scenario.id, exc) from exc
        results: list[TurnResult] = []
        for turn in scenario.turns:
            if self._over_budget():
                results.append(TurnResult(turn.user, TurnObservation("", error=BUDGET_REACHED), ()))
                break
            if turn.new_session:
                session_id = f"memory-eval-{scenario.id}-{uuid.uuid4().hex[:8]}"
                tree = seeding.new_tree()
                await self._save_tree(session_id, tree, soul_id)
            history = [
                OpenAIMessage(role=node["role"], content=node["content"])
                for node in TreeMessageManager.get_linear_path(tree)
            ]
            request = OpenAIChatRequest(
                model=soul_id,
                messages=[*history, OpenAIMessage(role="user", content=turn.user)],
                session_id=session_id,
                stream=True,
                max_tokens=self._max_tokens,
            )
            observation = await self._send(request, soul_id)
            if not observation.error and any(c.rule in STATE_RULES for c in turn.checks):
                observation = await self._with_memory_state(observation, soul_id, seeded)
            self.spent_usd += observation.cost_usd
            verdicts = tuple(judge(check, observation, canary=self._canary)
                             for check in turn.checks)
            results.append(TurnResult(turn.user, observation, verdicts))
            if observation.error:
                break
            now = self._now_ms()
            seeding.append_node(tree, "user", turn.user, now)
            seeding.append_node(tree, "assistant", observation.text, now)
            await self._save_tree(session_id, tree, soul_id)
        return ScenarioResult(scenario.id, tuple(results), planned_turns=len(scenario.turns),
                              soul_id=soul_id, canary=self._canary, round_number=round_number)

    async def _run_all(self, scenarios: tuple[Scenario, ...], report: BatchReport) -> None:
        """一輪跑完整批再跑下一輪；只跑一輪時沒跑的照舊只寫情境 id，多輪時加 ``#輪次``。"""
        for round_number in range(1, report.repeats + 1):
            for scenario in scenarios:
                label = scenario.id if report.repeats == 1 else f"{scenario.id}#{round_number}"
                if not report.stopped_reason and self._over_budget():
                    report.stopped_reason = BUDGET_REACHED
                if report.stopped_reason:
                    report.not_run.append(label)
                    continue
                try:
                    result = await self.run_scenario(scenario, round_number=round_number)
                except (ActivityWritesPending, SeedFailed) as exc:  # 準備沒做完：這個情境不送
                    report.stopped_reason = str(exc)
                    report.not_run.append(label)
                    continue
                report.results.append(result)
                report.stopped_reason = result.error

    async def run_batch(self, scenarios: Iterable[Scenario], *, repeats: int = 1) -> BatchReport:
        batch = tuple(scenarios)
        report = BatchReport(canary=self._canary, repeats=repeats,
                             scenario_ids=tuple(s.id for s in batch))
        if not self._activity_record:
            await self._run_all(batch, report)
        else:
            continuity_events.start_recording()
            try:
                await self._run_all(batch, report)
            finally:
                continuity_events.stop_recording()
                pending = await drain_activity_writes()
                if pending and not report.stopped_reason:
                    report.stopped_reason = str(ActivityWritesPending(pending))
        report.spent_usd = self.spent_usd
        return report
