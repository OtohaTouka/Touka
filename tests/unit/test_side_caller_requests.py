"""model-p1b U4：主線外三個旁路呼叫交給 complete_once 的內容被凍結（ADR-0244）。

摘要（dialogue_summarizer）、互審 critic（review_loop.build_critic_completer）與
評分（creation.model_judge.ModelJudge）都只經 ``complete_once``、不再自建 Anthropic client。
一律注入假的 Completer 攔下參數，整組以字面值比對 purpose／model／max_tokens／timeout，
system 與 user 原樣轉交；不建立真的連線、不記帳。
"""

from __future__ import annotations

from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.services.completion import CompletionResult
from src.services.creation.model_judge import ModelJudge
from src.services.creation.models import EvalTask
from src.services.dialogue_summarizer import summarize_dialogue
from src.services.review.review_loop import build_critic_completer

_OWNER = AgentIdentity(agent_id="DebugAgent")


class _FakeCompleter:
    """簽名同 Completer；記下每次呼叫的完整參數，回固定成功結果。"""

    def __init__(self, text: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self._text = text

    async def __call__(
        self,
        identity: AgentIdentity,
        purpose: str,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        timeout_s: float,
        response_schema: dict[str, Any] | None = None,
    ) -> CompletionResult:
        self.calls.append({
            "identity": identity, "purpose": purpose, "model": model, "system": system,
            "user": user, "max_tokens": max_tokens, "timeout_s": timeout_s,
            "response_schema": response_schema,
        })
        return CompletionResult(text=self._text, usage={}, cost_usd=0.0, stop_reason="end",
                                error=None)


@pytest.mark.parametrize(("budget", "max_tokens"), [(10, 64), (300, 300), (99999, 4096)])
async def test_summarizer_request(monkeypatch, budget, max_tokens):
    """max_tokens＝budget 夾在 64～4096；逾時 30 秒；對話攤平成 user 一則。"""
    fake = _FakeCompleter("• 約定下週交付")
    monkeypatch.setattr("src.services.dialogue_summarizer.complete_once", fake)
    summary = await summarize_dialogue(
        _OWNER,
        [{"role": "user", "content": "我們約好下週交付。"},
         {"role": "assistant", "content": "好的，我會準備。"}],
        budget, "claude-haiku-4-5",
    )
    assert summary is not None and summary.split("\n", 1)[1] == "• 約定下週交付"  # 第一行是時間範圍
    assert fake.calls == [{
        "identity": _OWNER,
        "purpose": "summarizer",
        "model": "claude-haiku-4-5",
        "system": (
            "你是對話歸檔摘要員。下面是一段『即將移出 AI 即時脈絡的較早對話』。"
            "請壓成第三人稱要點，保留：人名、做過的決定、承諾與待辦、未解問題、"
            "情緒或關係的轉折；丟棄寒暄、重複與無關閒聊。"
            "用繁體中文輸出精簡條列，不要開場白或結語，只要要點本身。"
            "每行開頭方括號是該則訊息在對話紀錄裡的台北時間（可能來自使用者裝置的時鐘；"
            "unknown time 表示沒有紀錄）。有可靠時間戳時，把「明天」「昨天」「下週」這類相對時間"
            "換成絕對日期；時間不明或不確定時保留原話。"
        ),
        "user": (
            "[unknown time] user: 我們約好下週交付。\n[unknown time] assistant: 好的，我會準備。"
        ),
        "max_tokens": max_tokens,
        "timeout_s": 30.0,
        "response_schema": None,
    }]


async def test_critic_completer_request(monkeypatch):
    """critic 固定 max_tokens 1024、逾時 120 秒；system／user 由 GroundedReviewer 給，原樣轉交。"""
    fake = _FakeCompleter("審查結果")
    monkeypatch.setattr("src.services.review.review_loop.complete_once", fake)
    completer = build_critic_completer(_OWNER, "claude-opus-4-8")
    assert await completer("審查系統提示", "審查內容") == "審查結果"
    assert fake.calls == [{
        "identity": _OWNER,
        "purpose": "critic",
        "model": "claude-opus-4-8",
        "system": "審查系統提示",
        "user": "審查內容",
        "max_tokens": 1024,
        "timeout_s": 120.0,
        "response_schema": None,
    }]


async def test_judge_request():
    """評分固定 max_tokens 16、逾時 30 秒；產物只送前 4000 字；記帳到建立者。"""
    fake = _FakeCompleter("0.8")
    tasks = [EvalTask(id="t1", title="列出檔案", expectation="回傳清單")]
    score = await ModelJudge(fake, "claude-haiku-4-5").score(
        tasks, "# Skill\n" + "x" * 5000, _OWNER
    )
    assert score == 0.8
    assert fake.calls == [{
        "identity": _OWNER,
        "purpose": "judge",
        "model": "claude-haiku-4-5",
        "system": (
            "You are a strict evaluation judge. Given read-only evaluation tasks and a "
            "generated artifact, reply with ONLY a single float in [0.0, 1.0] scoring how "
            "well the artifact satisfies the tasks. No words, no explanation — just the number."
        ),
        "user": "# Tasks\n- 列出檔案: 回傳清單\n\n# Artifact\n# Skill\n" + "x" * 3992
                + "\n\nScore (0.0-1.0):",
        "max_tokens": 16,
        "timeout_s": 30.0,
        "response_schema": None,
    }]


async def test_judge_without_creator_bills_system_account():
    """拿不到建立者身分時，記到 agent_id="System"、無角色的系統帳。"""
    fake = _FakeCompleter("0.5")
    await ModelJudge(fake, "claude-haiku-4-5").score([], "md")
    assert [call["identity"] for call in fake.calls] == [AgentIdentity(agent_id="System")]
    assert fake.calls[0]["identity"].agent_role is None
