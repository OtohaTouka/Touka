"""整次 run 的金額分檔（r55 第 6 點，T8）。

- 每輪照自己的提示長度選檔（未快取輸入＋快取讀＋快取寫），再相加；不拿多輪加總去選檔
  （Haiku 5.5 多輪加總超過 10 萬會整筆算成貴檔，高估 5 倍）。
- 沒分檔的型號，整次金額＝改前的 ``usage_cost_usd(totals)``（特徵化 golden 一個數字都不變）。
- 畫面 token_usage 事件、稽核 CSV 與 token_usage.json（每輪入帳）三處金額一致。
- ``log_usage_audit(cost_usd=...)`` 寫傳入值；沒傳照舊自己算。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.handlers.openai_handler import handle_openai_request
from src.handlers.react_core import _RunUsage
from src.models.identity import AgentIdentity
from src.services import telemetry
from src.services.providers import pricing
from src.services.providers.pricing import Rate
from src.services.resource_manager import resource_manager
from src.services.rhythm_service import rhythm_service

from tests.helpers.stream_transport import FakeUpstream, stream_reply
from tests.unit.test_per_round_accounting import (
    _audit_rows,
    _ctx,
    _executor,
    _openai_round,
    _token_usage,
    _usage_events,
)

_HAIKU = "claude-haiku-5-5"
_UNTIERED = ["claude-sonnet-4-6", "claude-opus-5-5", "deepseek-chat", "gemini-2.5-flash",
             "gpt-4o", None]


@pytest.fixture(autouse=True)
def _builtin_pricing(monkeypatch):
    """只讓 model_pricing.json 讀不到（用內建表）；其他設定檔照常讀。"""
    real_read = pricing.config_utils.read_config_json
    monkeypatch.setattr(pricing.config_utils, "read_config_json",
                        lambda name: None if name == "model_pricing.json" else real_read(name))


def _usage(prompt: int, completion: int = 0, cache_read: int = 0, cache_write: int = 0) -> dict:
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write}


def _run(model: str | None, *rounds: dict) -> _RunUsage:
    run = _RunUsage(model=model)
    for usage in rounds:
        run.add(usage, usage["prompt_tokens"])
    return run


# ── 判斷與算式 ─────────────────────────────────────────────────────────────────


def test_is_long_prompt_counts_cache_reads_and_writes():
    assert telemetry.is_long_prompt(_usage(100_000), model=_HAIKU) is False
    assert telemetry.is_long_prompt(_usage(60_000, cache_read=50_000), model=_HAIKU) is True
    assert telemetry.is_long_prompt(_usage(99_999, cache_write=2), model=_HAIKU) is True
    assert telemetry.is_long_prompt(_usage(10, completion=500_000), model=_HAIKU) is False
    assert telemetry.is_long_prompt(_usage(5_000_000), model="claude-opus-5-5") is False


def test_two_short_haiku_rounds_stay_on_the_short_tier():
    """兩輪各 6 萬：每輪都是便宜檔，整次 0.012；拿加總 12 萬去選檔會變 0.06。"""
    run = _run(_HAIKU, _usage(60_000), _usage(60_000))
    assert run.long_totals == {}
    assert run.cost_usd == pytest.approx(0.012, abs=1e-9)
    assert telemetry.usage_cost_usd(run.totals, model=_HAIKU) == pytest.approx(0.06, abs=1e-9)


def test_long_and_short_haiku_rounds_are_priced_separately():
    """15 萬（貴檔：7.5 萬分＋輸出 0.25 分）＋1 萬（便宜檔：0.1 分＋輸出 0.05 分）。"""
    run = _run(_HAIKU, _usage(150_000, completion=1_000), _usage(10_000, completion=1_000))
    assert run.long_totals == _usage(150_000, completion=1_000)
    assert run.cost_usd == pytest.approx(0.0775 + 0.0015, abs=1e-9)


@pytest.mark.parametrize("model", _UNTIERED)
def test_untiered_models_keep_the_old_run_total(model):
    """沒分檔：long_totals 永遠空，整次金額和改前的 usage_cost_usd(totals) 完全相同。"""
    run = _run(model, _usage(1_234, 567, 890, 12), _usage(150_000, 3_333, 70_000, 1),
               _usage(7, 0, 0, 0))
    assert run.long_totals == {}
    assert run.cost_usd == telemetry.usage_cost_usd(run.totals, model=model)
    assert telemetry.run_cost_usd(run.totals, {}, model=model) == telemetry.usage_cost_usd(
        run.totals, model=model)


def test_log_usage_audit_writes_the_given_cost(monkeypatch):
    rows: list[list] = []
    monkeypatch.setattr(resource_manager, "append_csv_row",
                        lambda _path, row, headers=None: rows.append(row))
    identity = AgentIdentity.for_role(agent_id="TierAudit", role="Owner")
    totals = _usage(120_000)

    telemetry.log_usage_audit(identity, "req-1", "Anthropic", {}, totals, model=_HAIKU,
                              cost_usd=0.012)
    telemetry.log_usage_audit(identity, "req-2", "Anthropic", {}, totals, model=_HAIKU)

    assert rows[0][11] == "0.012000"
    assert rows[1][11] == "0.060000"  # 沒傳：照舊用 usage_cost_usd(totals)


# ── 端到端：主迴圈兩輪，各自便宜檔 ────────────────────────────────────────────────


async def test_run_total_on_screen_and_in_audit_matches_per_round_billing(monkeypatch):
    """假型號門檻 1,000：兩輪各 600／700（各自短檔），加總 1,300 不得整筆算長檔。

    每輪入帳（token_usage.json）本來就逐請求選檔；畫面事件與稽核 CSV 的整次金額要和它一致。
    """
    monkeypatch.setitem(pricing._BUILTIN_PRICING, "acct-tiered",
                        Rate(1.0, 2.0, long_prompt=(1_000, Rate(10.0, 20.0))))
    ctx = _ctx("TierRun")
    request = MagicMock()
    request.model = "acct-tiered"
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "acct-tiered", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    request.messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)]
    upstream = FakeUpstream([
        stream_reply(_openai_round(600, 0, 10, tool="vault_tools"), "semantic"),
        stream_reply(_openai_round(700, 0, 20), "semantic"),
    ])
    rhythm_service.clear_session(ctx["session_id"])
    rhythm_service.clear_interrupt(ctx["session_id"])
    try:
        with patch("src.handlers.react_core.ToolExecutor") as executor_cls:
            upstream.install(monkeypatch)
            executor_cls.return_value.execute_to_queue = AsyncMock(side_effect=_executor([]))
            chunks = [c async for c in handle_openai_request(request, ctx)]
    finally:
        rhythm_service.clear_session(ctx["session_id"])

    expected = 0.00136  # (600×1＋10×2)＋(700×1＋20×2)，每百萬；整筆長檔會是 0.0136
    assert _token_usage(ctx["identity"])["OpenAI"]["total_cost_usd"] == expected
    (event,) = _usage_events(chunks)
    assert event["cost"] == expected
    audit = _audit_rows(ctx["identity"])
    assert audit[1][11] == "0.001360"
