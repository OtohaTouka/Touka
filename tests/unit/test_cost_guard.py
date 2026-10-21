"""送前最壞花費（r55 第 5 點，T6 離線部分）。

- ``cost_guard`` 的純函式和人工實測的 ``tests/memory_eval/live_bounds.py`` 同輸入同結果
  （產品不 import tests，所以兩邊各一份，這裡釘住不漂移）。
- ``AutonomyService.would_exceed``：上限沒開或沒設上限＝False；已花＋最壞 > 上限＝True；
  剛好等於＝False（送了也不會超過）。
"""

from __future__ import annotations

import inspect

import pytest
from src.models.identity import AgentIdentity
from src.services import completion, cost_guard
from src.services.autonomy import autonomy_service
from src.services.models.manager import ModelManager
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution
from src.services.providers import pricing
from src.services.providers.pricing import Rate

from tests.memory_eval import live_bounds
from tests.unit import test_complete_once_contract as contract

_IDENTITY = AgentIdentity.for_role(agent_id="CostGuardTester", role="Owner")
_PAYLOADS = [
    {},
    {"kwargs": {"model": "claude-opus-5-5", "max_tokens": 8192,
                "messages": [{"role": "user", "content": "請判斷這個動作"}]}},
    {"payload": {"contents": [{"parts": [{"text": "x" * 5000}]}]}, "kwargs": None},
    ["中文每字至少一個 token", 3, 4.5, None, True],
    {"obj": object()},  # 不能序列化的值照 str() 估（default=str）
]
_RATES = [
    Rate(3.0, 15.0),  # 沒填快取價：照 input
    Rate(4.0, 20.0, cache_read=0.20, cache_write=5.0),  # 寫入快取較貴
    Rate(1.0, 2.0, cache_read=0.1, cache_write=0.5),  # 寫入快取較便宜
]


def _no_pricing_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing.config_utils, "read_config_json", lambda _name: None)


def _cap(monkeypatch: pytest.MonkeyPatch, *, enabled: bool, limit: float | None,
         spent: float) -> None:
    monkeypatch.setitem(autonomy_service.config, "cap",
                        {"enabled": enabled, "max_total_cost_usd": limit})
    monkeypatch.setattr(autonomy_service, "get_total_cost_usd", lambda _identity: spent)


# ── 純函式 ──────────────────────────────────────────────────────────────────────


def test_estimate_counts_every_json_character_plus_vendor_overhead():
    payload = {"a": "中文"}
    assert cost_guard.VENDOR_OVERHEAD_TOKENS == 1_024
    assert cost_guard.estimate_input_tokens(payload) == len('{"a": "中文"}') + 1_024


@pytest.mark.parametrize(("rate", "expected"), [
    (Rate(3.0, 15.0), 3.0),
    (Rate(4.0, 20.0, cache_read=0.20, cache_write=5.0), 5.0),
    (Rate(1.0, 2.0, cache_write=0.5), 1.0),
])
def test_worst_input_price_is_the_dearer_of_input_and_cache_write(rate, expected):
    assert cost_guard.worst_case_input_price(rate) == expected


def test_worst_case_usd_fills_the_whole_output_cap():
    worst = cost_guard.worst_case_usd(input_tokens=2_000, max_output_tokens=8_192,
                                      input_price=5.0, output_price=20.0)
    assert worst == pytest.approx((2_000 * 5.0 + 8_192 * 20.0) / 1_000_000)


def test_worst_case_call_prices_the_payload_at_the_model_rate(monkeypatch):
    _no_pricing_override(monkeypatch)
    payload = {"kwargs": {"model": "claude-opus-5-5", "max_tokens": 8192}}
    tokens = cost_guard.estimate_input_tokens(payload)
    # Opus 5.5：輸入 $4、寫入快取 $5 取較貴的 5；輸出 $20
    expected = (tokens * 5.0 + 8_192 * 20.0) / 1_000_000
    assert cost_guard.worst_case_call_usd("claude-opus-5-5", payload, 8_192) == pytest.approx(
        expected)


def test_worst_case_call_unknown_model_uses_the_nonzero_fallback(monkeypatch):
    """未知型號照保守 fallback（不可為 0，否則送前估算永遠放行）。"""
    _no_pricing_override(monkeypatch)
    assert cost_guard.worst_case_call_usd(None, {}, 1) > 0
    assert cost_guard.worst_case_call_usd("totally-unknown-xyz", {}, 1) > 0


# ── 和人工實測工具的估法一致（parity）────────────────────────────────────────────


def test_parity_overhead_constant():
    assert cost_guard.VENDOR_OVERHEAD_TOKENS == live_bounds.VENDOR_OVERHEAD_TOKENS


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_parity_estimate_input_tokens(payload):
    assert cost_guard.estimate_input_tokens(payload) == live_bounds.estimate_input_tokens(payload)


@pytest.mark.parametrize("rate", _RATES)
def test_parity_worst_case_input_price(rate):
    assert cost_guard.worst_case_input_price(rate) == live_bounds.worst_case_input_price(rate)


@pytest.mark.parametrize(("tokens", "cap", "price_in", "price_out"), [
    (0, 0, 0.0, 0.0), (1_024, 1, 3.0, 15.0), (123_456, 8_192, 0.625, 2.5),
])
def test_parity_worst_case_usd(tokens, cap, price_in, price_out):
    kwargs = {"input_tokens": tokens, "max_output_tokens": cap,
              "input_price": price_in, "output_price": price_out}
    assert cost_guard.worst_case_usd(**kwargs) == live_bounds.worst_case_usd(**kwargs)


# ── AutonomyService.would_exceed ───────────────────────────────────────────────


def test_would_exceed_false_when_cap_disabled(monkeypatch):
    _cap(monkeypatch, enabled=False, limit=1.0, spent=999.0)
    assert autonomy_service.would_exceed(_IDENTITY, 999.0) is False


def test_would_exceed_false_without_a_limit(monkeypatch):
    _cap(monkeypatch, enabled=True, limit=None, spent=999.0)
    assert autonomy_service.would_exceed(_IDENTITY, 999.0) is False


def test_would_exceed_false_when_landing_exactly_on_the_limit(monkeypatch):
    _cap(monkeypatch, enabled=True, limit=1.0, spent=0.75)
    assert autonomy_service.would_exceed(_IDENTITY, 0.25) is False


def test_would_exceed_true_when_spent_plus_worst_passes_the_limit(monkeypatch):
    _cap(monkeypatch, enabled=True, limit=1.0, spent=0.95)
    assert autonomy_service.would_exceed(_IDENTITY, 0.17) is True
    assert autonomy_service.would_exceed(_IDENTITY, 0.04) is False


def test_would_exceed_reads_the_spend_of_that_identity(monkeypatch):
    seen: list[AgentIdentity] = []
    monkeypatch.setitem(autonomy_service.config, "cap",
                        {"enabled": True, "max_total_cost_usd": 1.0})
    monkeypatch.setattr(autonomy_service, "get_total_cost_usd",
                        lambda identity: seen.append(identity) or 0.0)
    assert autonomy_service.would_exceed(_IDENTITY, 0.5) is False
    assert seen == [_IDENTITY]


def test_is_over_budget_unchanged_after_sharing_the_cap_reader(monkeypatch):
    """is_over_budget 改用共用的上限讀取後行為不變（>= 邊界、沒開、沒設上限）。"""
    _cap(monkeypatch, enabled=True, limit=1.0, spent=1.0)
    assert autonomy_service.is_over_budget(_IDENTITY) is True
    _cap(monkeypatch, enabled=False, limit=1.0, spent=5.0)
    assert autonomy_service.is_over_budget(_IDENTITY) is False
    _cap(monkeypatch, enabled=True, limit=None, spent=5.0)
    assert autonomy_service.is_over_budget(_IDENTITY) is False


# ── r55 第 6 點：Haiku 5.5 依估計提示長度選檔 ─────────────────────────────────────


@pytest.mark.parametrize(("chars", "input_price", "output_price"), [
    (98_974, 0.125, 0.50),  # 估計剛好 100,000（字串 98,974＋引號 2＋餘量 1,024）＝便宜檔
    (98_975, 0.625, 2.50),  # 估計 100,001＝貴檔
])
def test_worst_case_call_picks_the_haiku_5_5_tier_by_estimated_length(
    monkeypatch, chars, input_price, output_price
):
    _no_pricing_override(monkeypatch)
    payload = "x" * chars
    tokens = cost_guard.estimate_input_tokens(payload)
    assert tokens == chars + 2 + 1_024
    expected = (tokens * input_price + 512 * output_price) / 1_000_000
    assert cost_guard.worst_case_call_usd("claude-haiku-5-5", payload, 512) == pytest.approx(
        expected)


# ── complete_once 送前估算（執行者 1 的接點併入後才會跑）──────────────────────────
# 接點照凍結介面在 completion.py 呼叫 would_exceed；還沒併入前整段跳過（不是失敗）。

_WIRED = "would_exceed" in inspect.getsource(completion)
_needs_wiring = pytest.mark.skipif(
    not _WIRED, reason="r55 T6：等執行者 1 的 complete_once 送前估算接點併入")


@pytest.fixture
def claude_wire(monkeypatch: pytest.MonkeyPatch) -> contract._Wire:
    """只換最外層的 Anthropic 客戶端（同契約測試）；記帳換成記錄器、不落盤。"""
    wire = contract._Wire()
    wire.ledger = []
    monkeypatch.setattr(completion, "AsyncAnthropic", wire.anthropic_factory)
    monkeypatch.setattr(ModelManager, "get_models",
                        lambda _self: [{"id": "Human", "owned_by": "human"}])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN))
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-anthropic", raising=False)
    monkeypatch.setattr(completion, "accumulate_token_usage",
                        lambda *a, **k: wire.ledger.append(("usage", a, k)))
    monkeypatch.setattr(completion, "log_usage_audit",
                        lambda *a, **k: wire.ledger.append(("audit", a, k)))
    _no_pricing_override(monkeypatch)
    return wire


async def _once(purpose: str, model: str, max_tokens: int) -> completion.CompletionResult:
    return await completion.complete_once(_IDENTITY, purpose, model, "系統提示", "使用者內容",
                                          max_tokens, 5.0)


@_needs_wiring
async def test_complete_once_refuses_when_spent_plus_worst_case_passes_the_cap(
    claude_wire, monkeypatch
):
    """上限 $1、已花 $0.95：Opus 5.5 評分實送 8,192 → 最壞約 $0.17 → 不送、不記帳、client 已關。"""
    _cap(monkeypatch, enabled=True, limit=1.0, spent=0.95)
    result = await _once("judge", "claude-opus-5-5", 16)
    assert (result.error.error_class, result.error.reason) == ("permission_denied",
                                                              "over_budget")
    assert claude_wire.claude_calls == []
    assert all(client.closed for client in claude_wire.claude)
    assert claude_wire.ledger == []


@_needs_wiring
async def test_complete_once_sends_when_the_worst_case_fits(claude_wire, monkeypatch):
    """同樣已花 $0.95：Haiku 5.5 摘要員（實送 256，便宜檔）最壞不到 $0.01 → 照送。"""
    _cap(monkeypatch, enabled=True, limit=1.0, spent=0.95)
    claude_wire.claude_script.append(contract._claude_msg())
    result = await _once("summarizer", "claude-haiku-5-5", 256)
    assert result.error is None
    assert len(claude_wire.claude_calls) == 1


@_needs_wiring
async def test_complete_once_sends_when_the_cap_is_off(claude_wire, monkeypatch):
    _cap(monkeypatch, enabled=False, limit=1.0, spent=999.0)
    claude_wire.claude_script.append(contract._claude_msg())
    result = await _once("judge", "claude-opus-5-5", 16)
    assert result.error is None
    assert len(claude_wire.claude_calls) == 1
