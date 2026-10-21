"""延遲實測工具的硬界（T12；全部離線：不傳核准旗標、不外連，送出點換成假的）。"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.services import completion

probe = importlib.import_module("tests.smoke.manual.thinking_latency_probe")
policy = importlib.import_module("tests.live_api_policy")
shared = importlib.import_module("tests.smoke.manual.memory_eval_probe")
PROBE_PATH = Path(probe.__file__)
_MODELS = ["claude-haiku-5-5", "claude-opus-5-5"]
_BOUNDS = {"models": _MODELS, "profiles": ["minimal", "default"], "repeats": 1,
           "max_requests": 4, "max_tokens": 256, "budget": 1.0,
           "env_file": probe.DEFAULT_ENV_FILE}


@pytest.fixture
def restore(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """還原 probe 對產品類別的安裝；原送出點換成記錄器（不外連）。"""
    sent: list[Any] = []

    async def _fake_send(_adapter: Any, state: Any) -> Any:
        sent.append(state)
        yield SimpleNamespace(content=[SimpleNamespace(type="text", text="one sentence")],
                              stop_reason="end_turn", usage=SimpleNamespace(model_dump=lambda: {
                                  "input_tokens": 2000, "output_tokens": 40}))

    monkeypatch.setattr(ClaudeAdapter, "send_round", ClaudeAdapter.send_round)
    monkeypatch.setattr(ClaudeAdapter, "extract_usage", ClaudeAdapter.extract_usage)
    monkeypatch.setattr(probe, "_ORIGINAL_SEND_ROUND", _fake_send)
    for name, value in (("_BUDGET", None), ("_LEDGER", None), ("_CURRENT", {})):
        monkeypatch.setattr(probe, name, value)
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-anthropic", raising=False)
    monkeypatch.setattr(completion, "accumulate_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(completion, "log_usage_audit", lambda *a, **k: None)
    return sent


def _approval(**overrides: Any) -> Any:
    bounds = {**_BOUNDS, **overrides}
    return policy.issue_live_api_approval(approved=True, scope=probe.latency_scope(**bounds))


def test_import_leaves_the_product_untouched() -> None:
    assert ClaudeAdapter.send_round is probe._ORIGINAL_SEND_ROUND
    assert ClaudeAdapter.extract_usage is probe._ORIGINAL_EXTRACT_USAGE


def test_gate_precedes_dotenv_sandbox_and_product_imports() -> None:
    tree = ast.parse(PROBE_PATH.read_text(encoding="utf-8"))
    gate = min(node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
               and ast.unparse(node.func) == "preflight_live_api_cli")
    before = {node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
              for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
              and node.lineno < gate}
    assert before <= {"__future__", "argparse", "asyncio", "io", "statistics", "sys", "pathlib",
                      "typing", "tests.live_api_policy"}
    source = PROBE_PATH.read_text(encoding="utf-8")
    assert source.index("enable_smoke_sandbox(") < source.index("load_dotenv(env_file_of")
    assert source.index("load_dotenv(env_file_of") < source.index("from src.")
    assert not PROBE_PATH.name.startswith(("test_", "smoke_"))


def test_cli_requires_approval_and_refuses_bad_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")  # main 不改包 pytest 的 stdout
    args = ["--models", "claude-haiku-5-5", "--profiles", "minimal", "--repeats", "1",
            "--max-provider-requests", "1", "--max-tokens", "64", "--budget", "0.1"]
    with pytest.raises(policy.LiveApiApprovalRequired):
        probe.main(args)
    for bad in (["--models", "claude-haiku-4-5"], ["--profiles", "fast"], ["--repeats", "6"],
                ["--max-tokens", "1025"], ["--budget", "3.5"], ["--max-provider-requests", "31"],
                ["--repeats", "2"]):  # 最後一個：1×1×2 超過請求上限 1
        with pytest.raises(SystemExit):
            probe.main([*args, *bad])


@pytest.mark.parametrize("overrides", [
    {"models": ["claude-opus-5"]}, {"models": ["gpt-6-sol"]}, {"repeats": 6},
    {"max_requests": 3}, {"max_tokens": 0}, {"profiles": ["minimal", "fast"]},
    {"budget": 3.01}, {"budget": 0}, {"models": []},
])
def test_check_request_refuses_out_of_scope_batches(overrides: dict[str, Any]) -> None:
    with pytest.raises(probe.ProbeRefused):
        probe.check_request(**{**_BOUNDS, **overrides})


def test_refuses_when_the_purpose_table_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(completion, "thinking_profile_for", lambda _p: "minimal")
    with pytest.raises(probe.ProbeRefused, match="purpose table"):
        probe.check_request(**_BOUNDS)


def test_scope_discloses_the_wire_caps_and_prices() -> None:
    limits = "\n".join(probe.latency_scope(**_BOUNDS).limits)
    assert ("wire_max_tokens=claude-haiku-5-5/minimal=256;claude-haiku-5-5/default=8192;"
            "claude-opus-5-5/minimal=8192;claude-opus-5-5/default=8192") in limits
    for item in ("expected_requests=4", "provider_retry=0", "fallback=0", "budget_usd=1.0000",
                 "profiles=minimal->summarizer,default->critic",
                 # 每個型號自己的最壞單價；Haiku 5.5 取 >10 萬 token 的貴檔
                 "claude-haiku-5-5=in:0.625/out:2.5;claude-opus-5-5=in:5/out:20"):
        assert item in limits


async def test_batch_sends_through_complete_once_and_reports_tokens_and_time(
        restore: list[Any]) -> None:
    report = await probe.run_latency_probe(approval=_approval(), **_BOUNDS)
    assert report["stopped_reason"] == "" and report["provider_requests_used"] == 4
    assert [(r["model"], r["profile"], r["sent"]) for r in report["requests"]] == [
        ("claude-haiku-5-5", "minimal", {"max_tokens": 256, "thinking": "disabled",
                                         "effort": "low"}),
        ("claude-haiku-5-5", "default", {"max_tokens": 8192, "thinking": None, "effort": None}),
        ("claude-opus-5-5", "minimal", {"max_tokens": 8192, "thinking": None, "effort": "low"}),
        ("claude-opus-5-5", "default", {"max_tokens": 8192, "thinking": None, "effort": None}),
    ]
    row = report["requests"][0]
    assert (row["input_tokens"], row["output_tokens_incl_thinking"], row["text_chars"]) == (
        2000, 40, len("one sentence"))
    assert "one sentence" not in str(report)  # 不印回應原文
    settled = report["ledger"]["requests"]  # 送前預留最壞，回來照實際用量結清
    assert [r["output_tokens_incl_thinking"] for r in settled] == [40] * 4
    assert all(0 < r["cost_usd"] < r["worst_case_usd"] for r in settled)
    assert {g["profile"] for g in report["groups"]} == {"minimal", "default"}


async def test_worst_case_over_budget_sends_nothing_and_spends_no_request(
        restore: list[Any]) -> None:
    # 0.002：便宜檔估（約 0.001）會放行、貴檔估（約 0.005）不放行——每個請求要用貴檔估
    report = await probe.run_latency_probe(approval=_approval(budget=0.002), **{
        **_BOUNDS, "budget": 0.002})
    assert restore == [] and report["provider_requests_used"] == 0
    assert [r["ok"] for r in report["requests"]] == [False]  # 第一個就停
    assert "over budget" in report["ledger"]["send_refused"] and report["stopped_reason"]
    assert report["requests"][0]["cost_usd"] == 0.0


async def test_wire_cap_above_approval_is_refused_before_budget(
        restore: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "wire_max_tokens", lambda *_a: 128)  # 核准值比實送值小
    report = await probe.run_latency_probe(approval=_approval(), **_BOUNDS)
    assert restore == [] and report["provider_requests_used"] == 0
    assert report["ledger"]["send_refused"] == "" and report["stopped_reason"]


async def test_request_budget_is_consumed_last_and_unwinds_the_ledger(
        restore: list[Any]) -> None:
    probe.install_bounds(1, 1.0)
    probe._CURRENT.update(model="claude-haiku-5-5", wire_cap=256)
    state = SimpleNamespace(kwargs={"max_tokens": 256, "messages": []})
    assert len([item async for item in ClaudeAdapter.send_round(object(), state)]) == 1
    with pytest.raises(policy.LiveApiRequestLimitExceeded):
        _ = [item async for item in ClaudeAdapter.send_round(object(), state)]
    assert len(restore) == 1 and len(probe._LEDGER.records) == 1


async def test_run_requires_the_issued_scope(restore: list[Any]) -> None:
    with pytest.raises(policy.LiveApiApprovalScopeMismatch):
        await probe.run_latency_probe(approval=_approval(repeats=1, max_tokens=64), **_BOUNDS)
    assert ClaudeAdapter.send_round is not probe._bounded_send_round  # 沒裝任何東西


def test_report_and_sandbox_release_reuse_the_memory_probe() -> None:
    assert probe.shared is shared
    assert shared._report_then_release(lambda: {"stopped_reason": "x"}, "") == 1
