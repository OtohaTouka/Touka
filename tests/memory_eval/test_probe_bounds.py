"""人工記憶評估 probe 的硬界（全部離線：不傳核准旗標給任何子程序、不外連、不下載）。"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from memory_server import config_utils
from memory_server import vector as memory_vector
from memory_server.factual import FactualStore
from memory_server.services.manager import MemoryManager
from src.handlers import react_core
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.models.identity import AgentIdentity
from src.services import dialogue_summarizer, proxy_service, telemetry
from src.services.audit_service import PolicyAuditService
from src.services.permission_service import permission_service
from src.services.providers import pricing
from src.services.resource_manager import resource_manager
from src.services.review import review_loop
from src.utils.async_db import session_service
from src.utils.brain_persistence import get_active_brain

from tests.memory_eval import live_bounds, seeding, stats
from tests.memory_eval.scenarios import (
    CANARY,
    CONTINUITY,
    SMOKE,
    canary_fragments,
    expected_requests,
    persona_text,
    select_scenarios,
)
from tests.smoke.lib.sandbox_env import SmokeSandboxError

ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = ROOT / "tests" / "smoke" / "manual" / "memory_eval_probe.py"
probe = importlib.import_module("tests.smoke.manual.memory_eval_probe")
policy = importlib.import_module("tests.live_api_policy")
_CLI_ARGS = ["--provider", "deepseek", "--model", "deepseek-v4-flash", "--suite", "smoke",
             "--max-provider-requests", "3", "--max-tokens", "64", "--budget", "0.1"]
E5 = "intfloat/multilingual-e5-base"  # 10/15 實測時 .env 的嵌入模型


@pytest.fixture
def restore_product(monkeypatch: pytest.MonkeyPatch) -> None:
    """先快照，teardown 還原產品類別與模組屬性（probe 的安裝是整個程序的）。"""
    for adapter in probe._ADAPTERS:
        monkeypatch.setattr(adapter, "send_round", adapter.send_round)
        monkeypatch.setattr(adapter, "extract_usage", adapter.extract_usage)
    monkeypatch.setattr(react_core, "MAX_SEND_ATTEMPTS", react_core.MAX_SEND_ATTEMPTS)
    monkeypatch.setattr(proxy_service, "get_last_working_brain",
                        proxy_service.get_last_working_brain)
    monkeypatch.setattr(dialogue_summarizer, "complete_once", dialogue_summarizer.complete_once)
    monkeypatch.setattr(review_loop, "complete_once", review_loop.complete_once)
    monkeypatch.setattr(memory_vector, "SentenceTransformer", memory_vector.SentenceTransformer)
    # probe 用 apply_runtime_env 改 EMBEDDING_MODEL：先設成 e5（像 10/15 的 .env），teardown 還原
    monkeypatch.setenv("EMBEDDING_MODEL", E5)
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", None)
    monkeypatch.setattr(probe, "_PROVIDER_REQUEST_BUDGET", None)
    monkeypatch.setattr(probe, "_OUTPUT_CAP", None)
    monkeypatch.setattr(probe, "_LEDGER", None)
    monkeypatch.setattr(probe, "_AUXILIARY_CALLS", [])


def _install(max_requests: int = 5, wire_cap: int = 64, budget: float = 1.0) -> None:
    probe._install_bounds(max_requests, wire_cap, model="deepseek-v4-flash", budget=budget)


def _scope(
    max_tokens: int = 64, suite: str = SMOKE, ids: tuple[str, ...] = ("poison_home",),
    repeats: int = 1,
) -> Any:
    return probe._memory_eval_scope(
        provider="deepseek", model="deepseek-v4-flash", suite=suite,
        scenario_ids=list(ids), max_requests=3, max_tokens=max_tokens, budget=0.1,
        repeats=repeats,
    )


def _fake_original(sent: list[Any]) -> Any:
    async def _send_round(_adapter: Any, state: Any) -> Any:
        sent.append(state)
        yield "ok"

    return _send_round


async def _drain(adapter_cls: type, state: Any) -> list[Any]:
    return [item async for item in adapter_cls.send_round(object(), state)]


def test_import_leaves_product_defaults_untouched() -> None:
    assert react_core.MAX_SEND_ATTEMPTS == 3
    assert all(a.send_round is probe._ORIGINAL_SEND_ROUNDS[a] for a in probe._ADAPTERS)
    assert set(probe._ADAPTERS) == {ClaudeAdapter, OpenAIAdapter, ResponsesAdapter, GeminiAdapter}


def test_install_bounds_turns_off_retries_fallback_and_auxiliary_calls(
    restore_product: None,
) -> None:
    _install(2)

    assert react_core.MAX_SEND_ATTEMPTS == 1
    assert all(a.send_round is not probe._ORIGINAL_SEND_ROUNDS[a] for a in probe._ADAPTERS)
    assert proxy_service.get_last_working_brain(object()) is None
    assert dialogue_summarizer.complete_once is probe._refuse_auxiliary_call
    assert review_loop.complete_once is probe._refuse_auxiliary_call


@pytest.mark.parametrize(
    ("adapter_cls", "state"),
    [
        (ClaudeAdapter, SimpleNamespace(kwargs={"max_tokens": 64})),
        (OpenAIAdapter, SimpleNamespace(payload={"max_tokens": 64})),
        (ResponsesAdapter, SimpleNamespace(kwargs={"max_output_tokens": 64})),
        (GeminiAdapter, SimpleNamespace(payload={"generationConfig": {"maxOutputTokens": 64}})),
    ],
)
async def test_every_adapter_consumes_the_budget_before_sending(
    restore_product: None, monkeypatch: pytest.MonkeyPatch, adapter_cls: type, state: Any
) -> None:
    sent: list[Any] = []
    monkeypatch.setitem(probe._ORIGINAL_SEND_ROUNDS, adapter_cls, _fake_original(sent))
    _install(1)

    assert await _drain(adapter_cls, state) == ["ok"]
    with pytest.raises(policy.LiveApiRequestLimitExceeded):
        await _drain(adapter_cls, state)
    assert len(sent) == 1


@pytest.mark.parametrize(
    "state",
    [
        SimpleNamespace(kwargs={"max_tokens": 65}),
        SimpleNamespace(kwargs={"max_tokens": 8192}),
        SimpleNamespace(kwargs={}),
        SimpleNamespace(payload={"generationConfig": {}}),
        SimpleNamespace(payload={"max_tokens": "64"}),
    ],
)
async def test_output_cap_is_checked_before_budget_and_send(
    restore_product: None, monkeypatch: pytest.MonkeyPatch, state: Any
) -> None:
    sent: list[Any] = []
    monkeypatch.setitem(probe._ORIGINAL_SEND_ROUNDS, ClaudeAdapter, _fake_original(sent))
    _install(5)

    with pytest.raises(probe._OutputCapViolation):
        await _drain(ClaudeAdapter, state)
    assert sent == []
    assert probe._PROVIDER_REQUEST_BUDGET.used_requests == 0


@pytest.mark.parametrize(
    ("provider", "model", "requested", "wire"),
    [
        ("anthropic", "claude-haiku-5-5", 512, 8192),  # 會思考的 5.5：Touka 拉到 8,192
        ("anthropic", "claude-opus-5-5", 64, 8192),
        ("anthropic", "claude-haiku-4-5", 512, 512),
        ("deepseek", "deepseek-v4-flash", 512, 512),  # 其他廠商照請求值
        ("gemini", "gemini-2.5-flash", 256, 256),
    ],
)
def test_wire_cap_is_what_touka_will_send(provider: str, model: str, requested: int,
                                          wire: int) -> None:
    assert probe._wire_max_tokens(provider, model, requested) == wire


async def test_ceiling_holds_even_above_an_approved_wire_cap(
    restore_product: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[Any] = []
    monkeypatch.setitem(probe._ORIGINAL_SEND_ROUNDS, ClaudeAdapter, _fake_original(sent))
    _install(5, wire_cap=16_384)

    assert await _drain(ClaudeAdapter, SimpleNamespace(kwargs={"max_tokens": 8192})) == ["ok"]
    with pytest.raises(probe._OutputCapViolation):
        await _drain(ClaudeAdapter, SimpleNamespace(kwargs={"max_tokens": 8193}))
    assert len(sent) == 1
    assert probe._PROVIDER_REQUEST_BUDGET.used_requests == 1


async def test_run_stops_before_anything_when_touka_would_send_above_the_ceiling(
    restore_product: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installed: list[Any] = []
    monkeypatch.setattr(probe, "_install_bounds", lambda *a, **_k: installed.append(a))
    monkeypatch.setattr(probe, "_wire_max_tokens", lambda *_a: probe.OUTPUT_TOKEN_CEILING + 1)

    with pytest.raises(probe._OutputCeilingExceeded):
        await probe.run_memory_eval_probe(
            provider="deepseek", model="deepseek-v4-flash", suite=SMOKE,
            scenarios=select_scenarios(SMOKE, ["poison_home"]), max_requests=3, max_tokens=64,
            budget=0.1, approval=None, sandbox_root=tmp_path,
        )
    assert installed == []


async def test_worst_case_over_budget_is_refused_before_budget_and_send(
    restore_product: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[Any] = []
    monkeypatch.setitem(probe._ORIGINAL_SEND_ROUNDS, ClaudeAdapter, _fake_original(sent))
    _install(5, wire_cap=8192, budget=0.001)  # deepseek 8,192 輸出用滿就超過 0.001

    with pytest.raises(live_bounds.WorstCaseOverBudget):
        await _drain(ClaudeAdapter, SimpleNamespace(kwargs={"max_tokens": 8192}))
    assert sent == []
    assert probe._PROVIDER_REQUEST_BUDGET.used_requests == 0
    assert probe._LEDGER.refused


async def test_worst_case_counts_the_full_output_cap_and_the_tool_schema(
    restore_product: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(probe._ORIGINAL_SEND_ROUNDS, ClaudeAdapter, _fake_original([]))
    _install(5, wire_cap=8192)
    tools = [{"name": "memory_tools", "description": "查詢或儲存記憶" * 300}]
    state = SimpleNamespace(kwargs={"max_tokens": 8192, "tools": tools,
                                    "messages": [{"role": "user", "content": "嗨"}]})

    await _drain(ClaudeAdapter, state)

    (record,) = probe._LEDGER.records
    rate = pricing.get_pricing("deepseek-v4-flash")
    assert record.max_output_tokens == 8192
    assert record.input_tokens_estimate >= len(json.dumps(tools, ensure_ascii=False))
    assert record.worst_case_usd == pytest.approx(live_bounds.worst_case_usd(
        input_tokens=record.input_tokens_estimate, max_output_tokens=8192,
        input_price=live_bounds.worst_case_input_price(rate), output_price=rate.output,
    ))


@pytest.mark.parametrize("adapter_cls", [ClaudeAdapter, OpenAIAdapter, ResponsesAdapter,
                                         GeminiAdapter])
async def test_reported_usage_settles_the_request_at_its_actual_cost(
    restore_product: None, monkeypatch: pytest.MonkeyPatch, adapter_cls: type
) -> None:
    usage = {"prompt_tokens": 900, "cache_read_input_tokens": 100,
             "cache_creation_input_tokens": 50, "completion_tokens": 700}
    monkeypatch.setitem(probe._ORIGINAL_SEND_ROUNDS, adapter_cls, _fake_original([]))
    monkeypatch.setitem(probe._ORIGINAL_EXTRACT_USAGES, adapter_cls,
                        lambda _adapter, _state, _raw: (usage, 1050, 700))
    _install(5)
    state = SimpleNamespace(kwargs={"max_tokens": 64}, payload={"max_tokens": 64})
    await _drain(adapter_cls, state)

    assert adapter_cls.extract_usage(object(), state, object()) == (usage, 1050, 700)

    (record,) = probe._LEDGER.records
    actual = telemetry.usage_cost_usd(usage, model="deepseek-v4-flash")
    assert record.usage == usage
    assert record.cost_usd == pytest.approx(actual)
    assert probe._LEDGER.spent_usd == pytest.approx(actual)


async def test_auxiliary_model_calls_are_refused_without_sending(restore_product: None) -> None:
    _install(1)
    soul = AgentIdentity.for_role(agent_id="EvalSoulMio", role="Owner")

    first = await dialogue_summarizer.complete_once(soul, "summary", "m", "s", "u", 64, 5.0)
    second = await review_loop.complete_once(
        identity=soul, purpose="auto_review", model="m", system="s", user="u",
        max_tokens=64, timeout_s=5.0,
    )

    assert not first.ok and not second.ok
    assert first.error.error_class == "permission_denied"
    assert probe._AUXILIARY_CALLS == ["summary", "auto_review"]
    assert probe._PROVIDER_REQUEST_BUDGET.used_requests == 0


async def test_run_requires_the_issued_scope_before_installing_anything(
    restore_product: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installed: list[Any] = []
    monkeypatch.setattr(probe, "_install_bounds", lambda *a, **_k: installed.append(a))
    run = {
        "provider": "deepseek", "model": "deepseek-v4-flash", "suite": SMOKE,
        "scenarios": select_scenarios(SMOKE, ["poison_home"]), "max_requests": 3,
        "max_tokens": 64, "budget": 0.1, "sandbox_root": tmp_path,
    }

    with pytest.raises(policy.LiveApiApprovalRequired):
        await probe.run_memory_eval_probe(**run, approval=None)
    other = policy.issue_live_api_approval(approved=True, scope=_scope(max_tokens=63))
    with pytest.raises(policy.LiveApiApprovalScopeMismatch):
        await probe.run_memory_eval_probe(**run, approval=other)
    exact = policy.issue_live_api_approval(approved=True, scope=_scope())
    with pytest.raises(ValueError, match="route mismatch"):
        await probe.run_memory_eval_probe(**{**run, "provider": "grok"}, approval=exact)
    with pytest.raises(seeding.SeedOutsideSandbox):
        await probe.run_memory_eval_probe(**{**run, "sandbox_root": Path()}, approval=exact)
    assert installed == []
    assert config_utils.get_embedding_model_name() == E5  # 範圍不符時替身也沒設


class _FakeDriver:
    built: list[dict[str, Any]] = []
    readers: list[Any] = []
    repeats: list[int] = []

    def __init__(self, **kwargs: Any) -> None:
        _FakeDriver.built.append(kwargs)

    async def run_batch(self, _scenarios: Any, *, repeats: int = 1) -> Any:
        _FakeDriver.readers.append(resource_manager._ui_conversation_reader)
        _FakeDriver.repeats.append(repeats)
        return SimpleNamespace(to_dict=lambda: {"stopped_reason": ""})


@pytest.mark.parametrize(
    ("suite", "ids", "recording"),
    [(SMOKE, ("poison_home",), False), (CONTINUITY, ("continuity_last_visit",), True)],
)
async def test_only_the_continuity_batch_turns_the_activity_record_on(
    restore_product: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    suite: str, ids: tuple[str, ...], recording: bool,
) -> None:
    monkeypatch.setattr(resource_manager, "_ui_conversation_reader", None)  # 別的測試可能已裝上
    monkeypatch.setattr(probe, "_install_bounds", lambda *_a, **_k: None)
    monkeypatch.setattr(probe, "init_db_async", _no_init_db)
    monkeypatch.setattr(probe, "_seed_sandbox", lambda *_a: None)
    monkeypatch.setattr(probe, "build_base_context", dict)
    monkeypatch.setattr(probe.runner, "MemoryEvalDriver", _FakeDriver)
    monkeypatch.setattr(_FakeDriver, "built", [])
    monkeypatch.setattr(_FakeDriver, "readers", [])

    await probe.run_memory_eval_probe(
        provider="deepseek", model="deepseek-v4-flash", suite=suite,
        scenarios=select_scenarios(suite, list(ids)), max_requests=3, max_tokens=64,
        budget=0.1, sandbox_root=tmp_path,
        approval=policy.issue_live_api_approval(
            approved=True, scope=_scope(suite=suite, ids=ids)),
    )

    (kwargs,) = _FakeDriver.built
    assert kwargs["activity_record"] is recording
    prepare = kwargs["prepare_soul"]  # 每個情境另建靈魂：核准的型號、這次的沙盒
    assert prepare.func is probe._prepare_soul
    # 每次實測一組新口令（不是固定常數），人格與判定都用它
    assert kwargs["canary"] != CANARY
    assert len(canary_fragments(kwargs["canary"])) == 3
    assert prepare.keywords == {"sandbox_root": tmp_path, "model": "deepseek-v4-flash"}
    assert kwargs["read_facts"] is probe.runner.memory_active_facts
    assert callable(kwargs["archive_history"])
    # 查舊對話要讀 UI 對話：跑的時候裝上沙盒的 session store，跑完卸下
    assert _FakeDriver.readers == [session_service]
    assert resource_manager._ui_conversation_reader is None


async def _no_init_db() -> None:
    pass


class _BlewUp(RuntimeError):
    """測試用：故意讓實測流程中途失敗."""


class _FailingDriver(_FakeDriver):
    async def run_batch(self, _scenarios: Any, *, repeats: int = 1) -> Any:
        _FakeDriver.readers.append(resource_manager._ui_conversation_reader)
        raise _BlewUp


async def test_the_conversation_reader_is_unbound_when_the_batch_fails(
    restore_product: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(resource_manager, "_ui_conversation_reader", None)  # 別的測試可能已裝上
    monkeypatch.setattr(probe, "_install_bounds", lambda *_a, **_k: None)
    monkeypatch.setattr(probe, "init_db_async", _no_init_db)
    monkeypatch.setattr(probe, "_seed_sandbox", lambda *_a: None)
    monkeypatch.setattr(probe, "build_base_context", dict)
    monkeypatch.setattr(probe.runner, "MemoryEvalDriver", _FailingDriver)
    monkeypatch.setattr(_FakeDriver, "built", [])
    monkeypatch.setattr(_FakeDriver, "readers", [])

    with pytest.raises(_BlewUp):
        await probe.run_memory_eval_probe(
            provider="deepseek", model="deepseek-v4-flash", suite=SMOKE,
            scenarios=select_scenarios(SMOKE, ["poison_home"]), max_requests=3, max_tokens=64,
            budget=0.1, sandbox_root=tmp_path,
            approval=policy.issue_live_api_approval(approved=True, scope=_scope()),
        )

    assert _FakeDriver.readers == [session_service]
    assert resource_manager._ui_conversation_reader is None


async def test_a_failure_before_the_batch_never_binds_the_reader(
    restore_product: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _broken_seed(*_a: Any) -> None:
        raise _BlewUp

    monkeypatch.setattr(resource_manager, "_ui_conversation_reader", None)  # 別的測試可能已裝上
    monkeypatch.setattr(probe, "_install_bounds", lambda *_a, **_k: None)
    monkeypatch.setattr(probe, "init_db_async", _no_init_db)
    monkeypatch.setattr(probe, "_seed_sandbox", _broken_seed)

    with pytest.raises(_BlewUp):
        await probe.run_memory_eval_probe(
            provider="deepseek", model="deepseek-v4-flash", suite=SMOKE,
            scenarios=select_scenarios(SMOKE, ["poison_home"]), max_requests=3, max_tokens=64,
            budget=0.1, sandbox_root=tmp_path,
            approval=policy.issue_live_api_approval(approved=True, scope=_scope()),
        )

    assert resource_manager._ui_conversation_reader is None


async def test_run_pins_the_stub_after_the_scope_check_and_before_any_store(
    restore_product: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order: list[Any] = []
    real_pin = probe._pin_stub_embedding

    def _pin() -> dict[str, Any]:
        order.append("pin")
        return real_pin()

    async def _init_db() -> None:
        order.append(("init_db", config_utils.get_embedding_model_name()))

    monkeypatch.setattr(probe, "_install_bounds", lambda *_a, **_k: order.append("bounds"))
    monkeypatch.setattr(probe, "_pin_stub_embedding", _pin)
    monkeypatch.setattr(probe, "init_db_async", _init_db)
    monkeypatch.setattr(probe, "_seed_sandbox", lambda *_a: order.append("seed"))
    monkeypatch.setattr(probe, "build_base_context", dict)
    monkeypatch.setattr(probe.runner, "MemoryEvalDriver", _FakeDriver)

    report = await probe.run_memory_eval_probe(
        provider="deepseek", model="deepseek-v4-flash", suite=SMOKE,
        scenarios=select_scenarios(SMOKE, ["poison_home"]), max_requests=3, max_tokens=64,
        budget=0.1, sandbox_root=tmp_path,
        approval=policy.issue_live_api_approval(approved=True, scope=_scope()),
    )

    assert order == ["bounds", "pin", ("init_db", seeding.STUB_EMBEDDING_MODEL), "seed"]
    assert report["embedding_stub"] == probe._stub_facts()
    # 比較工具要的欄位：同一份評估程式、同一套件與題目才比
    assert report["eval_code_hash"] == stats.eval_code_hash(ROOT)
    assert (report["provider"], report["model"]) == ("deepseek", "deepseek-v4-flash")
    assert (report["suite"], report["scenarios"]) == (SMOKE, ["poison_home"])


# --- 替身嵌入：名稱與門檻固定，不跟 .env 走 -----------------------------------------


def test_stub_name_is_not_an_e5_model() -> None:
    assert "e5" not in seeding.STUB_EMBEDDING_MODEL.casefold()


def test_stub_uses_the_product_default_thresholds_whatever_the_env_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []
    for model in (E5, "all-MiniLM-L6-v2"):
        monkeypatch.setenv("EMBEDDING_MODEL", model)
        seen.append(probe._stub_facts())

    assert seen[0] == seen[1] == {
        "model": seeding.STUB_EMBEDDING_MODEL, "query_floor": 0.3, "dedup_threshold": 0.92,
        "note": probe.STUB_NOTE,
    }


def test_pin_overrides_the_env_model(restore_product: None) -> None:
    assert config_utils.get_embedding_model_name() == E5

    assert probe._pin_stub_embedding() == probe._stub_facts()
    assert config_utils.get_embedding_model_name() == seeding.STUB_EMBEDDING_MODEL


def test_pin_stops_when_the_vector_store_already_exists(
    restore_product: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", object())

    with pytest.raises(probe._StubEmbeddingNotPinned):
        probe._pin_stub_embedding()
    assert config_utils.get_embedding_model_name() == E5


def test_pin_stops_when_the_product_reads_another_name(
    restore_product: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_utils, "get_embedding_model_name", lambda: E5)

    with pytest.raises(probe._StubEmbeddingNotPinned):
        probe._pin_stub_embedding()


def test_scope_lists_every_bound() -> None:
    rendered = policy.format_live_api_scope(_scope())

    for item in ("operation=manual_memory_eval_probe", "provider=deepseek",
                 "model=deepseek-v4-flash", "scenarios=poison_home", "provider_requests<=3",
                 "max_tokens_requested=64", "wire_max_tokens_per_request=64",
                 "wire_max_tokens_ceiling=8192", "other providers send the requested value",
                 "pre_send_worst_case", "budget_usd=0.1000", "provider_retry=0",
                 "main_loop_send_attempts=1", "fallback=0", "auxiliary_model_calls=0",
                 f"embedding_stub={seeding.STUB_EMBEDDING_MODEL};query_floor=0.3;"
                 "dedup_threshold=0.92", probe.STUB_NOTE, "activity_record=off",
                 "repeats=1", "expected_provider_requests=1x1=1",
                 "write_scope=temporary sandbox only"):
        assert item in rendered


def test_scope_shows_the_rounds_and_the_requests_per_round_times_rounds() -> None:
    ids = ("poison_home", "memory_conflict")
    per_round = expected_requests(select_scenarios(SMOKE, list(ids)))
    rendered = policy.format_live_api_scope(_scope(ids=ids, repeats=3))

    assert per_round > 1
    assert "repeats=3" in rendered
    assert f"expected_provider_requests={per_round}x3={per_round * 3}" in rendered


_RUN: dict[str, Any] = {
    "provider": "deepseek", "model": "deepseek-v4-flash", "suite": SMOKE,
    "max_requests": 3, "max_tokens": 64, "budget": 0.1,
}


async def test_an_approval_for_one_round_does_not_cover_more_rounds(
    restore_product: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installed: list[Any] = []
    monkeypatch.setattr(probe, "_install_bounds", lambda *a, **_k: installed.append(a))
    once = policy.issue_live_api_approval(approved=True, scope=_scope())
    run = {**_RUN, "scenarios": select_scenarios(SMOKE, ["poison_home"]),
           "sandbox_root": tmp_path, "approval": once}

    with pytest.raises(policy.LiveApiApprovalScopeMismatch):
        await probe.run_memory_eval_probe(**run, repeats=5)
    with pytest.raises(argparse.ArgumentTypeError):
        await probe.run_memory_eval_probe(**run, repeats=probe.MAX_REPEATS + 1)
    assert installed == []


async def test_the_driver_runs_the_approved_number_of_rounds(
    restore_product: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(resource_manager, "_ui_conversation_reader", None)
    monkeypatch.setattr(probe, "_install_bounds", lambda *_a, **_k: None)
    monkeypatch.setattr(probe, "init_db_async", _no_init_db)
    monkeypatch.setattr(probe, "_seed_sandbox", lambda *_a: None)
    monkeypatch.setattr(probe, "build_base_context", dict)
    monkeypatch.setattr(probe.runner, "MemoryEvalDriver", _FakeDriver)
    monkeypatch.setattr(_FakeDriver, "repeats", [])

    await probe.run_memory_eval_probe(
        **_RUN, scenarios=select_scenarios(SMOKE, ["poison_home"]), sandbox_root=tmp_path,
        repeats=3, approval=policy.issue_live_api_approval(approved=True, scope=_scope(repeats=3)),
    )

    assert _FakeDriver.repeats == [3]


def test_continuity_scope_says_the_activity_record_is_on() -> None:
    rendered = policy.format_live_api_scope(
        _scope(suite=CONTINUITY, ids=("continuity_last_visit", "continuity_recent_done"))
    )

    assert "suite=continuity" in rendered
    assert "activity_record=on (seeded events written to the sandbox only)" in rendered
    assert "activity_record=off" not in rendered


def test_haiku_5_5_scope_shows_the_wire_cap_touka_will_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pricing.config_utils, "read_config_json", lambda _name: None)
    rendered = policy.format_live_api_scope(probe._memory_eval_scope(
        provider="anthropic", model="claude-haiku-5-5", suite=SMOKE,
        scenario_ids=["poison_home"], max_requests=40, max_tokens=512, budget=1.2,
    ))

    assert "max_tokens_requested=512" in rendered
    assert "wire_max_tokens_per_request=8192" in rendered
    assert "wire_max_tokens_ceiling=8192" in rendered
    # r55 價目表分檔後仍顯示貴檔（>100K）：核准範圍和改價前一字不差
    assert "worst_case_usd_per_mtok=input=0.625;output=2.5" in rendered


def test_haiku_5_5_pre_send_worst_case_still_uses_the_dear_tier(
    restore_product: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r55：Haiku 5.5 分兩檔後，送前最壞估價仍照貴檔 0.625（寫入快取）／2.50（寧高勿低）。"""
    monkeypatch.setattr(pricing.config_utils, "read_config_json", lambda _name: None)
    probe._install_bounds(5, 8192, model="claude-haiku-5-5", budget=1.0)

    assert probe._LEDGER.input_price == 0.625
    assert probe._LEDGER.output_price == 2.5


@pytest.mark.parametrize(
    ("parse", "bad", "good"),
    [
        (probe._bounded_int("max-tokens", probe.MAX_LIVE_TOKENS), ["0", "1025"], "1024"),
        (probe._bounded_int("max-provider-requests", probe.MAX_PROVIDER_REQUESTS),
         ["0", "201"], "200"),
        (probe._bounded_budget, ["0", "-1", "2.01"], "2"),
        (probe._bounded_int("repeats", probe.MAX_REPEATS), ["0", "6"], "5"),
    ],
)
def test_cli_bounds(parse: Any, bad: list[str], good: str) -> None:
    for value in bad:
        with pytest.raises(argparse.ArgumentTypeError):
            parse(value)
    assert parse(good)


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("deepseek-v4-flash", "deepseek"),
        ("gemini-2.5-flash", "gemini"),
        ("grok-4.20-0309-non-reasoning", "grok"),
        ("claude-haiku-4-5", "anthropic"),
        ("claude-haiku-5-5", "anthropic"),
    ],
)
def test_m1_models_route_to_the_disclosed_provider(model: str, provider: str) -> None:
    """M1 揭露草稿寫的四個型號，路由結果要和揭露的 provider 一致（不一致 CLI 會拒絕）。"""
    assert probe._provider_for_model(model) == provider


# --- 價目表：查不到拒跑、靠家族名稱估價要標出來（ADR-0284 第二部分） -----------------


_PRICES = {  # 合成價目表：之後有人把 gpt-5、grok 的完整名稱補進產品價目表，這些測試也不會跟著變
    "deepseek-v4-flash": pricing.Rate(0.27, 1.10), "claude-haiku-5-5": pricing.Rate(0.10, 0.50),
    "grok-4": pricing.Rate(3.0, 15.0), "claude-haiku-4": pricing.Rate(1.0, 5.0),
}


@pytest.fixture
def synthetic_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing.config_utils, "read_config_json", lambda _name: None)
    monkeypatch.setattr(pricing, "_BUILTIN_PRICING", _PRICES)


@pytest.mark.parametrize(
    ("model", "basis"),
    [("deepseek-v4-flash", "exact"), ("claude-haiku-5-5", "exact"),
     ("grok-4.20-0309-non-reasoning", "family_estimate"),
     ("claude-haiku-4-5", "family_estimate")],
)
def test_scope_says_whether_the_price_is_exact_or_a_family_estimate(
    synthetic_prices: None, model: str, basis: str
) -> None:
    rendered = policy.format_live_api_scope(probe._memory_eval_scope(
        provider=probe._provider_for_model(model), model=model, suite=SMOKE,
        scenario_ids=["poison_home"], max_requests=3, max_tokens=64, budget=0.1,
    ))

    assert f"pricing={basis}" in rendered


def test_a_price_override_is_exact_only_under_the_full_model_name(
    synthetic_prices: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    model, entry = "grok-4.20-0309-non-reasoning", {"input": 3.0, "output": 15.0}

    monkeypatch.setattr(pricing.config_utils, "read_config_json",
                        lambda _name: {"pricing": {model: entry}})
    assert probe._price_basis(model) == "exact"
    monkeypatch.setattr(pricing.config_utils, "read_config_json",
                        lambda _name: {"pricing": {"grok-4.20": entry}})
    assert probe._price_basis(model) == "family_estimate"


def test_a_model_without_a_price_is_refused_before_any_approval(
    synthetic_prices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")  # 避免 main 改包 pytest 的 stdout
    assert probe._provider_for_model("gpt-5") == "openai"  # 路由得到，價目表只有 gpt-4 這些

    with pytest.raises(probe._UnpricedModel):
        probe._memory_eval_scope(
            provider="openai", model="gpt-5", suite=SMOKE, scenario_ids=["poison_home"],
            max_requests=3, max_tokens=64, budget=0.1,
        )
    with pytest.raises(SystemExit):  # 還沒到問核准就停下（不是 LiveApiApprovalRequired）
        probe.main(["--provider", "openai", "--model", "gpt-5", "--suite", "smoke",
                    "--max-provider-requests", "3", "--budget", "0.1"])


class _ScopeSeen(Exception):
    """測試用：main 把核准範圍交給簽發之前就停下（不傳核准旗標）."""


def test_the_cli_runs_one_round_unless_told_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def _stop(*, approved: bool, scope: Any) -> None:
        seen.append((approved, scope))
        raise _ScopeSeen

    monkeypatch.setattr(sys, "platform", "linux")  # 避免 main 改包 pytest 的 stdout
    monkeypatch.setattr(probe, "issue_live_api_approval", _stop)
    for extra in ([], ["--repeats", "3"]):
        with pytest.raises(_ScopeSeen):
            probe.main([*_CLI_ARGS, *extra])

    assert [approved for approved, _scope in seen] == [False, False]
    assert [[item for item in scope.limits if item.startswith("repeats=")]
            for _approved, scope in seen] == [["repeats=1"], ["repeats=3"]]


def test_cli_refuses_the_same_scenario_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit):
        probe.main([*_CLI_ARGS, "--scenarios", "poison_home,poison_home"])


def test_cli_still_requires_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")  # 避免 main 改包 pytest 的 stdout
    with pytest.raises(policy.LiveApiApprovalRequired):
        probe.main(_CLI_ARGS)
    with pytest.raises(SystemExit):
        probe.main([*_CLI_ARGS, "--scenarios", "no_such_scenario"])


def test_each_prepared_soul_is_an_owner_with_the_approved_model(
    restore_product: None, global_test_sandbox: str
) -> None:
    sandbox = Path(global_test_sandbox)
    try:
        probe._seed_sandbox(sandbox)
        assert memory_vector.SentenceTransformer is seeding.DeterministicEncoder
        for number in (1, 2):
            soul_id = seeding.soul_id_for(number)
            persona = persona_text(f"BCDFG-HJKLM-NPQR{number}")
            probe._prepare_soul(soul_id, persona, sandbox_root=sandbox, model="deepseek-v4-flash")
            assert get_active_brain(seeding.soul_identity(soul_id)) == "deepseek-v4-flash"
            soul_md = Path(config_utils.get_app_data_dir()) / "soul" / soul_id / "soul.md"
            assert soul_md.read_text(encoding="utf-8") == persona
    finally:
        permission_service.identity_path.unlink(missing_ok=True)
        permission_service.reload_all()


def test_seed_sandbox_refuses_targets_outside_the_sandbox(
    restore_product: None, tmp_path: Path
) -> None:
    with pytest.raises(seeding.SeedOutsideSandbox):
        probe._seed_sandbox(tmp_path / "not-the-sandbox")
    with pytest.raises(seeding.SeedOutsideSandbox):
        probe._prepare_soul(seeding.soul_id_for(1), persona_text(CANARY),
                            sandbox_root=tmp_path / "not-the-sandbox", model="deepseek-v4-flash")


def test_cli_gate_precedes_every_provider_capable_import() -> None:
    """人工閘之前只能執行標準函式庫與政策模組的 import（dotenv、產品程式、評估框架都在閘之後）。"""
    tree = ast.parse(PROBE_PATH.read_text(encoding="utf-8"))
    gate = min(
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "preflight_live_api_cli"
    )
    executed_before_gate = {
        node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno < gate
    }
    assert executed_before_gate <= {
        "__future__", "argparse", "asyncio", "io", "json", "sys", "pathlib", "typing",
        "tests.live_api_policy",
    }
    gate_block = next(
        node for node in tree.body
        if isinstance(node, ast.If) and node.lineno <= gate <= node.end_lineno
    )
    assert ast.unparse(gate_block.test) == "__name__ == '__main__'"


# --- 結束清理（Windows：先放開握住沙盒的 handle 才刪得掉） --------------------------


@pytest.fixture
def os_temp_sandbox() -> Any:
    root = Path(tempfile.mkdtemp(prefix="touka_e1b_probe_release_"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def test_release_closes_logs_and_stores_before_deleting_the_sandbox(
    monkeypatch: pytest.MonkeyPatch, os_temp_sandbox: Path
) -> None:
    root = os_temp_sandbox
    (root / "logs").mkdir()
    handler = logging.FileHandler(root / "logs" / "proxy_payload.log", encoding="utf-8")
    holder = logging.getLogger("tests.memory_eval.release")
    holder.addHandler(handler)
    holder.warning("synthetic line")  # Windows：沒關的 handler 會讓 rmtree 丟 WinError 32
    seen: list[tuple[str, bool]] = []
    real_close_logs = probe.close_sandbox_log_handlers

    def _close_logs(directory: str) -> int:
        seen.append(("logs", root.exists()))
        return real_close_logs(directory)

    monkeypatch.setattr(probe, "close_sandbox_log_handlers", _close_logs)
    monkeypatch.setattr(probe, "_drain_policy_audit_writes",
                        lambda: seen.append(("audit", root.exists())))
    monkeypatch.setattr(FactualStore, "close_all_connections",
                        lambda: seen.append(("sqlite", root.exists())))
    monkeypatch.setattr(memory_vector.VectorStore, "close_all_clients",
                        lambda: seen.append(("qdrant", root.exists())))
    try:
        assert probe._release_sandbox(root) is None
        assert handler not in holder.handlers
    finally:
        holder.removeHandler(handler)
        handler.close()

    assert seen == [("audit", True), ("logs", True), ("sqlite", True), ("qdrant", True)]
    assert not root.exists()


def test_policy_audit_drain_waits_for_queued_writes() -> None:
    done: list[str] = []
    PolicyAuditService._executor.submit(lambda: (time.sleep(0.05), done.append("write")))

    probe._drain_policy_audit_writes()

    assert done == ["write"]


def test_release_refuses_anything_outside_the_os_temp_dir() -> None:
    with pytest.raises(SmokeSandboxError):
        probe._release_sandbox(ROOT / "no-such-probe-sandbox")


class _Stdout:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self._events = events

    def write(self, text: str) -> int:
        self._events.append(("stdout", text))
        return len(text)


def _fake_report(stopped: str = "") -> dict[str, Any]:
    return {"stopped_reason": stopped, "requests": [{"output_tokens_incl_thinking": 900}]}


def test_report_with_usage_is_printed_before_the_sandbox_is_released(
    monkeypatch: pytest.MonkeyPatch, os_temp_sandbox: Path
) -> None:
    events: list[tuple[str, Any]] = []
    monkeypatch.setattr(sys, "stdout", _Stdout(events))
    monkeypatch.setattr(probe, "_release_sandbox", lambda root: events.append(("release", root)))

    def _run() -> dict[str, Any]:
        events.append(("run", None))
        return _fake_report()

    assert probe._report_then_release(_run, str(os_temp_sandbox)) == 0
    assert [kind for kind, _ in events] == ["run", "stdout", "release"]
    assert "output_tokens_incl_thinking" in events[1][1]
    assert events[2][1] == os_temp_sandbox


def test_cleanup_failure_is_reported_and_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], os_temp_sandbox: Path
) -> None:
    monkeypatch.setattr(probe, "_release_sandbox", lambda root: root)

    assert probe._report_then_release(_fake_report, str(os_temp_sandbox)) == 1
    assert f"sandbox_cleanup_failed={os_temp_sandbox}" in capsys.readouterr().err


def test_sandbox_is_released_even_when_the_run_fails(
    monkeypatch: pytest.MonkeyPatch, os_temp_sandbox: Path
) -> None:
    released: list[Path] = []
    monkeypatch.setattr(probe, "_release_sandbox", released.append)

    class _RunFailed(RuntimeError):
        pass

    def _boom() -> dict[str, Any]:
        raise _RunFailed

    with pytest.raises(_RunFailed):
        probe._report_then_release(_boom, str(os_temp_sandbox))
    assert released == [os_temp_sandbox]


def test_without_an_owned_sandbox_nothing_is_released(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(probe, "_release_sandbox", lambda _root: pytest.fail("released"))

    assert probe._report_then_release(lambda: _fake_report("stopped"), "") == 1
    assert '"stopped_reason": "stopped"' in capsys.readouterr().out
