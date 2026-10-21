"""react-core-extraction E4 守衛錨：ReAct 迴圈邏輯唯一存在於 react_core（SSOT）。

ADR-0155 修掉的六個漂移 bug 病灶＝同一狀態機三份拷貝；抽取後本錨防止回退——
任何人把迴圈邏輯（保險絲/中斷閘/fan-out/進度/guard 呼叫）重新 inline 回
handler 薄殼或 adapter，此處即紅。薄殼的唯一職責＝入口名轉接
（``run_react_loop(XxxAdapter(), request, context)``），factory 與呼叫端零改動。
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.handlers import claude_handler, gemini_handler, openai_handler, react_core
from src.handlers.adapters import claude_adapter, gemini_adapter, openai_adapter
from src.handlers.adapters.base import ProviderAdapter, RoundError, Turn
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import dispatcher
from src.services.dispatchers import factory
from src.services.models.routes import CustomRouteDisposition
from src.services.stream_buffer_service import StreamBufferService
from src.utils import async_db

# 迴圈邏輯的指紋字串（每個都曾三份拷貝＝ADR-0155 的病理切片）
_LOOP_MARKERS = (
    "while True",  # ReAct 迴圈本體
    "wrapping_up",  # ADR-0146 額度耗盡收尾旗標
    "已由使用者中斷",  # 雙中斷閘（呼叫前/回合後）字串
    "consume_round",  # 工具輪保險絲
    "react_slots_event(",  # 有限 run 進度事件
    "ToolExecutor(",  # 工具 fan-out 建構
    "collect_known_tool_names(",  # 防幻覺 guard 呼叫點（禁令 59(c) SSOT）
)

_SHIM_MODULES = {
    "openai_handler.py": openai_handler,
    "claude_handler.py": claude_handler,
    "gemini_handler.py": gemini_handler,
}

_ADAPTER_MODULES = {
    "openai_adapter.py": openai_adapter,
    "claude_adapter.py": claude_adapter,
    "gemini_adapter.py": gemini_adapter,
}


def test_react_core_owns_all_loop_markers():
    """正向錨：每個迴圈指紋都必須存在於 react_core（單一實作）。"""
    src = inspect.getsource(react_core)
    for marker in _LOOP_MARKERS:
        assert marker in src, f"react_core 缺少迴圈指紋：{marker}"


@pytest.mark.parametrize("shim_file", sorted(_SHIM_MODULES))
def test_handler_shims_contain_no_loop_logic(shim_file: str):
    """反向錨：三 handler＝薄殼，不得含任何迴圈邏輯（防 inline 回退）。"""
    src = inspect.getsource(_SHIM_MODULES[shim_file])
    for marker in _LOOP_MARKERS:
        assert marker not in src, f"{shim_file} 不得含迴圈邏輯（{marker}）——修 react_core"
    assert "run_react_loop" in src, f"{shim_file} 薄殼必須轉接 run_react_loop"


@pytest.mark.parametrize("adapter_file", sorted(_ADAPTER_MODULES))
def test_adapters_contain_no_loop_logic(adapter_file: str):
    """反向錨：adapter＝wire 翻譯，不得長出迴圈政策（SRP 邊界）。"""
    src = inspect.getsource(_ADAPTER_MODULES[adapter_file])
    for marker in _LOOP_MARKERS:
        assert marker not in src, f"{adapter_file} 不得含迴圈邏輯（{marker}）——歸 react_core"


@pytest.mark.parametrize("failure", ["round_error", "interrupted_before", "interrupted_after"])
@pytest.mark.parametrize("explicit_ui", [True, False])
@pytest.mark.parametrize("ingress", ["direct", "converted"])
@pytest.mark.asyncio
async def test_true_react_failure_reaches_ui_terminal_without_partial_save(
    tmp_path, monkeypatch, failure, explicit_ui, ingress
):
    monkeypatch.setattr(async_db, "get_db_path", lambda: tmp_path / "react.db")
    await async_db.init_db_async()
    owner = AgentIdentity.for_role(agent_id="synthetic-human", role="normal", is_human=True)
    tree = {
        "messages": {"user": {"id": "user", "role": "user", "content": "hi"}},
        "currentId": "user",
    }
    assert await async_db.session_service.upsert_session(
        "react", "test", tree, owner_user_id=owner.agent_id
    )
    snapshot = await async_db.session_service.read_persistence_snapshot("react", owner)
    admission = {
        "mode": "persistent",
        "revision": snapshot.revision,
        "user_parent_id": "user",
        "assistant_node_id": "assistant",
    }
    request_model = AnthropicRequest if ingress == "converted" else OpenAIChatRequest
    request = request_model(
        model="test",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        session_id="react",
        **({"ui_persistence": admission} if explicit_ui else {}),
    )
    ctx = PipelineContext(session_id="react", identity=owner, request_id="test", t0=0.0)
    if explicit_ui:
        ctx["ui_requester"] = owner
        ctx["ui_authorized"] = True
    buffer = StreamBufferService()
    monkeypatch.setattr(dispatcher, "stream_buffer_service", buffer)
    monkeypatch.setattr(dispatcher, "record_working_brain", Mock())
    save = AsyncMock(wraps=async_db.session_service.conditional_save_session)
    monkeypatch.setattr(async_db.session_service, "conditional_save_session", save)
    trim = AsyncMock()
    monkeypatch.setattr(dispatcher, "_maybe_trim_after_chat", trim)
    monkeypatch.setattr(react_core.rhythm_service, "initialize_session", Mock())
    monkeypatch.setattr(react_core.rhythm_service, "get_effective_rounds", Mock(return_value=0))
    monkeypatch.setattr(
        react_core.rhythm_service,
        "is_interrupted",
        Mock(
            side_effect=[True]
            if failure == "interrupted_before"
            else [False, failure == "interrupted_after"]
        ),
    )
    adapter = Mock(spec=ProviderAdapter)
    adapter.prepare_request = AsyncMock(return_value=SimpleNamespace(chunk_model="test"))
    adapter.close = AsyncMock()
    adapter.parse_round.return_value = Turn(text_parts=["partial response"])
    adapter.extract_usage.return_value = ({}, 0, 0)
    adapter.finalize.return_value = {}
    adapter.resolve_provider_label = Mock(return_value="test")

    async def send(_state):
        yield RoundError("synthetic-private-upstream-error") if failure == "round_error" else {}

    adapter.send_round.side_effect = send

    async def get_stream(*_args, **_kwargs):
        return react_core.run_react_loop(adapter, request, ctx)

    if ingress == "converted":
        monkeypatch.setattr(
            factory,
            "model_manager",
            SimpleNamespace(
                get_models=Mock(return_value=[]),
                resolve_custom_route=Mock(
                    return_value=SimpleNamespace(disposition=CustomRouteDisposition.UNKNOWN)
                ),
            ),
        )
        monkeypatch.setattr(
            factory,
            "get_provider_info",
            Mock(return_value=("https://synthetic.invalid/v1", "", "test")),
        )
        monkeypatch.setattr(openai_handler, "OpenAIAdapter", lambda **_kw: adapter)
        monkeypatch.setattr(dispatcher, "get_stream_generator", factory.get_stream_generator)
    else:
        monkeypatch.setattr(dispatcher, "get_stream_generator", get_stream)
    response = await dispatcher.dispatch_request("test", request, owner, "test", 0.0, ctx)
    chunks = [chunk async for chunk in response.body_iterator]
    events = [json.loads(chunk[6:]) for chunk in chunks if chunk.startswith("data: {")]
    if explicit_ui:
        terminal = next(event for event in events if event.get("type") == "touka_stream_terminal")
        assert terminal["generation"] == ("error" if failure == "round_error" else "cancelled")
        assert terminal["persistence"]["status"] == "unknown"
        assert "synthetic-private" not in "".join(chunks)
        assert (
            await async_db.session_service.read_persistence_snapshot("react", owner)
        ).revision == snapshot.revision
    else:
        assert not any(event.get("type") == "touka_stream_terminal" for event in events)
        assert "data: [DONE]\n\n" in chunks
        assert (
            "synthetic-private-upstream-error" if failure == "round_error" else "已由使用者中斷"
        ) in "".join(chunks)
    save.assert_not_awaited()
    trim.assert_not_awaited()
    adapter.close.assert_awaited_once()
    if ingress == "converted":
        converted_request = adapter.prepare_request.await_args.args[0]
        assert (
            isinstance(converted_request, OpenAIChatRequest)
            and converted_request.ui_persistence is None
        )
