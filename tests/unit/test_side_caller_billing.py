"""model-p1b U4：三個旁路呼叫改走 complete_once 後一律記帳、超預算回非成功（ADR-0244）。

原本摘要、互審 critic、創建評分只走 Anthropic SDK：不記帳、不受成本閘。這裡走真的
complete_once（路由、adapter、用量正規化），只換最外層傳輸（``completion.AsyncAnthropic``）
與記帳、成本閘的落點；不落盤、不連網。超預算時三者照各自原本的失敗語意處理：
摘要回 None、critic fail-closed 成 REJECT、評分回 0.0。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.services import completion
from src.services.creation.model_judge import ModelJudge
from src.services.creation.models import EvalTask
from src.services.dialogue_summarizer import summarize_dialogue
from src.services.models.manager import ModelManager
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution
from src.services.review.review_loop import build_critic_completer
from src.services.review.reviewer import EvidenceBundle, GroundedReviewer

_IDENTITY = AgentIdentity(agent_id="DebugAgent")
_MODEL = "claude-haiku-4-5"
_USAGE = {"prompt_tokens": 100, "cache_read_input_tokens": 0,
          "cache_creation_input_tokens": 0, "completion_tokens": 20}
_APPROVE = ('```json\n{"verdict": "APPROVE", "reasoning": "ok", "required_fixes": [],'
            ' "confidence": 0.9}\n```')


class _Wire:
    """假的 AsyncAnthropic：記下送出的請求，回固定文字與用量。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.reply = ""

    def factory(self, **_kwargs: Any) -> Any:
        async def _create(**kw: Any) -> Any:
            self.sent.append(kw)
            usage = {"input_tokens": 100, "output_tokens": 20}
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=self.reply)],
                stop_reason="end_turn",
                usage=SimpleNamespace(model_dump=lambda: dict(usage)),
            )

        async def _close() -> None:
            return None

        return SimpleNamespace(messages=SimpleNamespace(create=_create), close=_close)


@pytest.fixture
def wire(monkeypatch) -> _Wire:
    w = _Wire()
    monkeypatch.setattr(completion, "AsyncAnthropic", w.factory)
    monkeypatch.setattr(ModelManager, "get_models", lambda _self: [])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN))
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-anthropic", raising=False)
    return w


@pytest.fixture
def budget(monkeypatch) -> SimpleNamespace:
    state = SimpleNamespace(over=False, checked=[])

    def _is_over_budget(identity: AgentIdentity) -> bool:
        state.checked.append(identity)
        return state.over

    monkeypatch.setattr(completion, "autonomy_service",
                        SimpleNamespace(is_over_budget=_is_over_budget,
                                        would_exceed=lambda _identity, _worst: False))
    return state


@pytest.fixture
def ledger(monkeypatch) -> list:
    book: list = []
    monkeypatch.setattr(completion, "accumulate_token_usage", lambda *a, **k: book.append((a, k)))
    monkeypatch.setattr(completion, "log_usage_audit", lambda *a, **k: None)
    return book


async def _summarize() -> str | None:
    return await summarize_dialogue(
        _IDENTITY, [{"role": "user", "content": "約好下週交付"}], 100, _MODEL
    )


async def _critic():
    reviewer = GroundedReviewer(build_critic_completer(_IDENTITY, _MODEL),
                                critic_model=_MODEL, reviewer_id="r")
    return await reviewer.review(goal="g", boundary_rules=[], producer_claims="",
                                 evidence=EvidenceBundle(artifacts_present={"out.md": True}))


async def _judge(identity: AgentIdentity | None = _IDENTITY) -> float:
    tasks = [EvalTask(id="t1", title="T", expectation="e")]
    return await ModelJudge(completion.complete_once, _MODEL).score(tasks, "md", identity)


async def test_summarizer_bills_its_identity(wire, budget, ledger):
    wire.reply = "• 約定下週交付"
    assert (await _summarize() or "").endswith("\n• 約定下週交付")  # 第一行是時間範圍
    assert budget.checked == [_IDENTITY]
    assert ledger == [((_IDENTITY, "summarizer", _USAGE), {"e_index": None, "model": _MODEL})]


async def test_critic_bills_its_identity(wire, budget, ledger):
    wire.reply = _APPROVE
    report = await _critic()
    assert report.verdict == "APPROVE"
    assert budget.checked == [_IDENTITY]
    assert ledger == [((_IDENTITY, "critic", _USAGE), {"e_index": None, "model": _MODEL})]


async def test_judge_bills_creator_identity(wire, budget, ledger):
    wire.reply = "0.7"
    assert await _judge() == 0.7
    assert budget.checked == [_IDENTITY]
    assert ledger == [((_IDENTITY, "judge", _USAGE), {"e_index": None, "model": _MODEL})]


async def test_judge_without_creator_bills_system_account(wire, budget, ledger):
    wire.reply = "0.7"
    assert await _judge(None) == 0.7
    system = AgentIdentity(agent_id="System")
    assert budget.checked == [system]
    assert ledger == [((system, "judge", _USAGE), {"e_index": None, "model": _MODEL})]


async def test_over_budget_summarizer_returns_none_without_sending(wire, budget, ledger):
    budget.over = True
    wire.reply = "不該送出"
    assert await _summarize() is None
    assert wire.sent == [] and ledger == []


async def test_over_budget_critic_fails_closed(wire, budget, ledger):
    budget.over = True
    wire.reply = _APPROVE
    report = await _critic()
    assert report.verdict == "REJECT"
    assert report.reasoning == "fail-closed: completer error: CriticCallError"
    assert wire.sent == [] and ledger == []


async def test_over_budget_judge_scores_zero(wire, budget, ledger):
    budget.over = True
    wire.reply = "0.9"
    assert await _judge() == 0.0
    assert wire.sent == [] and ledger == []
