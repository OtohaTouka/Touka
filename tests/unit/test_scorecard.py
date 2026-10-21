"""TDD — 軌B 批次1：計分卡引擎（純函式，假 runner，零 API 花費）。

涵蓋 Budget（回合間累計上限）、evaluate（結構驗證）、run_scorecard（編排＋預算略過）、
formatter（md+json）、cost_meter（token_usage.json 快照差額）、scenarios（篩選）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from src.models.identity import AgentIdentity
from src.services.resource_manager import resource_manager
from src.services.scorecard import cost_meter, formatter
from src.services.scorecard.engine import Budget, evaluate, run_scorecard
from src.services.scorecard.models import RunOutcome, Scenario
from src.services.scorecard.runner import _parse_events
from src.services.scorecard.scenarios import CORE_SCENARIOS, select_scenarios

_PIPELINE_ERR = RuntimeError("pipeline down")


def _fake_runner(outcomes: dict[str, RunOutcome]):
    async def runner(_model: str, scenario: Scenario) -> RunOutcome:
        return outcomes.get(scenario.id, RunOutcome(text="ok", tokens=10, cost_usd=0.0))
    return runner


# ---- Budget ----

def test_budget_exceeded_boundary():
    b = Budget(1.0)
    assert b.exceeded() is False
    b.add(0.999)
    assert b.exceeded() is False
    b.add(0.001)  # 累計剛好等於上限
    assert b.exceeded() is True


def test_budget_zero_cap_means_unlimited():
    b = Budget(0.0)
    b.add(9999.0)
    assert b.exceeded() is False


# ---- evaluate（結構驗證）----

def test_evaluate_pass_on_text():
    sc = Scenario(id="c", surface="chat", prompt="hi")
    r = evaluate(sc, RunOutcome(text="hello", tokens=5, cost_usd=0.01, latency_ms=12))
    assert r.structural_pass is True and r.detail == "ok"
    assert r.tokens == 5 and r.cost_usd == 0.01 and r.latency_ms == 12


def test_evaluate_fail_on_empty_text():
    sc = Scenario(id="c", surface="chat")
    assert evaluate(sc, RunOutcome(text="   ")).structural_pass is False


def test_evaluate_fail_on_error():
    sc = Scenario(id="c", surface="chat")
    r = evaluate(sc, RunOutcome(text="hi", error="boom"))
    assert r.structural_pass is False and "boom" in r.detail


def test_evaluate_fail_on_missing_expected_tool():
    sc = Scenario(id="t", surface="react", expect_tool="query_memory")
    r = evaluate(sc, RunOutcome(text="done", tools_called=("other_tool",)))
    assert r.structural_pass is False and "query_memory" in r.detail


def test_evaluate_pass_when_expected_tool_called():
    sc = Scenario(id="t", surface="react", expect_tool="query_memory")
    r = evaluate(sc, RunOutcome(text="done", tools_called=("query_memory",)))
    assert r.structural_pass is True


def test_evaluate_matches_mcp_prefixed_tool_name():
    """MCP 工具名為 `{server}__{tool}`；expect_tool 以後綴比對（Batch 4 docgen 等）。"""
    sc = Scenario(id="t", surface="react", expect_tool="create_xlsx")
    ok = evaluate(sc, RunOutcome(text="done", tools_called=("mcp-docgen__create_xlsx",)))
    assert ok.structural_pass is True
    # 不誤判：不同工具的後綴不該命中
    bad = evaluate(sc, RunOutcome(text="done", tools_called=("mcp-docgen__create_docx",)))
    assert bad.structural_pass is False


# ---- run_scorecard（編排＋預算略過）----

@pytest.mark.asyncio
async def test_run_scorecard_all_pass():
    scenarios = (Scenario("a", "chat"), Scenario("b", "chat"))
    runner = _fake_runner({
        "a": RunOutcome(text="x", tokens=10, cost_usd=0.01),
        "b": RunOutcome(text="y", tokens=20, cost_usd=0.02),
    })
    card = await run_scorecard("m", 1.0, scenarios, runner)
    assert card.passed == 2 and card.total == 2 and card.skipped == 0
    assert card.total_tokens == 30
    assert card.spent_usd == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_run_scorecard_skips_after_budget_exhausted():
    """超支後不再開新情境（completeness-2：回合間檢查；一個進行中情境可超支）。"""
    scenarios = (Scenario("a", "chat"), Scenario("b", "chat"), Scenario("c", "chat"))
    runner = _fake_runner({
        "a": RunOutcome(text="x", cost_usd=0.40),
        "b": RunOutcome(text="y", cost_usd=0.40),  # 跑完累計 0.80 > 0.50（容許單輪超支）
        "c": RunOutcome(text="z", cost_usd=0.40),
    })
    card = await run_scorecard("m", 0.50, scenarios, runner)
    assert card.passed == 2          # a、b 執行
    assert card.skipped == 1         # c 被略過（預算已耗盡）
    assert card.results[2].skipped is True
    assert "budget_exhausted" in card.results[2].detail


@pytest.mark.asyncio
async def test_run_scorecard_runner_exception_becomes_fail():
    async def boom(_m, _s):
        raise _PIPELINE_ERR
    card = await run_scorecard("m", 1.0, (Scenario("a", "chat"),), boom)
    assert card.passed == 0 and card.total == 1
    assert "pipeline down" in card.results[0].detail


@pytest.mark.asyncio
async def test_run_scorecard_progress_callback():
    events: list[dict] = []
    runner = _fake_runner({"a": RunOutcome(text="x", cost_usd=0.01)})
    await run_scorecard("m", 1.0, (Scenario("a", "chat"),), runner,
                        on_progress=events.append)
    assert events and events[-1]["status"] == "pass"


# ---- formatter ----

@pytest.mark.asyncio
async def test_formatter_json_and_markdown():
    runner = _fake_runner({"a": RunOutcome(text="x", tokens=10, cost_usd=0.01, latency_ms=5)})
    card = await run_scorecard("deepseek-chat", 1.0, (Scenario("a", "chat"),), runner)
    js = formatter.to_json(card)
    assert js["model"] == "deepseek-chat" and js["passed"] == 1
    assert js["results"][0]["scenario"] == "a"
    md = formatter.to_markdown(card)
    assert "模型計分卡：deepseek-chat" in md and "| a |" in md


# ---- cost_meter ----

def test_cost_meter_measure_reads_token_usage():
    ident = AgentIdentity.for_role(agent_id="ScorecardMeter", role="Owner")
    path = Path(resource_manager.get_stats_path(ident)) / "token_usage.json"
    resource_manager.write_json(path, {
        "Anthropic": {"prompt_tokens": 1000, "completion_tokens": 500, "total_cost_usd": 0.012},
        "DeepSeek": {"prompt_tokens": 200, "completion_tokens": 100, "total_cost_usd": 0.001},
    })
    cost, tokens = cost_meter.measure(ident)
    assert cost == pytest.approx(0.013) and tokens == 1800


def test_cost_meter_missing_file_returns_zero():
    ident = AgentIdentity.for_role(agent_id="ScorecardEmpty", role="Owner")
    assert cost_meter.measure(ident) == (0.0, 0)


# ---- scenarios ----

def test_select_scenarios_default_and_filter():
    assert select_scenarios(None) == CORE_SCENARIOS
    picked = select_scenarios(["chat_basic"])
    assert len(picked) == 1 and picked[0].id == "chat_basic"
    assert select_scenarios(["nonexistent"]) == ()


# ---- runner SSE 解析（token 取自串流 token_usage 事件，不靠 stats 差額）----

def test_parse_events_extracts_tokens_cost_text():
    chunks = [
        'data: {"type":"touka_progress","kind":"token_usage","total":1500,"cost":0.0042}\n\n',
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        "data: [DONE]\n\n",
    ]
    pe = _parse_events(chunks)
    assert pe.tokens == 1500
    assert pe.cost == 0.0042
    assert pe.text == "hi"
    assert not pe.error and pe.tools == ()


def test_parse_events_sums_multiturn_token_usage():
    chunks = [
        'data: {"type":"touka_progress","kind":"token_usage","total":100,"cost":0.001}\n\n',
        'data: {"type":"touka_progress","kind":"token_usage","total":200,"cost":0.002}\n\n',
    ]
    pe = _parse_events(chunks)
    assert pe.tokens == 300
    assert pe.cost == 0.003


def test_parse_events_detects_error_marker():
    chunks = ['data: {"choices":[{"delta":{"content":"Traceback (most recent call last):"}}]}\n\n']
    assert _parse_events(chunks).error


def test_parse_events_captures_provider_error_snippet():
    """上游錯誤（handler 當文字吐出）→ 帶出完整訊息，而非只回標記。"""
    inner = '"DeepSeek API Error (400): Model Not Exist"'
    chunks = ['data: {"choices":[{"delta":{"content":' + inner + "}}]}\n\n"]
    error = _parse_events(chunks).error
    assert "API Error (400)" in error
    assert "Model Not Exist" in error


# ---- 軌B 加強：telemetry 擷取（工具軌跡、ReAct 迴圈）＋事件契約 ----

def test_parse_events_captures_tool_trace_masked():
    """tool_start.args＋tool_result.result 依 id 配對成軌跡；secret 類鍵遮罩、過長截斷。"""
    chunks = [
        'data: {"type":"tool_start","id":"c1","tool":"memory_tools",'
        '"args":{"action":"search","query":"燈火","api_key":"sk-LEAK"}}\n\n',
        'data: {"type":"tool_result","id":"c1",'
        '"result":"找到 2 筆關於燈火的記憶","status":"success"}\n\n',
    ]
    pe = _parse_events(chunks)
    assert len(pe.tool_trace) == 1
    entry = pe.tool_trace[0]
    assert entry["name"] == "memory_tools"
    assert "燈火" in entry["args"] and "search" in entry["args"]
    assert "sk-LEAK" not in entry["args"]  # secret 鍵被遮罩
    assert entry["result"] == "找到 2 筆關於燈火的記憶"
    assert entry["status"] == "success"


def test_parse_events_truncates_long_trace():
    big = "x" * 5000
    chunks = [
        'data: {"type":"tool_start","id":"c1","tool":"research","args":{"query":"q"}}\n\n',
        'data: {"type":"tool_result","id":"c1","result":"' + big + '","status":"success"}\n\n',
    ]
    entry = _parse_events(chunks).tool_trace[0]
    assert len(entry["result"]) < 600 and entry["result"].endswith("(truncated)")


def test_parse_events_react_turns_and_finish():
    """react_slots 事件 → react_turns；finish_reason 取最後值；預算標記 → early_stop。"""
    chunks = [
        'data: {"type":"touka_progress","kind":"react_slots","current":1,"total":3}\n\n',
        'data: {"type":"touka_progress","kind":"react_slots","current":2,"total":3}\n\n',
        'data: {"choices":[{"delta":{"content":"⚠️ 已達總量上限（累計成本）"},'
        '"finish_reason":"stop"}]}\n\n',
    ]
    pe = _parse_events(chunks)
    assert pe.react_turns == 2
    assert pe.finish_reason == "stop"
    assert pe.early_stop is True


def test_parse_events_no_react_no_early_stop():
    chunk = 'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\n'
    pe = _parse_events([chunk])
    assert pe.react_turns == 0 and pe.early_stop is False and pe.finish_reason == "stop"


# ---- estimated 旗標透傳（上游未回報 usage 時的文字估算）----

def test_evaluate_threads_estimated_flag():
    sc = Scenario(id="c", surface="chat")
    r = evaluate(sc, RunOutcome(text="hi", tokens=50, cost_usd=0.001, estimated=True))
    assert r.structural_pass is True
    assert r.estimated is True


@pytest.mark.asyncio
async def test_formatter_json_includes_estimated():
    runner = _fake_runner({"a": RunOutcome(text="x", tokens=10, cost_usd=0.01, estimated=True)})
    card = await run_scorecard("m", 1.0, (Scenario("a", "chat"),), runner)
    js = formatter.to_json(card)
    assert js["results"][0]["estimated"] is True


@pytest.mark.asyncio
async def test_formatter_markdown_marks_estimated():
    runner = _fake_runner({"a": RunOutcome(text="x", tokens=10, cost_usd=0.01, estimated=True)})
    card = await run_scorecard("m", 1.0, (Scenario("a", "chat"),), runner)
    md = formatter.to_markdown(card)
    assert "~10" in md and "估算" in md


# ---- 批次3：wall-clock 逾時、not-testable 略過、情境擴充 ----

@pytest.mark.asyncio
async def test_run_scorecard_wall_clock_timeout():
    """情境超過 timeout_s → 記為失敗（防 ask_user/失控迴圈卡死），不中斷整輪。"""
    async def slow(_m, _s):
        await asyncio.sleep(5)
        return RunOutcome(text="late")

    sc = Scenario("slow", "chat", timeout_s=0.05)
    card = await run_scorecard("m", 1.0, (sc,), slow)
    assert card.passed == 0
    assert "timeout" in card.results[0].detail
    assert card.results[0].skipped is False  # 逾時算失敗，非略過


@pytest.mark.asyncio
async def test_run_scorecard_not_testable_skipped():
    """供應商/環境錯誤（4xx/連線/金鑰）→ 略過（not_testable），不算品質失敗。"""
    runner = _fake_runner({"a": RunOutcome(error="provider/env error: API Error (404)")})
    card = await run_scorecard("m", 1.0, (Scenario("a", "chat"),), runner)
    assert card.results[0].skipped is True
    assert "not_testable" in card.results[0].detail
    assert card.passed == 0 and card.total == 0  # total 排除略過


@pytest.mark.asyncio
async def test_run_scorecard_internal_error_is_fail_not_skip():
    """真正的內部錯誤（Traceback）→ 失敗，非略過（與環境不可用區分）。"""
    err = "internal error: Traceback (most recent call last)"
    runner = _fake_runner({"a": RunOutcome(error=err)})
    card = await run_scorecard("m", 1.0, (Scenario("a", "chat"),), runner)
    assert card.results[0].skipped is False
    assert card.passed == 0


def test_tool_scenario_declares_expect_tool():
    by_id = {s.id: s for s in CORE_SCENARIOS}
    assert by_id["tool_memory_recall"].expect_tool == "memory_tools"


def test_capability_scenarios_declared():
    """Batch 4：skill／網路搜尋／docgen 能力情境齊備；docgen 預啟用其技能。"""
    by_id = {s.id: s for s in CORE_SCENARIOS}
    assert by_id["skill_activation"].expect_tool == "activate_skill"
    assert by_id["web_search"].expect_tool == "research"
    docgen = by_id["docgen_xlsx"]
    assert docgen.expect_tool == "create_xlsx"
    assert docgen.skills == ("document-generator",)
