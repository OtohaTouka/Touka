"""記憶評估多輪 driver 與合成資料（離線：假串流、沙盒 session DB、零下載）。

最後一個測試走真 pipeline（Monitor→…→Tool 各層），只把送出換成假串流，確認人工 probe
真的打到合成靈魂：人格、記憶工具、連續性區塊、身分都對（scorecard 打不到靈魂是 F2）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from src.main import build_base_context
from src.services.permission_service import permission_service
from src.utils.async_db import init_db_async, session_service
from src.utils.auth_mode import mode_for_acting_agent
from src.utils.brain_persistence import set_active_brain
from src.utils.history_manager import TreeMessageManager
from src.utils.tool_events import TOOL_RESULT, TOOL_START

from tests.memory_eval import runner, seeding
from tests.memory_eval import scenarios as sc

if TYPE_CHECKING:
    from collections.abc import Callable

_IDENTITY_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "identity_map.json.example"
)
NOW_MS = 1_760_500_000_000


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _text_chunks(text: str) -> list[str]:
    return [_sse({"choices": [{"delta": {"content": text}}]}), "data: [DONE]\n\n"]


def _tool_chunks(name: str, args: dict[str, Any], status: str = "success") -> list[str]:
    return [
        _sse({"type": TOOL_START, "id": "c1", "tool": name, "args": args}),
        _sse({"type": TOOL_RESULT, "id": "c1", "tool": name, "status": status, "result": "ok"}),
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


@pytest.mark.parametrize("text", ["❌ API Error (429)\n\n`rate`", "連線錯誤：timeout"])
def test_parse_sse_marks_provider_errors(text: str) -> None:
    assert runner.parse_sse(_text_chunks(text)).error


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


def test_seed_plan_preloads_history_with_backend_timestamps() -> None:
    scenario = next(s for s in sc.SCENARIOS if s.id == "long_history_recall")

    plan = seeding.plan_seed(scenario, NOW_MS)
    path = TreeMessageManager.get_linear_path(plan.session_tree)

    assert len(path) == 200
    assert [n["role"] for n in path[:2]] == ["user", "assistant"]
    assert scenario.seed.early_fact in path[4]["content"]  # 第 3 輪的 user
    assert path[-1]["timestamp"] == NOW_MS - 5 * 60 * 1000
    stamps = [n["timestamp"] for n in path]
    assert stamps == sorted(stamps)
    assert all(isinstance(s, int) for s in stamps)


def test_seed_plan_without_age_has_no_timestamps() -> None:
    scenario = next(s for s in sc.SCENARIOS if s.id == "poison_home")

    plan = seeding.plan_seed(scenario, NOW_MS)

    assert TreeMessageManager.get_linear_path(plan.session_tree) == []
    home = TreeMessageManager.get_linear_path(plan.home_tree)
    assert [n["role"] for n in home] == ["user", "assistant"]
    assert sc.FAKE_CLOSE in home[1]["content"]


def test_synthetic_soul_is_written_only_under_the_sandbox_root(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    soul_dir = seeding.write_synthetic_soul(root / "app", sandbox_root=root)

    assert (soul_dir / "soul.md").read_text(encoding="utf-8") == sc.PERSONA_TEXT
    config = json.loads((soul_dir / "config.json").read_text(encoding="utf-8"))
    assert config["is_toukasoul"] is True
    assert set(config["enabled_tools"]) == {"memory_tools", "mission_tools"}
    with pytest.raises(seeding.SeedOutsideSandbox):
        seeding.write_synthetic_soul(tmp_path / "elsewhere", sandbox_root=root)


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


# --- 多輪 driver --------------------------------------------------------------


async def test_driver_runs_multi_turn_with_history_and_persists_tree(sandbox_db: Any) -> None:
    scenario = next(s for s in sc.SCENARIOS if s.id == "persona_identity")
    sent: list[tuple[Any, Any]] = []
    send = _fake_sender([_text_chunks("我是霧島澪。"), _text_chunks("我還是澪。")], sent)

    result = await _driver(send).run_scenario(scenario)

    assert result.passed, result.to_dict()
    assert len(sent) == 2
    first, second = (request for request, _ctx in sent)
    assert first.model == sc.SOUL_ID
    assert first.max_tokens == 256
    assert first.session_id == second.session_id
    assert [m.role for m in second.messages] == ["user", "assistant", "user"]
    assert second.messages[1].content == "我是霧島澪。"
    ctx = sent[0][1]
    assert ctx["identity"].is_human
    assert ctx["identity"].owned_agents == [sc.SOUL_ID]
    stored = await session_service.get_session_by_id(first.session_id)
    path = TreeMessageManager.get_linear_path(stored["messages"])
    assert [n["content"] for n in path] == [
        scenario.turns[0].user, "我是霧島澪。", scenario.turns[1].user, "我還是澪。",
    ]
    assert all(n["timestamp"] == NOW_MS for n in path)
    assert stored["agent_id"] == sc.SOUL_ID
    # ToolLayer 用同一個函式從 session 解析這次執行的模式（ADR-0246）
    resolved = mode_for_acting_agent(
        stored["auth_level"], bound_agent=sc.SOUL_ID, acting_agent=sc.SOUL_ID
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
    saved: list[str] = []

    async def _save(identity: Any, content: str) -> str:
        assert identity.agent_id == sc.SOUL_ID
        saved.append(content)
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
    await driver.seed(conflict, "seed-only-session")
    assert saved == list(conflict.seed.facts)


async def test_batch_stops_after_the_first_provider_error(sandbox_db: Any) -> None:
    sent: list[tuple[Any, Any]] = []
    send = _fake_sender([_text_chunks("❌ API Error (429)\n\n`rate`")], sent)
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
    seeding.write_synthetic_soul(Path(global_test_sandbox), sandbox_root=Path(global_test_sandbox))
    set_active_brain(seeding.soul_identity(), "deepseek-v4-flash")
    dispatched: list[tuple[str, Any, Any]] = []

    async def _fake_dispatch(model: str, request: Any, identity: Any, *_a: Any) -> Any:
        dispatched.append((model, request, identity))
        return _FakeStreamingResponse(_text_chunks("我是霧島澪。"))

    scenario = next(s for s in sc.SCENARIOS if s.id == "elapsed_3h")
    driver = runner.MemoryEvalDriver(
        base_ctx=build_base_context(), send_turn=runner.pipeline_send_turn,
        save_fact=_no_save, max_tokens=128,
    )
    with patch("src.services.proxy_service.dispatch_request", _fake_dispatch):
        result = await driver.run_scenario(scenario)

    assert result.turns[0].observation.text == "我是霧島澪。"
    (model, request, identity), = dispatched
    assert model == request.model == "deepseek-v4-flash"
    assert request.max_tokens == 128
    assert identity.agent_id == sc.SOUL_ID
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
