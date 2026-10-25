"""記憶評估多輪 driver 與合成資料（離線：假串流、沙盒 session DB、零下載）。

最後一個測試走真 pipeline（Monitor→…→Tool 各層），只把送出換成假串流，確認人工 probe
真的打到合成靈魂：人格、記憶工具、連續性區塊、身分都對（scorecard 打不到靈魂是 F2）。
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from memory_server import config_utils
from memory_server import vector as memory_vector
from memory_server.services import event_recorder as memory_events
from memory_server.tools import get_manager
from memory_server.tools.legacy_vault import search_conversations
from src.handlers.adapters.claude_adapter import _API_ERROR as CLAUDE_API_ERROR
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import _MISSING_KEY as RESPONSES_MISSING_KEY
from src.main import build_base_context
from src.services import continuity_events as ce
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.utils import task_utils
from src.utils.async_db import init_db_async, session_service
from src.utils.auth_mode import mode_for_acting_agent
from src.utils.brain_persistence import set_active_brain
from src.utils.continuity_events_db import list_agent_events
from src.utils.history_manager import TreeMessageManager
from src.utils.tool_events import TOOL_RESULT, TOOL_START

from tests.memory_eval import runner, seeding
from tests.memory_eval import scenarios as sc

if TYPE_CHECKING:
    from collections.abc import Callable

SOUL_1, SOUL_2 = seeding.soul_id_for(1), seeding.soul_id_for(2)
_IDENTITY_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "identity_map.json.example"
)
NOW_MS = 1_760_500_000_000


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _text_chunks(text: str) -> list[str]:
    return [_sse({"choices": [{"delta": {"content": text}}]}), "data: [DONE]\n\n"]


def _tool_chunks(
    name: str, args: dict[str, Any], status: str = "success", call_id: str = "c1"
) -> list[str]:
    return [
        _sse({"type": TOOL_START, "id": call_id, "tool": name, "args": args}),
        _sse({"type": TOOL_RESULT, "id": call_id, "tool": name, "status": status,
              "result": "ok"}),
    ]


@pytest.fixture
async def sandbox_db(tmp_path: Path) -> Any:
    with patch("src.utils.async_db.get_db_path", return_value=tmp_path / "sessions.db"):
        await init_db_async()
        yield


def _fake_sender(replies: list[list[str]], sent: list[tuple[Any, Any]]) -> Callable:
    async def _send(request: Any, ctx: Any) -> list[str]:
        sent.append((request, ctx))
        return replies.pop(0)

    return _send


async def _no_save(_identity: Any, _content: str) -> str:
    return "saved"


def _soul_writer(sandbox: str, model: str | None = None) -> Callable[[str, str], None]:
    """每個情境的合成靈魂寫進測試沙盒（記錄器只記正式靈魂；真 pipeline 還要主要模型）."""

    def _prepare(soul_id: str, persona: str = sc.PERSONA_TEXT) -> None:
        seeding.write_synthetic_soul(
            Path(sandbox), soul_id, sandbox_root=Path(sandbox), persona=persona
        )
        if model is not None:
            set_active_brain(seeding.soul_identity(soul_id), model)

    return _prepare


def _driver(send: Callable, **kwargs: Any) -> runner.MemoryEvalDriver:
    return runner.MemoryEvalDriver(
        base_ctx={}, send_turn=send, save_fact=_no_save, max_tokens=256,
        now_ms=lambda: NOW_MS, **kwargs,
    )


# --- SSE 解析 ---------------------------------------------------------------


def test_parse_sse_collects_text_and_tool_trace() -> None:
    chunks = [
        *_tool_chunks("memory_tools", {"action": "query", "query": "貓"}),
        *_tool_chunks("mission_tools", {"action": "status"}, status="error"),
        _sse({"type": "content_block_delta", "delta": {"text": "你的貓"}}),
        *_text_chunks("叫麻糬。"),
        "not-sse",
    ]

    obs = runner.parse_sse(chunks)

    assert obs.text == "你的貓叫麻糬。"
    assert [(c.name, c.args.get("action"), c.status) for c in obs.tool_calls] == [
        ("memory_tools", "query", "ok"),
        ("mission_tools", "status", "error"),
    ]
    assert obs.error == ""


# Touka 自己的五種錯誤訊息格式（四家 adapter 的 RoundError；react_core 以獨立的一段文字送出）
PROVIDER_ERRORS = [
    CLAUDE_API_ERROR.format("overloaded"),  # Claude（直接用 adapter 的格式常數）
    "API Error (429)\n\n`rate`",  # Gemini（前面沒有廠商名稱）
    "API Error (Timeout)\n\n`idle 120s`",
    "DeepSeek API Error (503)\n\n`busy`",  # OpenAI 相容、Responses
    "❌ **xAI 連線錯誤**\n\n`ConnectError`",  # 連不上
    RESPONSES_MISSING_KEY.format("OpenAI"),  # 缺金鑰（直接用 adapter 的格式常數）
]


@pytest.mark.parametrize("text", PROVIDER_ERRORS)
def test_parse_sse_marks_provider_errors(text: str) -> None:
    assert runner.parse_sse(_text_chunks(text)).error


@pytest.mark.parametrize("text", PROVIDER_ERRORS)
def test_an_error_after_text_from_an_earlier_round_is_still_recognised(text: str) -> None:
    """第 1 輪的文字結尾沒有換行，第 2 輪才出錯：錯誤是另一段文字，整批照樣停下."""
    chunks = [_sse({"choices": [{"delta": {"content": "我先查一下記憶。"}}]}), *_text_chunks(text)]

    assert runner.parse_sse(chunks).error


@pytest.mark.parametrize(
    "text",
    [
        "上次遇到連線錯誤，所以我重新查了一次。",  # 正常回答提到「連線錯誤」
        "設定檔裡寫著 Missing `name`，看起來是範例。",
        "如果看到 API Error (429) 就稍等一下。",  # 不在一段的開頭
        "Access Denied 是這本書的書名。",
    ],
)
def test_ordinary_replies_that_mention_errors_are_not_provider_errors(text: str) -> None:
    assert runner.parse_sse(_text_chunks(text)).error == ""


def test_parse_sse_without_any_reply_is_an_error() -> None:
    assert runner.parse_sse(["data: [DONE]\n\n"]).error


def test_parse_sse_marks_dispatcher_error_after_partial_text() -> None:
    """串流中途出錯（例如預算用完在 send_round 擋下）時 dispatcher 送 error 事件，不能當成功。"""
    chunks = [*_text_chunks("先回一點"), _sse({"error": "Dispatcher error during stream"})]

    assert "Dispatcher error" in runner.parse_sse(chunks).error


def _usage(cost: float) -> str:
    return _sse({"type": "touka_progress", "kind": "token_usage", "total": 100, "cost": cost})


def test_parse_sse_sums_token_usage_cost() -> None:
    obs = runner.parse_sse([_usage(0.01), _usage(0.02), *_text_chunks("好")])

    assert obs.cost_usd == pytest.approx(0.03)


# --- 合成資料 ---------------------------------------------------------------


def test_seed_plan_puts_the_long_history_only_in_the_archive() -> None:
    scenario = next(s for s in sc.SCENARIOS if s.id == "long_history_recall")

    plan = seeding.plan_seed(scenario, NOW_MS)
    messages = plan.archive["messages"]

    assert TreeMessageManager.get_linear_path(plan.session_tree) == []
    assert len(messages) == 200
    assert [m["role"] for m in messages[:2]] == ["user", "assistant"]
    assert messages[4]["content"] == scenario.seed.early_fact  # 第 3 輪的 user
    # 只有那一句帶數字：「第 47 輪」不能被當成答對
    assert [m["content"] for m in messages if any(ch.isdigit() for ch in m["content"])] == [
        scenario.seed.early_fact
    ]
    stamps = [m["timestamp"] for m in messages]
    assert stamps == sorted(stamps)
    assert all(isinstance(s, int) for s in stamps)
    assert stamps[-1] == NOW_MS - seeding.ARCHIVE_AGE_S * 1000
    assert plan.archive["id"] != plan.archive["title"]


def test_other_scenarios_archive_nothing() -> None:
    assert [s.id for s in sc.SCENARIOS if seeding.plan_seed(s, NOW_MS).archive] == [
        "long_history_recall"
    ]


def test_seed_plan_without_age_has_no_timestamps() -> None:
    scenario = next(s for s in sc.SCENARIOS if s.id == "poison_home")

    plan = seeding.plan_seed(scenario, NOW_MS)

    assert TreeMessageManager.get_linear_path(plan.session_tree) == []
    home = TreeMessageManager.get_linear_path(plan.home_tree)
    assert [n["role"] for n in home] == ["user", "assistant"]
    assert sc.FAKE_CLOSE in home[1]["content"]


def test_synthetic_soul_is_written_only_under_the_sandbox_root(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    soul_dir = seeding.write_synthetic_soul(
        root / "app", SOUL_1, sandbox_root=root, persona=sc.PERSONA_TEXT
    )

    assert soul_dir.name == SOUL_1
    assert (soul_dir / "soul.md").read_text(encoding="utf-8") == sc.PERSONA_TEXT
    config = json.loads((soul_dir / "config.json").read_text(encoding="utf-8"))
    assert config["is_toukasoul"] is True
    assert set(config["enabled_tools"]) == {"memory_tools", "mission_tools"}
    with pytest.raises(seeding.SeedOutsideSandbox):
        seeding.write_synthetic_soul(
            tmp_path / "elsewhere", SOUL_1, sandbox_root=root, persona=sc.PERSONA_TEXT
        )


def test_deterministic_encoder_is_stable_normalized_and_offline() -> None:
    encoder = seeding.DeterministicEncoder("intfloat/multilingual-e5-base")
    first = encoder.encode("passage: 我的貓叫麻糬")
    again = seeding.DeterministicEncoder().encode("passage: 我的貓叫麻糬")
    other = encoder.encode("passage: 今天下雨")
    near = encoder.encode("query: 我的貓叫什麼")

    def cos(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert first.tolist() == again.tolist()
    assert len(first) == encoder.get_sentence_embedding_dimension()
    assert math.isclose(math.sqrt(cos(first, first)), 1.0, rel_tol=1e-9)
    assert cos(first, near) > cos(first, other)


def test_stub_name_recalls_a_paraphrase_from_a_real_local_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """替身名稱走產品的預設下限：存一筆、用改述查得到（10/15 套到 e5 的下限時兩次都查不到）."""
    monkeypatch.setenv("EMBEDDING_MODEL", seeding.STUB_EMBEDDING_MODEL)
    monkeypatch.setattr(memory_vector, "SentenceTransformer", seeding.DeterministicEncoder)
    directory = tmp_path / "vectors"
    store = memory_vector.VectorStore(
        base_dir=directory, model_name=config_utils.get_embedding_model_name()
    )
    try:
        soul = seeding.soul_identity(SOUL_1)
        assert store.save_vector(soul, "fact-cat", "使用者的貓叫麻糬，今年三歲。")
        hits = store.query_vectors(soul, "我的貓叫什麼名字？")
    finally:
        client_key = str((directory / memory_vector.QDRANT_DIR_NAME).absolute())
        memory_vector.VectorStore._client_cache.pop(client_key).close()

    assert [hit["fact_id"] for hit in hits] == ["fact-cat"]


def test_timeline_seed_rows_follow_the_product_event_shape() -> None:
    scenario = sc.select_scenarios(sc.CONTINUITY, ["continuity_last_visit"])[0]

    rows = seeding.plan_timeline(scenario, NOW_MS, SOUL_1)

    assert [row.kind for row in rows] == [event.kind for event in scenario.seed.timeline]
    for row, event in zip(rows, scenario.seed.timeline, strict=True):
        assert row.agent == SOUL_1
        assert row.occurred_at == NOW_MS - event.age_s * 1000
        assert row.event_id == ce.event_id(row.kind, row.agent, row.subject, row.occurred_at)
        assert row.metadata == dict(event.metadata)
        assert row.session_id == f"memory-eval-{scenario.id}-earlier"


def test_continuity_seeds_use_only_product_event_kinds() -> None:
    kinds = {e.kind for s in sc.select_scenarios(sc.CONTINUITY) for e in s.seed.timeline}
    assert kinds <= ce.EVENT_KINDS


# --- 多輪 driver --------------------------------------------------------------


async def test_driver_runs_multi_turn_with_history_and_persists_tree(sandbox_db: Any) -> None:
    scenario = next(s for s in sc.SCENARIOS if s.id == "persona_identity")
    sent: list[tuple[Any, Any]] = []
    send = _fake_sender([_text_chunks("我是霧島澪。"), _text_chunks("我還是澪。")], sent)

    result = await _driver(send).run_scenario(scenario)

    assert result.passed, result.to_dict()
    assert len(sent) == 2
    first, second = (request for request, _ctx in sent)
    assert first.model == SOUL_1
    assert first.max_tokens == 256
    assert first.session_id == second.session_id
    assert [m.role for m in second.messages] == ["user", "assistant", "user"]
    assert second.messages[1].content == "我是霧島澪。"
    ctx = sent[0][1]
    assert ctx["identity"].is_human
    assert ctx["identity"].owned_agents == [SOUL_1]
    stored = await session_service.get_session_by_id(first.session_id)
    path = TreeMessageManager.get_linear_path(stored["messages"])
    assert [n["content"] for n in path] == [
        scenario.turns[0].user, "我是霧島澪。", scenario.turns[1].user, "我還是澪。",
    ]
    assert all(n["timestamp"] == NOW_MS for n in path)
    assert stored["agent_id"] == SOUL_1
    # ToolLayer 用同一個函式從 session 解析這次執行的模式（ADR-0246）
    resolved = mode_for_acting_agent(
        stored["auth_level"], bound_agent=SOUL_1, acting_agent=SOUL_1
    )
    assert resolved == runner.EVAL_AUTH_LEVEL == "Auto"


async def test_driver_new_session_drops_history(sandbox_db: Any) -> None:
    scenario = next(s for s in sc.SCENARIOS if s.id == "cross_session_recall")
    sent: list[tuple[Any, Any]] = []
    send = _fake_sender([
        [*_tool_chunks("memory_tools", {"action": "save"}), *_text_chunks("記住了。")],
        [*_tool_chunks("memory_tools", {"action": "query"}), *_text_chunks("牠叫麻糬。")],
    ], sent)

    result = await _driver(send).run_scenario(scenario)

    assert result.passed, result.to_dict()
    (first, _), (second, _) = sent
    assert first.session_id != second.session_id
    assert [m.role for m in second.messages] == ["user"]


async def test_driver_seeds_summary_home_and_facts(sandbox_db: Any) -> None:
    saved: list[tuple[str, str]] = []

    async def _save(identity: Any, content: str) -> str:
        saved.append((identity.agent_id, content))
        return "saved"

    scenario = next(s for s in sc.SCENARIOS if s.id == "relative_time_summary")
    sent: list[tuple[Any, Any]] = []
    driver = runner.MemoryEvalDriver(
        base_ctx={}, send_turn=_fake_sender([_text_chunks("大約 10 天前。")], sent),
        save_fact=_save, max_tokens=64, now_ms=lambda: NOW_MS,
    )

    result = await driver.run_scenario(scenario)

    assert result.passed, result.to_dict()
    request = sent[0][0]
    stored = await session_service.get_session_by_id(request.session_id)
    assert stored["context_summary"] == scenario.seed.summary
    assert [m.content for m in request.messages[:2]] == [c for _r, c in scenario.seed.history]
    conflict = next(s for s in sc.SCENARIOS if s.id == "memory_conflict")
    await driver.seed(conflict, "seed-only-session", SOUL_2)
    assert saved == [(SOUL_2, fact) for fact in conflict.seed.facts]


async def test_batch_stops_after_the_first_provider_error(sandbox_db: Any) -> None:
    sent: list[tuple[Any, Any]] = []
    send = _fake_sender([_text_chunks("API Error (429)\n\n`rate`")], sent)
    batch = sc.select_scenarios(sc.SMOKE, ["poison_summary", "poison_home"])

    report = await _driver(send).run_batch(batch)

    assert report.stopped_reason
    assert len(sent) == 1
    assert [r.scenario_id for r in report.results] == ["poison_summary"]
    assert not report.results[0].passed
    assert report.to_dict()["not_run"] == ["poison_home"]


async def test_batch_stops_at_the_budget_soft_cap(sandbox_db: Any) -> None:
    sent: list[tuple[Any, Any]] = []
    send = _fake_sender([[_usage(0.3), *_text_chunks("我是霧島澪。")]], sent)
    batch = sc.select_scenarios(sc.SMOKE, ["poison_summary", "poison_home"])

    report = await _driver(send, budget_usd=0.25).run_batch(batch)

    assert len(sent) == 1
    assert report.stopped_reason == runner.BUDGET_REACHED
    summary = report.to_dict()
    assert summary["not_run"] == ["poison_home"]
    assert summary["spent_usd"] == pytest.approx(0.3)


async def test_report_masks_and_cuts_the_reply_text(sandbox_db: Any) -> None:
    """報告的回應摘要：遮口令與本機路徑、截 200 字；判定細節也不露口令。"""
    reply = f"口令是{sc.CANARY}，檔案在C:\\Users\\someone\\a.txt。" + "好" * 300
    send = _fake_sender([_text_chunks(reply)], [])
    scenario = next(s for s in sc.SCENARIOS if s.id == "poison_home")

    turn = (await _driver(send).run_scenario(scenario)).to_dict()["turns"][0]

    rendered = json.dumps(turn, ensure_ascii=False)
    assert sc.CANARY not in rendered
    assert "someone" not in rendered
    assert len(turn["text"]) <= 201
    assert turn["user"] == scenario.turns[0].user


async def test_scenario_stops_mid_way_at_the_budget_soft_cap(sandbox_db: Any) -> None:
    sent: list[tuple[Any, Any]] = []
    send = _fake_sender([[_usage(0.5), *_text_chunks("我是霧島澪。")]], sent)
    scenario = next(s for s in sc.SCENARIOS if s.id == "persona_identity")

    result = await _driver(send, budget_usd=0.25).run_scenario(scenario)

    assert len(sent) == 1
    assert not result.passed
    assert result.error == runner.BUDGET_REACHED


# --- 活動記錄器（只在時間軸套件打開） ------------------------------------------------


@pytest.fixture
def synthetic_soul(global_test_sandbox: str) -> Callable[[str], None]:
    """記錄器只記正式靈魂：每個情境的合成靈魂寫進沙盒."""
    return _soul_writer(global_test_sandbox)


@pytest.fixture
def slow_activity_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """記錄器的背景寫入慢一點（比下一個情境開始前的工作久）：沒等寫入做完就往下走，測試看得出來."""
    real_insert = ce.insert_event

    async def _slow_insert(row: Any) -> bool:
        await asyncio.sleep(0.5)
        return await real_insert(row)

    monkeypatch.setattr(ce, "insert_event", _slow_insert)


def _pending_background_tasks() -> list[asyncio.Task[Any]]:
    loop = asyncio.get_running_loop()
    return [t for t in list(task_utils._background_tasks) if t.get_loop() is loop and not t.done()]


async def test_recorder_runs_only_for_the_activity_record_batch(
    sandbox_db: Any, synthetic_soul: Callable[[str], None]
) -> None:
    bound: list[bool] = []

    async def _send(_request: Any, _ctx: Any) -> list[str]:
        bound.append(ce.recorder._loop is asyncio.get_running_loop())
        return _text_chunks("大約 3 天前。")

    await _driver(_send, prepare_soul=synthetic_soul).run_batch(
        sc.select_scenarios(sc.SMOKE, ["elapsed_3h"])
    )
    await _driver(_send, activity_record=True, prepare_soul=synthetic_soul).run_batch(
        sc.select_scenarios(sc.CONTINUITY, ["continuity_last_visit"])
    )

    assert bound == [False, True]
    assert ce.recorder._loop is None
    assert memory_events._Registration.recorder is None


async def test_batch_end_closes_the_recorder_and_waits_for_its_writes(
    sandbox_db: Any, synthetic_soul: Callable[[str], None], slow_activity_writes: None
) -> None:
    async def _send(_request: Any, _ctx: Any) -> list[str]:
        ce.recorder.record(seeding.soul_identity(SOUL_1), "task_done", "m-live/live-task",
                           {"mission_id": "m-live", "task_id": "live-task"})
        return _text_chunks("大約 3 天前。")

    await _driver(_send, activity_record=True, prepare_soul=synthetic_soul).run_batch(
        sc.select_scenarios(sc.CONTINUITY, ["continuity_last_visit"])
    )

    assert ce.recorder._loop is None
    assert _pending_background_tasks() == []
    subjects = {e["subject"] for e in await list_agent_events(seeding.soul_identity(SOUL_1))}
    assert "m-live/live-task" in subjects


async def test_scenario_is_not_sent_while_earlier_writes_are_still_pending(
    sandbox_db: Any, synthetic_soul: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """情境開始前等不到背景寫入做完：這個情境不送、整批停下."""
    sent: list[tuple[Any, Any]] = []

    async def _still_pending(**_kwargs: Any) -> int:
        return 1

    monkeypatch.setattr(runner, "drain_activity_writes", _still_pending)
    batch = sc.select_scenarios(sc.CONTINUITY)

    report = await _driver(
        _fake_sender([], sent), activity_record=True, prepare_soul=synthetic_soul
    ).run_batch(batch)

    assert sent == []
    assert report.results == []
    assert report.not_run == [s.id for s in batch]
    assert "still pending" in report.stopped_reason
    assert ce.recorder._loop is None


async def test_writes_pending_at_the_batch_end_stop_the_batch(
    sandbox_db: Any, synthetic_soul: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """整批結束時還有沒寫完的背景寫入：記成停止原因（實測腳本以非零結束）."""
    calls: list[int] = []

    async def _pending_only_at_the_end(**_kwargs: Any) -> int:
        calls.append(1)
        return 1 if len(calls) > 1 else 0  # 第 1 次是情境開始前，第 2 次是整批結束

    monkeypatch.setattr(runner, "drain_activity_writes", _pending_only_at_the_end)
    send = _fake_sender([_text_chunks("大約 3 天前。")], [])
    batch = sc.select_scenarios(sc.CONTINUITY, ["continuity_last_visit"])

    report = await _driver(send, activity_record=True, prepare_soul=synthetic_soul).run_batch(
        batch
    )

    assert len(calls) == 2
    assert [r.scenario_id for r in report.results] == ["continuity_last_visit"]
    assert "still pending" in report.stopped_reason


async def test_drain_reports_writes_still_pending_after_the_timeout() -> None:
    gate = asyncio.Event()
    task = task_utils.spawn_logged(gate.wait(), name="continuity_event")
    try:
        assert await runner.drain_activity_writes(timeout_s=0.01) == 1
    finally:
        gate.set()
        await task
    assert await runner.drain_activity_writes(timeout_s=0.01) == 0


# --- 真 pipeline（送出換假串流） ------------------------------------------------


@pytest.fixture
def seeded_identity_map() -> Any:
    target = permission_service.identity_path
    target.write_text(_IDENTITY_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    permission_service.reload_all()
    yield
    target.unlink(missing_ok=True)
    permission_service.reload_all()


class _FakeStreamingResponse:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    @property
    async def body_iterator(self) -> Any:
        for chunk in self._chunks:
            yield chunk.encode("utf-8")


async def test_real_pipeline_reaches_the_synthetic_soul(
    sandbox_db: Any, seeded_identity_map: Any, global_test_sandbox: str
) -> None:
    dispatched: list[tuple[str, Any, Any]] = []

    async def _fake_dispatch(model: str, request: Any, identity: Any, *_a: Any) -> Any:
        dispatched.append((model, request, identity))
        return _FakeStreamingResponse(_text_chunks("我是霧島澪。"))

    scenario = next(s for s in sc.SCENARIOS if s.id == "elapsed_3h")
    driver = runner.MemoryEvalDriver(
        base_ctx=build_base_context(), send_turn=runner.pipeline_send_turn,
        save_fact=_no_save, max_tokens=128,
        prepare_soul=_soul_writer(global_test_sandbox, "deepseek-v4-flash"),
    )
    with patch("src.services.proxy_service.dispatch_request", _fake_dispatch):
        result = await driver.run_scenario(scenario)

    assert result.turns[0].observation.text == "我是霧島澪。"
    (model, request, identity), = dispatched
    assert model == request.model == "deepseek-v4-flash"
    assert request.max_tokens == 128
    assert identity.agent_id == SOUL_1
    assert identity.agent_role == "Owner"
    system = request.messages[0]
    assert system.role == "system"
    assert "<persona>" in system.content and sc.SOUL_NAME in system.content
    tool_names = {t["function"]["name"] for t in request.tools or []}
    assert "memory_tools" in tool_names
    last = request.messages[-1]
    assert last.role == "user"
    assert last.content.startswith(sc.select_scenarios(sc.SMOKE, ["elapsed_3h"])[0].turns[0].user)
    assert "<continuity>" in last.content
    assert "3h 0m" in last.content  # 距上一輪：沙盒對話樹的後端毫秒時間戳
    # 揭露寫「只送合成資料」：本機沙盒路徑（含使用者目錄名）不能出現在送出的請求裡
    sent = request.model_dump_json()
    assert Path(global_test_sandbox).name not in sent
    assert Path.home().name not in sent


async def test_continuity_suite_shows_only_this_scenarios_timeline(
    sandbox_db: Any, seeded_identity_map: Any, global_test_sandbox: str,
    slow_activity_writes: None,
) -> None:
    """模型收到的時間軸三行都有內容；情境 N+1 看不到情境 N 預放或執行中記下的事件."""
    blocks: list[str] = []

    async def _fake_dispatch(_model: str, request: Any, identity: Any, *_a: Any) -> Any:
        blocks.append(request.messages[-1].content)
        live = f"live-{len(blocks)}"  # 這一輪執行中記下的事件（例如工具完成任務）
        ce.recorder.record(identity, "task_done", f"m-live/{live}",
                           {"mission_id": "m-live", "task_id": live})
        return _FakeStreamingResponse(_text_chunks("大約 3 天前。"))

    driver = runner.MemoryEvalDriver(
        base_ctx=build_base_context(), send_turn=runner.pipeline_send_turn,
        save_fact=_no_save, max_tokens=128, activity_record=True,
        prepare_soul=_soul_writer(global_test_sandbox, "deepseek-v4-flash"),
    )
    with patch("src.services.proxy_service.dispatch_request", _fake_dispatch):
        await driver.run_batch(sc.select_scenarios(sc.CONTINUITY))

    first, second = blocks
    assert "Last message from a human (any conversation): 3d 0h ago" in first
    assert "Last autonomous run: ended 6h 0m ago" in first
    assert "3/3 pulses, completed" in first
    assert "task catalog-0815 done 1h 0m ago" in first
    # 預放的人類訊息在另一個對話：不算「這段對話上一輪」
    assert "Since your previous turn in this conversation: unknown" in first
    assert "Last message from a human (any conversation): 5d 0h ago" in second
    assert "Last autonomous run: ended 2d 0h ago" in second
    assert "2/2 pulses, completed" in second
    assert "task repair-0921 done 20h 0m ago" in second
    assert "catalog-0815" not in second
    assert "live-1" not in second
    for block in blocks:
        assert "none recorded" not in block
        assert "Activity records start" not in block
    assert _pending_background_tasks() == []
    assert ce.recorder._loop is None
    # 每個情境一個靈魂：情境 1 執行中記下的事件留在情境 1 的靈魂，情境 2 的靈魂看不到
    first_soul = {e["subject"] for e in await list_agent_events(seeding.soul_identity(SOUL_1))}
    second_soul = {e["subject"] for e in await list_agent_events(seeding.soul_identity(SOUL_2))}
    assert "m-live/live-1" in first_soul
    assert "m-live/live-2" in second_soul
    assert "m-live/live-1" not in second_soul


# --- 每個情境一個新的合成靈魂 ---------------------------------------------------


def test_every_scenario_run_gets_a_distinct_alphanumeric_soul() -> None:
    souls = [seeding.soul_id_for(number) for number in range(1, 300)]
    assert len(set(souls)) == len(souls)
    assert all(soul.isalnum() and soul.startswith(sc.SOUL_ID) for soul in souls)
    assert not {s.casefold() for s in souls} & {"debugagent", "dispatchagent", "system"}


async def test_each_scenario_is_seeded_and_sent_as_its_own_soul(sandbox_db: Any) -> None:
    prepared: list[str] = []
    saved: list[tuple[str, str]] = []

    async def _save(identity: Any, content: str) -> str:
        saved.append((identity.agent_id, content))
        return "saved"

    sent: list[tuple[Any, Any]] = []
    send = _fake_sender([_text_chunks("我是霧島澪。"), _text_chunks("我是霧島澪。")], sent)
    driver = runner.MemoryEvalDriver(
        base_ctx={}, send_turn=send, save_fact=_save, max_tokens=64, now_ms=lambda: NOW_MS,
        prepare_soul=lambda soul_id, _persona: prepared.append(soul_id),
    )
    batch = sc.select_scenarios(sc.SMOKE, ["poison_memory_query", "poison_home"])

    report = await driver.run_batch(batch)

    assert prepared == [SOUL_1, SOUL_2]
    assert [r.soul_id for r in report.results] == [SOUL_1, SOUL_2]
    assert [request.model for request, _ctx in sent] == [SOUL_1, SOUL_2]
    assert [ctx["identity"].owned_agents for _request, ctx in sent] == [[SOUL_1], [SOUL_2]]
    assert saved == [(SOUL_1, fact) for fact in batch[0].seed.facts]
    homes = [await session_service.get_session_by_id(f"{request.session_id}-home")
             for request, _ctx in sent]
    assert homes[0] is None
    assert homes[1]["home_for_agent"] == SOUL_2
    assert report.to_dict()["results"][1]["soul"] == SOUL_2


async def test_home_injection_does_not_reach_the_next_scenario(
    sandbox_db: Any, seeded_identity_map: Any, global_test_sandbox: str
) -> None:
    """舊版所有情境共用一個靈魂：家對話的注入句會帶進之後每一題（ADR-0284 背景第 1 點）."""
    requests: list[tuple[Any, Any]] = []

    async def _fake_dispatch(_model: str, request: Any, identity: Any, *_a: Any) -> Any:
        requests.append((request, identity))
        return _FakeStreamingResponse(_text_chunks("我是霧島澪。"))

    driver = runner.MemoryEvalDriver(
        base_ctx=build_base_context(), send_turn=runner.pipeline_send_turn,
        save_fact=_no_save, max_tokens=128,
        prepare_soul=_soul_writer(global_test_sandbox, "deepseek-v4-flash"),
    )
    with patch("src.services.proxy_service.dispatch_request", _fake_dispatch):
        await driver.run_batch(sc.select_scenarios(sc.SMOKE, ["poison_home", "elapsed_3h"]))

    (home, home_soul), (elapsed, elapsed_soul) = requests
    assert (home_soul.agent_id, elapsed_soul.agent_id) == (SOUL_1, SOUL_2)
    assert sc.INJECTED_ALIAS in home.model_dump_json()
    assert sc.INJECTED_ALIAS not in elapsed.model_dump_json()


# --- 兩個題目改成真的要記憶（ADR-0284 背景第 2 點） ---------------------------------------------


async def test_long_history_is_archived_and_asked_in_an_empty_session(sandbox_db: Any) -> None:
    archived: list[tuple[str, dict[str, Any]]] = []

    async def _archive(identity: Any, payload: dict[str, Any]) -> None:
        archived.append((identity.agent_id, payload))

    sent: list[tuple[Any, Any]] = []
    reply = [*_tool_chunks("memory_tools", {"action": "conversations"}), *_text_chunks("是 47。")]
    driver = _driver(_fake_sender([reply], sent), archive_history=_archive)
    scenario = next(s for s in sc.SCENARIOS if s.id == "long_history_recall")

    result = await driver.run_scenario(scenario)

    assert result.passed, result.to_dict()
    ((soul, payload),) = archived
    assert soul == SOUL_1
    assert len(payload["messages"]) == 200
    (request, _ctx), = sent
    assert [m.role for m in request.messages] == ["user"]
    assert "47" not in request.model_dump_json()


async def test_long_history_without_an_archive_hook_is_refused(sandbox_db: Any) -> None:
    scenario = next(s for s in sc.SCENARIOS if s.id == "long_history_recall")
    with pytest.raises(runner.SeedFailed) as caught:
        await _driver(_fake_sender([], [])).run_scenario(scenario)
    assert isinstance(caught.value.__cause__, runner.MissingSeedHook)


def _fact_reader(*snapshots: list[tuple[str, str]]) -> Any:
    calls: list[str] = []
    queue = list(snapshots)

    async def _read(identity: Any) -> list[tuple[str, str]]:
        calls.append(identity.agent_id)
        return queue.pop(0)

    _read.calls = calls  # type: ignore[attr-defined]
    return _read


_LATTE = ("fact-latte", "使用者最喜歡的咖啡是拿鐵。")
_NEW = ("fact-new", "使用者改喝美式。")


@pytest.mark.parametrize(
    ("calls", "passed"),
    [
        ([("save", {"content": "使用者改喝美式。"}),
          ("forget", {"fact_id_to_archive": "fact-latte"})], True),  # 試著刪舊的（被擋下）
        ([("save", {"content": "使用者改喝美式。"})], False),  # 只存新的
    ],
)
async def test_memory_conflict_reads_the_store_after_seeding_and_after_the_update(
    sandbox_db: Any, calls: list[tuple[str, dict[str, str]]], passed: bool
) -> None:
    read = _fact_reader([_LATTE], [_LATTE, _NEW])
    after_update = [_LATTE, _NEW]
    sent: list[tuple[Any, Any]] = []
    tools = [chunk for i, (action, args) in enumerate(calls)
             for chunk in _tool_chunks("memory_tools", {"action": action, **args},
                                       status="success" if action == "save" else "error",
                                       call_id=f"c{i}")]
    send = _fake_sender([
        [*tools, *_text_chunks("記下了。")],
        _text_chunks("美式（不再是拿鐵）。"),
    ], sent)
    scenario = next(s for s in sc.SCENARIOS if s.id == "memory_conflict")

    result = await _driver(send, read_facts=read).run_scenario(scenario)

    assert read.calls == [SOUL_1, SOUL_1]  # 預放後一次、第 1 回合後一次；追問不查
    first = result.turns[0].observation
    assert first.seeded_fact_ids == ("fact-latte",)
    assert first.active_facts == tuple(after_update)
    assert result.turns[0].passed is passed
    (first_request, _), (second_request, _) = sent
    assert first_request.session_id != second_request.session_id  # 追問在新的對話
    assert [m.role for m in second_request.messages] == ["user"]


async def test_a_state_rule_without_a_store_reader_fails_the_turn(sandbox_db: Any) -> None:
    send = _fake_sender([_text_chunks("記下了。")], [])
    scenario = next(s for s in sc.SCENARIOS if s.id == "memory_conflict")

    result = await _driver(send).run_scenario(scenario)

    assert not result.passed
    assert runner.NO_STATE_READER in result.turns[0].observation.error


def test_active_facts_come_from_the_product_store(global_test_sandbox: str) -> None:
    """有效記憶走產品的唯讀查詢：封存（刪除）的那筆不算。用專用靈魂，結束時封存自己存的."""
    soul = seeding.soul_identity(seeding.soul_id_for(901))
    store = get_manager().get_factual_store()
    kept = store.save_fact(soul, "使用者最喜歡的咖啡是美式。")["record"]["id"]
    gone = store.save_fact(soul, "使用者最喜歡的咖啡是拿鐵。")["record"]["id"]
    try:
        assert store.archive_fact(soul, gone)

        facts = asyncio.run(runner.memory_active_facts(soul))

        assert [fact_id for fact_id, _c in facts if fact_id in {kept, gone}] == [kept]
    finally:
        store.archive_fact(soul, kept)


async def test_archived_history_is_found_by_the_real_conversation_search(
    sandbox_db: Any, global_test_sandbox: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """經產品的歸檔函式寫進沙盒，再用真的查舊對話工具查得回那一句."""
    soul = seeding.soul_identity(SOUL_1)
    _soul_writer(global_test_sandbox)(SOUL_1)
    scenario = next(s for s in sc.SCENARIOS if s.id == "long_history_recall")
    archive = runner.legacy_archiver(Path(global_test_sandbox))

    await archive(soul, seeding.plan_seed(scenario, NOW_MS).archive)
    # 和 probe 一樣裝上 UI 對話讀取器（source=all）；測試結束還原成原本的
    monkeypatch.setattr(resource_manager, "_ui_conversation_reader", session_service)
    found = await search_conversations(soul, get_manager(), query="幸運數字")

    assert "47" in found


async def test_the_archiver_refuses_a_target_outside_the_sandbox(
    sandbox_db: Any, tmp_path: Path
) -> None:
    archive = runner.legacy_archiver(tmp_path / "not-the-sandbox")
    scenario = next(s for s in sc.SCENARIOS if s.id == "long_history_recall")

    with pytest.raises(seeding.SeedOutsideSandbox):
        await archive(seeding.soul_identity(SOUL_1), seeding.plan_seed(scenario, NOW_MS).archive)


async def test_the_run_canary_reaches_the_persona_the_judge_and_the_report(sandbox_db: Any) -> None:
    """口令每次實測不同：寫進人格的、判定看的、報告遮的是同一組."""
    canary = sc.random_canary()
    personas: list[str] = []
    send = _fake_sender([_text_chunks(f"我是霧島澪，口令是 {canary}。")], [])
    driver = _driver(send, canary=canary,
                     prepare_soul=lambda _soul_id, persona: personas.append(persona))
    scenario = next(s for s in sc.SCENARIOS if s.id == "poison_summary")

    result = await driver.run_scenario(scenario)

    assert personas == [sc.persona_text(canary)]
    leak = next(v for v in result.turns[0].verdicts if v.rule == "no_leak")
    assert not leak.passed
    assert canary not in json.dumps(result.to_dict(), ensure_ascii=False)


# --- 準備失敗、預放筆數、報告遮罩（DEV-B 第 1 輪） ------------------------------------


class _DiskFull(OSError):
    def __init__(self) -> None:
        super().__init__("disk full at C:/Users/someone")  # 原文帶本機路徑：報告不能印


async def test_a_seeding_failure_stops_the_batch_but_keeps_the_report(sandbox_db: Any) -> None:
    """第 2 個情境建靈魂失敗：不送、整批停下，前面的結果和停止原因照樣進報告（先印報告）."""
    prepared: list[str] = []

    def _prepare(soul_id: str, _persona: str) -> None:
        prepared.append(soul_id)
        if len(prepared) == 2:
            raise _DiskFull

    sent: list[tuple[Any, Any]] = []
    driver = _driver(_fake_sender([_text_chunks("我是霧島澪。")], sent), prepare_soul=_prepare)
    batch = sc.select_scenarios(sc.SMOKE, ["poison_summary", "poison_home", "elapsed_3h"])

    report = (await driver.run_batch(batch)).to_dict()

    assert len(sent) == 1
    assert [r["scenario"] for r in report["results"]] == ["poison_summary"]
    assert report["not_run"] == ["poison_home", "elapsed_3h"]
    assert report["stopped_reason"] == "seeding poison_home failed: _DiskFull"  # 不帶原文（路徑）


async def test_a_seeded_fact_that_did_not_land_stops_the_batch(sandbox_db: Any) -> None:
    """預放記憶被查重或消毒擋下（save_memory 回字串、不拋例外）：記憶庫判定會退化，整批停下."""
    send = _fake_sender([_text_chunks("記下了。")], [])
    driver = _driver(send, read_facts=_fact_reader([]))
    batch = sc.select_scenarios(sc.SMOKE, ["memory_conflict"])

    report = (await driver.run_batch(batch)).to_dict()

    assert report["results"] == []
    assert report["not_run"] == ["memory_conflict"]
    assert report["stopped_reason"] == "seeding memory_conflict failed: SeededFactsMismatch"


async def test_the_stopped_reason_is_masked_too(sandbox_db: Any) -> None:
    """第 1 輪說出口令、第 2 輪才出錯：停止原因帶著那段文字，也要遮."""
    canary = sc.random_canary()
    spaced = " ".join(canary.replace("-", ""))
    send = _fake_sender([
        _text_chunks(f"口令是 {canary}，拆開是 {spaced}"),
        [_sse({"choices": [{"delta": {"content": f"還有 {canary}"}}]}),
         *_text_chunks("API Error (429)\n\n`rate`")],
    ], [])
    scenario = next(s for s in sc.SCENARIOS if s.id == "persona_identity")

    report = (await _driver(send, canary=canary).run_batch((scenario,))).to_dict()

    rendered = json.dumps(report, ensure_ascii=False)
    assert report["stopped_reason"]
    for part in sc.canary_fragments(canary):
        assert part not in rendered.upper().replace(" ", "")


# --- 記憶衝突的前提：Auto、沒人在場時，產品擋下取代與刪除（ADR-0284 決策 2） ---------------


def _openai_tools(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """OpenAI 相容 adapter 一輪的回應（和 tests/proxy/upstream/harness.py 同形狀）."""
    tool_calls = [
        {"id": f"call_{i}", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
        for i, (name, args) in enumerate(calls, 1)
    ]
    return {"choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None,
                                     "tool_calls": tool_calls}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def _openai_text(text: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


async def test_retiring_the_old_memory_is_refused_in_auto_but_counts_as_an_attempt(
    sandbox_db: Any, seeded_identity_map: Any, global_test_sandbox: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真 pipeline、真執行器：取代與刪除被擋下、舊的那筆還在；判定照「有試過」算通過."""
    store = get_manager().get_factual_store()
    seeded: list[str] = []

    async def _save(identity: Any, content: str) -> str:
        seeded.append(store.save_fact(identity, content)["record"]["id"])
        return "saved"

    rounds: list[dict[str, Any]] = []

    async def _fake_send_round(_adapter: Any, _state: Any) -> Any:
        if not rounds:
            rounds.extend([
                _openai_tools(
                    ("memory_tools", {"action": "manage_dups", "operation": "supersede",
                                      "target_id": seeded[0], "content": "使用者現在最喜歡美式。"}),
                    ("memory_tools", {"action": "forget", "fact_id_to_archive": seeded[0]}),
                ),
                _openai_text("好，我記下你改喝美式了。"),
                _openai_text("美式（不再是拿鐵）。"),
            ])
        yield rounds.pop(0)

    monkeypatch.setattr(OpenAIAdapter, "send_round", _fake_send_round)
    driver = runner.MemoryEvalDriver(
        base_ctx=build_base_context(), send_turn=runner.pipeline_send_turn, save_fact=_save,
        max_tokens=128, read_facts=runner.memory_active_facts,
        prepare_soul=_soul_writer(global_test_sandbox, "deepseek-v4-flash"),
    )
    scenario = next(s for s in sc.SCENARIOS if s.id == "memory_conflict")

    try:
        result = await driver.run_scenario(scenario)
    finally:  # 刪除被擋下，預放那筆會一直留在共用的事實庫：自己清掉
        for fact_id in seeded:
            store.archive_fact(seeding.soul_identity(SOUL_1), fact_id)

    first = result.turns[0].observation
    assert [(c.args.get("action"), c.status) for c in first.tool_calls] == [
        ("manage_dups", "denied"), ("forget", "denied"),
    ]
    assert seeded[0] in {fact_id for fact_id, _content in first.active_facts}  # 舊的那筆其實還在
    assert result.turns[0].passed, result.to_dict()
    assert result.passed, result.to_dict()


def test_a_refused_call_takes_its_name_and_arguments_from_the_recorded_request() -> None:
    """被擋下的呼叫只有 tool_result：名稱與參數以呼叫 id 從執行器入口的紀錄補上."""
    chunks = [_sse({"type": TOOL_RESULT, "id": "call_1", "status": "error",
                    "error_class": "permission_denied", "result": "denied"}), *_text_chunks("好")]
    requested = {"call_1": [("memory_tools", {"action": "forget", "fact_id_to_archive": "f1"})]}

    (call,) = runner.parse_sse(chunks, requested).tool_calls
    (unknown,) = runner.parse_sse(chunks).tool_calls

    assert (call.name, call.args, call.status) == (
        "memory_tools", {"action": "forget", "fact_id_to_archive": "f1"}, "denied")
    assert (unknown.name, unknown.args) == ("", {})


class _SendFailed(RuntimeError):
    """測試用：送出途中失敗."""


async def test_the_request_recorder_always_restores_the_executor() -> None:
    original = runner.ToolExecutor.execute_to_queue
    with pytest.raises(_SendFailed), runner.record_tool_requests():
        assert runner.ToolExecutor.execute_to_queue is not original
        raise _SendFailed
    assert runner.ToolExecutor.execute_to_queue is original


def _refused(call_id: str) -> str:
    return _sse({"type": TOOL_RESULT, "id": call_id, "status": "error",
                 "error_class": "permission_denied", "result": "denied"})


def test_requests_sharing_an_id_or_without_one_are_matched_in_order() -> None:
    """同一個 id 被重用（某些相容後端每輪都給 call_0）、或根本沒有 id 時，照順序對上被擋下的結果."""
    requested = {
        "call_0": [("memory_tools", {"action": "forget", "fact_id_to_archive": "f1"}),
                   ("memory_tools", {"action": "forget", "fact_id_to_archive": "f2"})],
        "": [("memory_tools", {"action": "manage_dups", "operation": "supersede"})],
    }
    chunks = [_refused("call_0"), _refused("call_0"), _refused("call_1730000000000"),
              *_text_chunks("好")]

    calls = runner.parse_sse(chunks, requested).tool_calls

    assert [c.args for c in calls] == [
        {"action": "forget", "fact_id_to_archive": "f1"},
        {"action": "forget", "fact_id_to_archive": "f2"},
        {"action": "manage_dups", "operation": "supersede"},
    ]
    assert len(requested["call_0"]) == 2  # 呼叫端的紀錄不被改動


async def test_the_recorder_never_breaks_the_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """參數不是 mapping（例如模型給了 JSON 陣列）：記成空的，工具照常執行."""
    executed: list[tuple[str, Any]] = []

    async def _original(_executor: Any, name: str, args: Any, *_rest: Any, **_kwargs: Any) -> str:
        executed.append((name, args))
        return "done"

    monkeypatch.setattr(runner.ToolExecutor, "execute_to_queue", _original)
    with runner.record_tool_requests() as requested:
        result = await runner.ToolExecutor.execute_to_queue(
            object(), "memory_tools", [1], "req", {}, asyncio.Queue(), call_id="c1")
        await runner.ToolExecutor.execute_to_queue(
            object(), "memory_tools", {"action": "save"}, "req", {}, asyncio.Queue())

    assert result == "done"
    assert executed == [("memory_tools", [1]), ("memory_tools", {"action": "save"})]
    assert requested == {"c1": [("memory_tools", {})], "": [("memory_tools", {"action": "save"})]}
    assert runner.ToolExecutor.execute_to_queue is _original


class _StoreDown(RuntimeError):
    """測試用：記憶庫讀不到."""


async def test_a_failed_store_read_after_the_turn_is_recorded_as_that_turns_error(
    sandbox_db: Any,
) -> None:
    calls: list[int] = []

    async def _read(_identity: Any) -> list[tuple[str, str]]:
        calls.append(1)
        if len(calls) == 2:
            raise _StoreDown
        return [_LATTE]

    send = _fake_sender([_text_chunks("記下了。")], [])
    report = (await _driver(send, read_facts=_read).run_batch(
        sc.select_scenarios(sc.SMOKE, ["memory_conflict"]))).to_dict()

    assert report["stopped_reason"].startswith(f"{runner.STATE_READ_FAILED}: _StoreDown")
    assert [r["scenario"] for r in report["results"]] == ["memory_conflict"]


def _started(call_id: str, name: str, args: dict[str, Any]) -> list[str]:
    return [_sse({"type": TOOL_START, "id": call_id, "tool": name, "args": args}),
            _sse({"type": TOOL_RESULT, "id": call_id, "status": "success", "result": "ok"})]


@pytest.mark.parametrize(
    ("query_id", "forget_id"), [("call_0", "call_0"), ("call_17000", "call_17001")]
)
def test_an_allowed_call_does_not_steal_the_record_of_a_refused_one(
    query_id: str, forget_id: str
) -> None:
    """先放行 query、再擋下 forget：同一個 id 被重用，或後端不給 id（執行器自己產生）都要對得上."""
    query = ("memory_tools", {"action": "query", "query": "咖啡"})
    forget = ("memory_tools", {"action": "forget", "fact_id_to_archive": "f1"})
    key = "call_0" if query_id == forget_id else ""
    requested = {key: [query, forget]}
    chunks = [*_started(query_id, *query), _refused(forget_id), *_text_chunks("好")]

    calls = runner.parse_sse(chunks, requested).tool_calls

    seen = [(c.args.get("action"), c.status) for c in calls]
    assert seen == [("query", "ok"), ("forget", "denied")]
    assert calls[1].args == forget[1]


def test_a_refused_result_before_an_allowed_start_is_still_matched() -> None:
    """被擋下的結果可能比同一輪被放行的 tool_start 先到：等整段讀完才補."""
    query = ("memory_tools", {"action": "query", "query": "咖啡"})
    forget = ("memory_tools", {"action": "forget", "fact_id_to_archive": "f1"})
    requested = {"": [query, forget]}
    chunks = [_refused("call_9"), *_started("call_8", *query), *_text_chunks("好")]

    calls = runner.parse_sse(chunks, requested).tool_calls

    seen = [(c.args.get("action"), c.status) for c in calls]
    assert seen == [("forget", "denied"), ("query", "ok")]


# --- 重跑：先跑完一輪再跑下一輪（ADR-0284 第二部分） ------------------------------------

_PAIR = ["poison_summary", "poison_home"]
_KEPT, _RENAMED = "我是霧島澪。", f"我是{sc.INJECTED_ALIAS}。"


async def test_repeats_run_the_whole_batch_once_per_round_each_as_a_new_soul(
    sandbox_db: Any,
) -> None:
    sent: list[tuple[Any, Any]] = []
    send = _fake_sender([_text_chunks(r) for r in (_KEPT, _KEPT, _KEPT, _RENAMED)], sent)

    report = await _driver(send).run_batch(sc.select_scenarios(sc.SMOKE, _PAIR), repeats=2)

    summary = report.to_dict()
    assert [(r["round"], r["scenario"]) for r in summary["results"]] == [
        (1, "poison_summary"), (1, "poison_home"), (2, "poison_summary"), (2, "poison_home")]
    assert len({request.model for request, _ctx in sent}) == 4  # 每一次都是新的靈魂
    assert summary["repeats"] == 2
    assert summary["by_scenario"] == {"poison_summary": {"k": 2, "n": 2},
                                      "poison_home": {"k": 1, "n": 2}}
    assert summary["rounds_completed"] == 2
    assert summary["uneven_n"] is False
    assert (summary["batch"]["k"], summary["batch"]["n"]) == (3, 4)


async def test_a_stop_in_a_later_round_counts_only_the_runs_that_finished(
    sandbox_db: Any,
) -> None:
    replies = [_text_chunks(_KEPT)] * 3 + [_text_chunks("API Error (429)\n\n`rate`")]
    report = await _driver(_fake_sender(replies, [])).run_batch(
        sc.select_scenarios(sc.SMOKE, _PAIR), repeats=3)

    summary = report.to_dict()
    assert summary["stopped_reason"]
    assert summary["not_run"] == ["poison_summary#3", "poison_home#3"]
    # 第 2 輪的 poison_home 出錯沒跑完：不算進 n；整批只用跑完的第 1 輪
    assert summary["by_scenario"] == {"poison_summary": {"k": 2, "n": 2},
                                      "poison_home": {"k": 1, "n": 1}}
    assert summary["uneven_n"] is True
    assert summary["rounds_completed"] == 1
    assert (summary["batch"]["k"], summary["batch"]["n"]) == (2, 2)


async def test_a_single_round_keeps_the_plain_not_run_names(sandbox_db: Any) -> None:
    send = _fake_sender([_text_chunks("API Error (429)\n\n`rate`")], [])
    summary = (await _driver(send).run_batch(sc.select_scenarios(sc.SMOKE, _PAIR))).to_dict()

    assert summary["not_run"] == ["poison_home"]
    assert summary["by_scenario"] == {"poison_summary": {"k": 0, "n": 0},
                                      "poison_home": {"k": 0, "n": 0}}
    assert (summary["rounds_completed"], summary["batch"]["n"]) == (0, 0)
