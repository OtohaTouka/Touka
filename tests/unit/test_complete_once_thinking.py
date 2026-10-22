"""r55：complete_once 依用途決定 Claude 5.5 的思考強度（T3）；審查員照舊、等 60 秒。

用途表（ADR-0271）：評分（judge）、摘要員（summarizer）、IDE 補全（inline_completion）
少想；審查員（auto_reviewer）與互審（critic）刻意不列＝照模型預設，送出內容一個 byte 都不變。
用途表經 ``_adapter_context`` 交給 ClaudeAdapter（completion.py 不讀規則表）；``_shape_claude``
把 schema 併進 adapter 已放的 ``output_config``，不覆寫 effort。

假傳輸只換 ``completion.AsyncAnthropic``；路由、adapter 都走真的程式；記帳換成記錄器。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from src.handlers.adapters.base import THINKING_PROFILE_KEY
from src.models.autonomy import AutoReviewConfig
from src.models.identity import AgentIdentity
from src.services import completion
from src.services.completion import (
    PURPOSE_INLINE_COMPLETION,
    _adapter_context,
    complete_once,
    thinking_profile_for,
)
from src.services.dispatchers.route_family import ANTHROPIC_FAMILY, GEMINI_FAMILY, ModelRoute
from src.services.models.manager import ModelManager
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution

_IDENTITY = AgentIdentity(agent_id="DebugAgent")
_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}},
           "required": ["verdict"], "additionalProperties": False}
_FORMAT = {"type": "json_schema", "schema": _SCHEMA}
_AUTONOMY_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "autonomy.json.example"
)


def _reply() -> Any:
    usage = {"input_tokens": 10, "output_tokens": 5}
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                           stop_reason="end_turn",
                           usage=SimpleNamespace(model_dump=lambda: dict(usage)))


class _FakeAnthropic:
    def __init__(self, sent: list[dict], **_kwargs: Any) -> None:
        self._sent = sent
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw: Any) -> Any:
        self._sent.append(copy.deepcopy(kw))
        return _reply()

    async def close(self) -> None:
        return None


class _Budget:
    def is_over_budget(self, _identity: AgentIdentity) -> bool:
        return False

    def would_exceed(self, _identity: AgentIdentity, _worst_usd: float) -> bool:
        return False


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(completion, "AsyncAnthropic", lambda **kw: _FakeAnthropic(calls, **kw))
    monkeypatch.setattr(completion, "autonomy_service", _Budget())
    monkeypatch.setattr(completion, "accumulate_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(completion, "log_usage_audit", lambda *a, **k: None)
    monkeypatch.setattr(ModelManager, "get_models", lambda _self: [])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN))
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-anthropic", raising=False)
    return calls


async def _send(sent: list[dict], purpose: str, model: str, max_tokens: int = 512,
                schema: dict | None = None) -> dict:
    result = await complete_once(_IDENTITY, purpose, model, "system", "user", max_tokens, 5.0,
                                 response_schema=schema)
    assert result.ok, result.error
    return sent[-1]


# ── 用途表 ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("purpose", "profile"), [
    ("judge", "minimal"),
    ("summarizer", "minimal"),
    (PURPOSE_INLINE_COMPLETION, "minimal"),
    ("auto_reviewer", None),  # ADR-0271：審查員照模型預設
    ("critic", None),  # 互審是品質關卡：不降
    ("reviewer", None),
    ("unknown", None),
    ("", None),
])
def test_thinking_profile_for(purpose, profile):
    assert thinking_profile_for(purpose) == profile


def test_inline_completion_purpose_name():
    assert PURPOSE_INLINE_COMPLETION == "inline_completion"


def _route(kind: str) -> ModelRoute:
    return ModelRoute(kind=kind, api_key="k")


@pytest.mark.parametrize(("kind", "purpose", "has_key"), [
    (ANTHROPIC_FAMILY, "judge", True),
    (ANTHROPIC_FAMILY, "auto_reviewer", False),
    (ANTHROPIC_FAMILY, "critic", False),
    (GEMINI_FAMILY, "judge", False),  # 其他家族不讀這個鍵：不放
])
def test_adapter_context_carries_profile_only_for_claude_minimal_purposes(kind, purpose,
                                                                           has_key):
    context = _adapter_context(_route(kind), _IDENTITY, object(), purpose)
    assert (THINKING_PROFILE_KEY in context) is has_key
    if has_key:
        assert context[THINKING_PROFILE_KEY] == "minimal"


# ── 送出內容 ──────────────────────────────────────────────────────────────────


async def test_summarizer_on_haiku_5_5_turns_thinking_off_and_keeps_the_budget(sent):
    call = await _send(sent, "summarizer", "claude-haiku-5-5", 512)
    assert (call["thinking"], call["output_config"], call["max_tokens"]) == (
        {"type": "disabled"}, {"effort": "low"}, 512)


async def test_judge_on_sonnet_5_5_uses_between_tools(sent):
    call = await _send(sent, "judge", "claude-sonnet-5-5", 16)
    assert (call["thinking"], call["output_config"], call["max_tokens"]) == (
        {"type": "between_tools"}, {"effort": "low"}, 16)


async def test_judge_on_opus_5_5_only_lowers_effort(sent):
    call = await _send(sent, "judge", "claude-opus-5-5", 16)
    assert "thinking" not in call
    assert (call["output_config"], call["max_tokens"]) == ({"effort": "low"}, 8192)


@pytest.mark.parametrize("purpose", ["auto_reviewer", "critic"])
@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
async def test_reviewer_and_critic_send_exactly_what_they_did_before(sent, purpose, model):
    call = await _send(sent, purpose, model, 512)
    assert "thinking" not in call and "output_config" not in call
    assert call["max_tokens"] == 8192
    assert sorted(call) == ["max_tokens", "messages", "model", "system"]


async def test_schema_merges_with_effort_instead_of_overwriting(sent):
    call = await _send(sent, "judge", "claude-haiku-5-5", 16, schema=_SCHEMA)
    assert call["output_config"] == {"effort": "low", "format": _FORMAT}


async def test_reviewer_schema_is_the_only_output_config(sent):
    call = await _send(sent, "auto_reviewer", "claude-haiku-5-5", 512, schema=_SCHEMA)
    assert call["output_config"] == {"format": _FORMAT}
    assert "thinking" not in call


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-opus-5", "claude-fable-5-1"])
async def test_purpose_changes_nothing_outside_5_5(sent, model):
    judged = await _send(sent, "judge", model, 16, schema=_SCHEMA)
    reviewed = await _send(sent, "auto_reviewer", model, 16, schema=_SCHEMA)
    assert judged == reviewed
    assert judged["output_config"] == {"format": _FORMAT}


# ── 送前最壞估算（r55 §5-3）：prepare＋shaper 之後、送出之前 ──────────────────────


class _Guard:
    """記錄送前估算的輸入；``over`` 決定 would_exceed 的答案。"""

    def __init__(self, over: bool = False) -> None:
        self.over = over
        self.estimates: list[tuple[Any, Any, int]] = []
        self.asked: list[tuple[AgentIdentity, float]] = []
        self.pre_checked: list[AgentIdentity] = []

    def worst_case_call_usd(self, model: Any, payload: Any, max_output_tokens: int) -> float:
        self.estimates.append((model, copy.deepcopy(payload), max_output_tokens))
        return 0.42

    def is_over_budget(self, identity: AgentIdentity) -> bool:
        self.pre_checked.append(identity)
        return False

    def would_exceed(self, identity: AgentIdentity, worst_usd: float) -> bool:
        self.asked.append((identity, worst_usd))
        return self.over


@pytest.fixture
def guard(sent, monkeypatch) -> _Guard:
    g = _Guard()
    monkeypatch.setattr(completion, "worst_case_call_usd", g.worst_case_call_usd)
    monkeypatch.setattr(completion, "autonomy_service", g)
    return g


async def test_pre_send_estimate_reads_what_will_actually_be_sent(sent, guard):
    call = await _send(sent, "judge", "claude-opus-5-5", 16, schema=_SCHEMA)
    ((model, payload, max_output),) = guard.estimates
    assert (model, max_output) == ("claude-opus-5-5", 8192)  # 實送值（含 8,192 下限）
    assert payload["kwargs"] == call  # 實際要送的內容（含 shaper 併入的 format）
    assert guard.asked == [(_IDENTITY, 0.42)]
    assert guard.pre_checked == [_IDENTITY]  # 既有的前置檢查照舊


async def test_pre_send_estimate_uses_the_waived_cap_when_thinking_is_off(sent, guard):
    await _send(sent, "summarizer", "claude-haiku-5-5", 256)
    assert guard.estimates[0][2] == 256


async def test_over_budget_estimate_sends_nothing_and_books_nothing(sent, guard, monkeypatch):
    booked: list[Any] = []
    monkeypatch.setattr(completion, "accumulate_token_usage", lambda *a, **k: booked.append(a))
    guard.over = True
    result = await complete_once(_IDENTITY, "judge", "claude-opus-5-5", "s", "u", 16, 5.0)
    assert (result.ok, result.error.reason, result.error.error_class) == (
        False, "over_budget", "permission_denied")
    assert (result.text, result.usage, result.cost_usd) == ("", {}, 0.0)
    assert sent == [] and booked == []


@pytest.mark.parametrize(("state", "requested", "expected"), [
    (SimpleNamespace(kwargs={"max_tokens": 8192}), 16, 8192),  # Claude（含下限）
    (SimpleNamespace(kwargs={"max_output_tokens": 300}), 256, 300),  # Responses
    (SimpleNamespace(payload={"max_tokens": 256}), 256, 256),  # 相容 Chat
    (SimpleNamespace(payload={"max_completion_tokens": 900}), 256, 900),
    (SimpleNamespace(payload={"generationConfig": {"maxOutputTokens": 512}}), 64, 512),  # Gemini
    (SimpleNamespace(payload={"messages": []}), 256, 256),  # 讀不到：用請求值
    (SimpleNamespace(kwargs={"max_tokens": 100}), 256, 256),  # 取較大
    (SimpleNamespace(payload={"max_tokens": "9999"}), 64, 64),  # 不是整數不算
])
def test_wire_max_output(state, requested, expected):
    assert completion._wire_max_output(state, requested) == expected


def test_wire_payload_is_what_the_adapter_will_send():
    state = SimpleNamespace(kwargs={"model": "m"}, payload=None)
    assert completion._wire_payload(state) == {"kwargs": {"model": "m"}, "payload": None}


# ── 審查員等待時間（ADR-0271：只加長到 60 秒） ───────────────────────────


def test_reviewer_timeout_defaults_to_60_seconds():
    assert AutoReviewConfig().timeout_s == 60.0
    assert AutoReviewConfig.model_validate({"model": "x"}).timeout_s == 60.0


def test_reviewer_timeout_example_matches_default():
    example = json.loads(_AUTONOMY_EXAMPLE.read_text(encoding="utf-8"))
    assert example["auto_review"]["timeout_s"] == AutoReviewConfig().timeout_s


@pytest.mark.parametrize("value", [0, -1, 120.5])
def test_reviewer_timeout_bounds_unchanged(value):
    with pytest.raises(ValueError):
        AutoReviewConfig(timeout_s=value)
