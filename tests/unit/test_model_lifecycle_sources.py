"""模型狀態的另外兩個來源（ADR-0310）：Gemini 回覆裡的 modelStatus、complete_once 的「找不到模型」。

審查員和摘要員走 complete_once，沒有畫面可以講原因，所以廠商回「找不到模型」要記下來提示。
假傳輸只換 ``httpx.AsyncClient.send``；清單查詢在 test_model_lifecycle.py。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers.adapters import gemini_adapter
from src.handlers.adapters.base import RoundError
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.models.identity import AgentIdentity
from src.services import completion
from src.services.completion import complete_once
from src.services.models.manager import ModelManager
from src.services.models.model_lifecycle import ModelLifecycleService, ModelNotice
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution

from tests.helpers.stream_transport import FakeUpstream, Reply, json_reply, sse, status_reply

_MODEL = "gemini-2.5-flash"
_IDENTITY = AgentIdentity(agent_id="DebugAgent")
_DATE_UNKNOWN = ModelNotice("date_unknown", None)
_NOT_FOUND = ModelNotice("not_found", None)
_LEGACY = {"modelStage": "LEGACY"}


def _body(text: str = "好", **extra: Any) -> dict[str, Any]:
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                        "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
        **extra,
    }


@pytest.fixture
def notes(monkeypatch: pytest.MonkeyPatch) -> ModelLifecycleService:
    service = ModelLifecycleService()
    monkeypatch.setattr(gemini_adapter, "model_lifecycle_service", service)
    monkeypatch.setattr(completion, "model_lifecycle_service", service)
    return service


async def _round(adapter: GeminiAdapter) -> list[Any]:
    request = OpenAIChatRequest(model=_MODEL, messages=[OpenAIMessage(role="user", content="hi")])
    state = await adapter.prepare_request(request, {"gemini_api_key": "fake", "agent_id": "Sumi"})
    items = [item async for item in adapter.send_round(state)]
    await adapter.close(state)
    return items


async def test_non_stream_reply_reports_model_status(
    monkeypatch: pytest.MonkeyPatch, notes: ModelLifecycleService
) -> None:
    FakeUpstream([json_reply(_body(modelStatus=_LEGACY))]).install(monkeypatch)
    await _round(GeminiAdapter())
    assert notes.notice_for(_MODEL) == _DATE_UNKNOWN


async def test_stream_chunk_reports_model_status(
    monkeypatch: pytest.MonkeyPatch, notes: ModelLifecycleService
) -> None:
    FakeUpstream([Reply(200, [sse(_body(modelStatus=_LEGACY), done=False)])]).install(monkeypatch)
    await _round(GeminiAdapter(stream=True))
    assert notes.notice_for(_MODEL) == _DATE_UNKNOWN


async def test_status_in_an_early_chunk_is_not_lost(
    monkeypatch: pytest.MonkeyPatch, notes: ModelLifecycleService
) -> None:
    first = {"candidates": [{"content": {"role": "model", "parts": [{"text": "你"}]}, "index": 0}],
             "modelStatus": _LEGACY}
    FakeUpstream([Reply(200, [sse(first, _body(), done=False)])]).install(monkeypatch)
    await _round(GeminiAdapter(stream=True))
    assert notes.notice_for(_MODEL) == _DATE_UNKNOWN


async def test_reply_without_status_records_nothing(
    monkeypatch: pytest.MonkeyPatch, notes: ModelLifecycleService
) -> None:
    FakeUpstream([json_reply(_body())]).install(monkeypatch)
    await _round(GeminiAdapter())
    assert notes.notice_for(_MODEL) is None


async def test_error_reply_records_no_status(
    monkeypatch: pytest.MonkeyPatch, notes: ModelLifecycleService
) -> None:
    FakeUpstream([status_reply(500, '{"modelStatus": {"modelStage": "LEGACY"}}')]).install(
        monkeypatch
    )
    await _round(GeminiAdapter())
    assert notes.notice_for(_MODEL) is None


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(404, "model_not_found"), (503, "server_error"), (400, "invalid_request")],
)
async def test_non_main_loop_labels_the_failure_without_retrying(
    monkeypatch: pytest.MonkeyPatch, status: int, error_type: str
) -> None:
    upstream = FakeUpstream([status_reply(status)]).install(monkeypatch)
    adapter = GeminiAdapter()
    items = await _round(adapter)
    assert len(upstream.requests) == 1
    assert isinstance(items[-1], RoundError)
    assert items[-1].error_type == error_type
    assert items[-1].retry_note is None
    assert items[-1].retry_after is None


# ── complete_once ───────────────────────────────────────


class _Budget:
    def is_over_budget(self, _identity: AgentIdentity) -> bool:
        return False

    def would_exceed(self, _identity: AgentIdentity, _worst_usd: float) -> bool:
        return False


@pytest.fixture
def route(monkeypatch: pytest.MonkeyPatch, notes: ModelLifecycleService) -> ModelLifecycleService:
    monkeypatch.setattr(completion, "autonomy_service", _Budget())
    monkeypatch.setattr(completion, "accumulate_token_usage", lambda *_a, **_k: None)
    monkeypatch.setattr(completion, "log_usage_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(ModelManager, "get_models",
                        lambda _self: [{"id": "Human", "owned_by": "human"}])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN))
    monkeypatch.setattr("src.config.GEMINI_API_KEY", "fake-gemini", raising=False)
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", "fake-deepseek", raising=False)
    monkeypatch.setattr("src.config.DEEPSEEK_BASE_URL", "https://deepseek.test/v1",
                        raising=False)
    return notes


async def _call(model: str = _MODEL) -> Any:
    return await complete_once(_IDENTITY, "reviewer", model, "system", "user", 256, 5.0)


async def test_complete_once_remembers_not_found(
    monkeypatch: pytest.MonkeyPatch, route: ModelLifecycleService
) -> None:
    FakeUpstream([status_reply(404)]).install(monkeypatch)
    result = await _call()
    assert result.error is not None
    assert route.notice_for(_MODEL) == _NOT_FOUND


async def test_complete_once_success_clears_not_found(
    monkeypatch: pytest.MonkeyPatch, route: ModelLifecycleService
) -> None:
    route.record_not_found(_MODEL)
    FakeUpstream([json_reply(_body())]).install(monkeypatch)
    result = await _call()
    assert result.error is None
    assert route.notice_for(_MODEL) is None


async def test_complete_once_openai_style_not_found(
    monkeypatch: pytest.MonkeyPatch, route: ModelLifecycleService
) -> None:
    FakeUpstream([status_reply(404)]).install(monkeypatch)
    await _call("deepseek-chat")
    assert route.notice_for("deepseek-chat") == _NOT_FOUND


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
async def test_other_failures_are_not_not_found(
    monkeypatch: pytest.MonkeyPatch, route: ModelLifecycleService, status: int
) -> None:
    FakeUpstream([status_reply(status)]).install(monkeypatch)
    await _call()
    assert route.notice_for(_MODEL) is None


async def test_other_failure_keeps_an_earlier_not_found(
    monkeypatch: pytest.MonkeyPatch, route: ModelLifecycleService
) -> None:
    route.record_not_found(_MODEL)
    FakeUpstream([status_reply(500)]).install(monkeypatch)
    await _call()
    assert route.notice_for(_MODEL) == _NOT_FOUND


async def test_connection_failure_is_not_not_found(
    monkeypatch: pytest.MonkeyPatch, route: ModelLifecycleService
) -> None:
    FakeUpstream([Reply(error=httpx.ConnectError("down"))]).install(monkeypatch)
    await _call()
    assert route.notice_for(_MODEL) is None
