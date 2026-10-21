"""Phase 3 前置（互審設計文件 §7.1-A）：LIVE episode seam——TDD.

接通 `harness._run_case_live`：真跑 producer 子 loop（重用 Phase 1 組裝——真實
pipeline＝persona/soul 可注入；工具真的執行）→ SSE 抽 tool calls＋最終文字 →
Phase 2 回報塊解析 → artifact 存在檢查。單元測試全離線（mock 串流／組裝縫），
真實 LIVE 跑仍為擁有者手動（帶 API 金鑰、顯式 live_model）。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from src.models.identity import AgentIdentity

from . import harness, live_episode
from .cases import EvalCase, ToolCall
from .live_episode import LiveEpisode, run_case_live_episode

# --------------------------------------------------------------------------- #
# 輔助：偽 SSE 串流
# --------------------------------------------------------------------------- #

_REPORT_BLOCK = (
    "```json\n"
    '{"status": "done", "claims": ["寫出 out.md"], "artifact_refs": ["out.md"],'
    ' "confidence": 0.9}\n'
    "```"
)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _episode_stream_chunks() -> list[str]:
    return [
        _sse(
            {
                "type": "tool_start",
                "id": "c1",
                "tool": "write",
                "args": {"path": "out.md", "content": "x"},
            }
        ),
        _sse({"type": "tool_result", "id": "c1", "result": "ok", "status": "success"}),
        _sse(
            {
                "type": "tool_start",
                "id": "c2",
                "tool": "execute_command",
                "args": {"command": "rm -rf /"},
            }
        ),
        _sse(
            {
                "type": "tool_result",
                "id": "c2",
                "result": "denied",
                "status": "error",
                "error_class": "permission_denied",
            }
        ),
        _sse({"type": "tool_start", "id": "c3", "tool": "grep", "args": {"query": "x"}}),
        _sse({"type": "tool_result", "id": "c3", "result": "boom", "status": "error"}),
        _sse(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "完成。\n"},
            }
        ),
        _sse(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": _REPORT_BLOCK},
            }
        ),
        "data: [DONE]\n\n",
    ]


async def _fake_stream():
    for chunk in _episode_stream_chunks():
        yield chunk


def _case() -> EvalCase:
    return EvalCase(
        id="live-episode-case",
        title="t",
        prompt="寫一份摘要到 out.md",
        context={"agent_id": "DebugAgent", "role": "Debugger"},
        expected_artifact_check={"must_exist": ["out.md"]},
    )


class _FakeRequest:
    def __init__(self) -> None:
        self.model = "fake-brain"
        self.max_tokens = 8192
        self.tools: list[dict] = []


class _UnexpectedEpisodeAssembly(AssertionError):
    def __init__(self) -> None:
        super().__init__("episode assembly must not be reached")


# --------------------------------------------------------------------------- #
# 1. SSE drain：tool 事件 join ＋ 文字
# --------------------------------------------------------------------------- #


async def test_drain_joins_tool_events_and_text():
    text, calls = await live_episode._drain_episode(_fake_stream())
    assert "完成。" in text
    assert calls == [
        ToolCall("write", {"path": "out.md", "content": "x"}, status="ok"),
        ToolCall("execute_command", {"command": "rm -rf /"}, status="denied"),
        ToolCall("grep", {"query": "x"}, status="error"),
    ]


# --------------------------------------------------------------------------- #
# 2. episode 全流程（mock 組裝＋串流縫）
# --------------------------------------------------------------------------- #


async def test_run_case_live_episode_full(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    fake_req = _FakeRequest()

    async def _fake_assemble(*, case, identity, model, base_ctx):
        captured["identity"] = identity
        captured["model"] = model
        return fake_req

    async def _fake_factory(
        model_name, request, identity, request_id, start_time, context, session_id=None
    ):
        captured["stream_identity"] = identity
        captured["request_id"] = request_id
        return _fake_stream()

    monkeypatch.setattr(live_episode, "_assemble_episode_request", _fake_assemble)

    (tmp_path / "out.md").write_text("摘要", encoding="utf-8")

    episode = await run_case_live_episode(
        _case(),
        model="gpt-test",
        artifact_root=tmp_path,
        limits=live_episode.LiveEpisodeLimits(
            provider="openai",
            max_tokens=96,
            max_provider_requests=1,
        ),
        stream_factory=_fake_factory,
    )

    assert isinstance(episode, LiveEpisode)
    assert [c.name for c in episode.tool_calls] == ["write", "execute_command", "grep"]
    assert "完成。" in episode.final_text
    assert episode.report.status == "done"
    assert episode.report.artifact_refs == ["out.md"]
    assert episode.artifacts_present == {"out.md": True}
    # persona 縫：以 case.context 的 agent_id 組裝（真實 pipeline → soul 注入）
    assert isinstance(captured["identity"], AgentIdentity)
    assert captured["identity"].agent_id == "DebugAgent"
    assert captured["model"] == "gpt-test"


async def test_artifact_root_none_yields_empty_presence(monkeypatch):
    fake_req = _FakeRequest()

    async def _fake_assemble(**_kw: Any):
        return fake_req

    async def _fake_factory(*_a: Any, **_kw: Any):
        return _fake_stream()

    monkeypatch.setattr(live_episode, "_assemble_episode_request", _fake_assemble)

    episode = await run_case_live_episode(
        _case(),
        model="gpt-test",
        limits=live_episode.LiveEpisodeLimits(
            provider="openai",
            max_tokens=96,
            max_provider_requests=1,
        ),
        stream_factory=_fake_factory,
    )
    assert episode.artifacts_present == {}


async def test_live_episode_rejects_missing_limits_before_assembly(monkeypatch):
    async def _unexpected_assemble(**_kw: Any):
        raise _UnexpectedEpisodeAssembly

    monkeypatch.setattr(live_episode, "_assemble_episode_request", _unexpected_assemble)
    with pytest.raises(live_episode.LiveEpisodeLimitsRequired):
        await run_case_live_episode(_case(), model="gpt-test", limits=None)


async def test_true_live_episode_rejects_provider_model_mismatch_before_assembly(
    monkeypatch,
):
    async def _unexpected_assemble(**_kw: Any):
        raise _UnexpectedEpisodeAssembly

    monkeypatch.setattr(live_episode, "_assemble_episode_request", _unexpected_assemble)
    with pytest.raises(live_episode.LiveEpisodeProviderMismatch):
        await run_case_live_episode(
            _case(),
            model="gpt-test",
            limits=live_episode.LiveEpisodeLimits(
                provider="anthropic",
                max_tokens=64,
                max_provider_requests=1,
                anthropic_client=object(),
            ),
        )


async def test_true_anthropic_episode_requires_no_retry_client_before_assembly(
    monkeypatch,
):
    async def _unexpected_assemble(**_kw: Any):
        raise _UnexpectedEpisodeAssembly

    monkeypatch.setattr(live_episode, "_assemble_episode_request", _unexpected_assemble)
    with pytest.raises(live_episode.LiveEpisodeNoRetryClientRequired):
        await run_case_live_episode(
            _case(),
            model="claude-test",
            limits=live_episode.LiveEpisodeLimits(
                provider="anthropic",
                max_tokens=64,
                max_provider_requests=1,
            ),
        )


async def test_public_limits_override_tokens_and_inject_no_retry_client(monkeypatch):
    captured: dict[str, Any] = {}
    fake_req = _FakeRequest()
    fake_client = object()

    async def _fake_assemble(**_kw: Any):
        return fake_req

    async def _fake_factory(*_args: Any, **kwargs: Any):
        captured["context"] = kwargs.get("context") or _args[5]
        captured["max_tokens"] = _args[1].max_tokens
        return _fake_stream()

    monkeypatch.setattr(live_episode, "_assemble_episode_request", _fake_assemble)
    limits = live_episode.LiveEpisodeLimits(
        provider="anthropic",
        max_tokens=96,
        max_provider_requests=2,
        anthropic_client=fake_client,
    )

    await run_case_live_episode(
        _case(),
        model="claude-test",
        limits=limits,
        stream_factory=_fake_factory,
    )

    assert captured["max_tokens"] == 96
    assert captured["context"]["anthropic_client"] is fake_client


def test_public_limits_fail_closed_for_provider_with_internal_retry() -> None:
    with pytest.raises(live_episode.UnsupportedLiveEpisodeProvider, match="Gemini"):
        live_episode.LiveEpisodeLimits(
            provider="gemini",
            max_tokens=64,
            max_provider_requests=1,
        )


# --------------------------------------------------------------------------- #
# 3. harness 接線：mode="live" 不再 NotImplementedError
# --------------------------------------------------------------------------- #


def test_harness_live_wired_returns_tool_calls():
    calls = [ToolCall("read", {"scope": "project", "path": "a.md"}, status="ok")]
    episode = LiveEpisode(
        tool_calls=calls,
        final_text="ok",
        report=live_episode.parse_subagent_report("ok"),
        artifacts_present={},
    )

    async def _fake_episode(
        case,
        *,
        model,
        agent_id=None,
        artifact_root=None,
        limits,
        stream_factory=None,
    ):
        return episode

    with patch.object(live_episode, "run_case_live_episode", _fake_episode):
        observed = harness.run_case(
            _case(),
            mode="live",
            live_model="gpt-test",
            live_limits=live_episode.LiveEpisodeLimits(
                provider="openai",
                max_tokens=64,
                max_provider_requests=1,
            ),
        )
    assert observed == calls


def test_harness_live_without_model_raises_config_error():
    with pytest.raises(RuntimeError, match="live_model"):
        harness.run_case(_case(), mode="live")
