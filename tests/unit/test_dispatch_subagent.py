"""Phase 1 MVP（in-process 子代理協調提案 §9）：dispatch_subagent——TDD.

主 agent 在 ReAct loop 內以工具派出「一個」in-process 子代理：
- 立即回結構化成功（含 callback_token），背景開子 loop；
- 子 loop 完成後 ``deliver_result`` **in-process 直呼**（繞過 HTTP 回呼），
  喚醒既有 ``mission_tools(action="await_dispatch_result")``——await 工具零改動重用；
- 臨時身分 ``__subagent_`` 前綴＋Subagent 角色（ADR-0245）＋衍生 request_id＋無 session_id；
  非落盤 brain（全路徑不呼 ``record_working_brain``）；
- 遞迴根絕：組裝後的子代理 tools 剝除 ``dispatch_subagent``，且 Subagent 權限本就不含
  （Gate 1 不廣告＋Gate 2 拒絕，雙閘零新機制）；
- 失敗也必 deliver（FAILED），等待者不會掛到 TIMEOUT 才知道。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from src.models.identity import AgentIdentity
from src.services import tool_errors
from src.services.dispatch_callback_store import (
    _reset_callback_store,
    get_callback_store,
)
from src.services.permission_service import PermissionService
from src.services.subagent_report import REPORT_INSTRUCTION
from src.services.tool_executor import ToolExecutor
from src.tools import subagent_tools
from src.tools.builtin import builtin_tools_schema, tool_registry
from src.tools.mission_tools import mission_tools
from src.utils.time_utils import get_current_epoch

# --------------------------------------------------------------------------- #
# 輔助
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _fresh_callback_store():
    """每測隔離 callback store singleton。"""
    _reset_callback_store()
    yield
    _reset_callback_store()


def _debug_identity() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")


def _fake_stream(*texts: str):
    """回傳一個仿 factory 的 async 函式：drain 後得到 texts（Anthropic SSE 形）。"""

    captured: dict[str, Any] = {}

    async def _factory(
        model_name: str,
        request: Any,
        identity: AgentIdentity,
        request_id: str,
        start_time: float,
        context: dict,
        session_id: str | None = None,
    ):
        captured.update(
            model=model_name,
            request=request,
            identity=identity,
            request_id=request_id,
            session_id=session_id,
        )

        async def _gen():
            for t in texts:
                payload = {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": t},
                }
                yield f"data: {json.dumps(payload)}\n\n"
            yield "data: [DONE]\n\n"

        return _gen()

    return _factory, captured


class _FakeRequest:
    """最小子代理請求容器（_assemble 種子用）。"""

    def __init__(self) -> None:
        self.tools: list[dict] | None = None
        self.model = "fake-brain"
        self.system = ""
        self.messages: list[dict] = []


# --------------------------------------------------------------------------- #
# 1. 註冊／廣告／權限（遞迴閘）
# --------------------------------------------------------------------------- #


def test_dispatch_subagent_registered_in_tool_registry():
    assert "dispatch_subagent" in tool_registry


def test_dispatch_subagent_schema_advertised():
    names = {
        s.get("function", {}).get("name")
        for s in builtin_tools_schema
        if isinstance(s, dict)
    }
    assert "dispatch_subagent" in names


def test_subagent_role_excludes_dispatch_subagent():
    """遞迴根絕的權限面：子代理跑 Subagent 角色，Subagent 不得擁有 dispatch_subagent。"""
    service = PermissionService()
    sub = AgentIdentity.for_role(agent_id="__subagent_test__", role="Subagent")
    allowed = service.get_allowed_tools(sub)
    assert allowed.open_ended is False
    assert allowed.allows("dispatch_subagent") is False


# --------------------------------------------------------------------------- #
# 2. 立即回傳契約：結構化成功＋token 已註冊
# --------------------------------------------------------------------------- #


async def test_dispatch_returns_structured_success_with_registered_token(monkeypatch):
    async def _fake_run(**_kw: Any) -> str:
        return "SUB DONE"

    monkeypatch.setattr(subagent_tools, "_run_subagent", _fake_run)

    res = await subagent_tools.dispatch_subagent(
        goal="整理一份摘要",
        identity=_debug_identity(),
        request_id="req-1",
        _ctx={},
    )
    assert tool_errors.is_tool_success(res)
    payload = json.loads(res)
    token = payload.get("callback_token")
    assert token
    # deliver 尚未（或剛）發生都可，但 token 必須經過 register
    # （wait_for_result 對未註冊 token 會回 UNKNOWN_TOKEN）
    result = await get_callback_store().wait_for_result(token, timeout=2)
    assert result.get("status") != "UNKNOWN_TOKEN"


async def test_result_delivered_in_process_wakes_awaiter(monkeypatch):
    """核心黏合線：子 loop 完成 → deliver_result 直呼 → 既有 await 工具收到結果。"""

    async def _fake_run(**_kw: Any) -> str:
        return "SUB DONE: 摘要完成"

    monkeypatch.setattr(subagent_tools, "_run_subagent", _fake_run)

    res = await subagent_tools.dispatch_subagent(
        goal="整理一份摘要",
        identity=_debug_identity(),
        request_id="req-1",
        _ctx={},
    )
    token = json.loads(res)["callback_token"]

    out = await mission_tools(
        action="await_dispatch_result",
        callback_token=token,
        timeout=5,
        identity=_debug_identity(),
    )
    assert "SUB DONE" in str(out)


async def test_failure_delivers_failed_not_hang(monkeypatch):
    """子 loop 崩潰也必 deliver（FAILED），等待者不會空等到 TIMEOUT。"""

    async def _boom(**_kw: Any) -> str:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(subagent_tools, "_run_subagent", _boom)

    res = await subagent_tools.dispatch_subagent(
        goal="會爆炸的任務",
        identity=_debug_identity(),
        request_id="req-1",
        _ctx={},
    )
    token = json.loads(res)["callback_token"]
    result = await get_callback_store().wait_for_result(token, timeout=2)
    assert result.get("status") == "FAILED"
    assert "kaboom" in str(result.get("result_summary", ""))


# --------------------------------------------------------------------------- #
# 3. 子 loop 隔離契約：臨時 Subagent 身分／衍生 request_id／無 session／不落盤 brain
# --------------------------------------------------------------------------- #


async def test_subagent_isolation_contract(monkeypatch):
    factory, captured = _fake_stream("hello ", "world")
    monkeypatch.setattr(subagent_tools, "get_stream_generator", factory)

    fake_req = _FakeRequest()

    async def _fake_assemble(**_kw: Any):
        return fake_req

    monkeypatch.setattr(subagent_tools, "_assemble_subagent_request", _fake_assemble)

    brain_spy = MagicMock()
    monkeypatch.setattr(
        "src.utils.brain_persistence.record_working_brain", brain_spy
    )

    text = await subagent_tools._run_subagent(
        goal="測試隔離",
        parent_identity=_debug_identity(),
        parent_request_id="req-9",
        base_ctx={},
        model=None,
    )

    assert text == "hello world"
    sub_identity = captured["identity"]
    assert sub_identity.agent_id.startswith("__subagent_")
    assert sub_identity.agent_role == "Subagent"
    assert captured["request_id"].startswith("req-9:sub")
    assert captured["session_id"] is None
    brain_spy.assert_not_called()


# --------------------------------------------------------------------------- #
# G08（守衛涵蓋缺口）：headless 旗標（enable_autonomy）沿父鏈繼承（ADR-0146）。
# 既有隔離測試以 base_ctx={} 呼叫＝bool({}.get()) 與硬編 False 不可區分；本組以
# enable_autonomy=True 的 base_ctx 驅動、捕捉傳入 get_stream_generator 的 context，
# 斷言繼承。回歸（subagent_tools.py:193 改回硬編 False）→ heartbeat 子代理工具輪失控。
# --------------------------------------------------------------------------- #


def _capture_ctx_stream():
    """仿 factory：捕捉傳入的 context（PipelineContext），drain 立即 [DONE]。"""
    captured: dict[str, Any] = {}

    async def _factory(
        model_name: str,
        request: Any,
        identity: AgentIdentity,
        request_id: str,
        start_time: float,
        context: Any,
        session_id: str | None = None,
    ):
        captured["context"] = context

        async def _gen():
            yield "data: [DONE]\n\n"

        return _gen()

    return _factory, captured


async def _run_with_base_ctx(monkeypatch, base_ctx: dict, rid: str) -> Any:
    factory, captured = _capture_ctx_stream()
    monkeypatch.setattr(subagent_tools, "get_stream_generator", factory)

    async def _fake_assemble(**_kw: Any):
        return _FakeRequest()

    monkeypatch.setattr(subagent_tools, "_assemble_subagent_request", _fake_assemble)
    monkeypatch.setattr("src.utils.brain_persistence.record_working_brain", MagicMock())

    await subagent_tools._run_subagent(
        goal="g",
        parent_identity=_debug_identity(),
        parent_request_id=rid,
        base_ctx=base_ctx,
        model=None,
    )
    return captured["context"]


async def test_subagent_inherits_enable_autonomy_true_from_parent(monkeypatch):
    """heartbeat 派生（base_ctx enable_autonomy=True）→ 子 ctx 繼承 True（有限工具輪保險絲）。"""
    ctx = await _run_with_base_ctx(monkeypatch, {"enable_autonomy": True}, "req-h")
    assert ctx["enable_autonomy"] is True, (
        "子代理未繼承 enable_autonomy＝硬編 False 回歸→heartbeat 子代理工具輪失控"
    )


async def test_subagent_enable_autonomy_false_when_parent_absent(monkeypatch):
    """互動派生（base_ctx 無 enable_autonomy）→ 子 ctx False（維持互動語意）。"""
    ctx = await _run_with_base_ctx(monkeypatch, {}, "req-i")
    assert ctx["enable_autonomy"] is False


async def test_subagent_ctx_is_marked_so_review_never_asks(monkeypatch):
    """B2：子 loop 吞審批卡——互動派生的子 ctx 也帶 subagent_run，request_review 不問人。"""
    ctx = await _run_with_base_ctx(monkeypatch, {"resumable_stream": True}, "req-sub")
    assert ctx["subagent_run"] is True


async def test_assembled_tools_exclude_dispatch_subagent(monkeypatch):
    """遞迴根絕的能力面：組裝後的子代理 tools 必剝除 dispatch_subagent。"""

    class _FakePipeline:
        async def execute(self, request: Any, ctx: Any) -> None:
            request.tools = [
                {"function": {"name": "dispatch_subagent"}},
                {"function": {"name": "read"}},
            ]

    monkeypatch.setattr("src.services.proxy_service.pipeline", _FakePipeline())

    request = await subagent_tools._assemble_subagent_request(
        goal="測試遞迴剝除",
        sub_identity=AgentIdentity.for_role(agent_id="__subagent_x__", role="Subagent"),
        base_ctx={},
        model="fake-brain",
    )
    names = {t.get("function", {}).get("name") for t in (request.tools or [])}
    assert "dispatch_subagent" not in names
    assert "read" in names


# --------------------------------------------------------------------------- #
# 4. 參數／接線防呆
# --------------------------------------------------------------------------- #


async def test_missing_goal_returns_validation_error():
    res = await subagent_tools.dispatch_subagent(
        goal="   ",
        identity=_debug_identity(),
        request_id="req-1",
        _ctx={},
    )
    assert tool_errors.extract_error_class(res) == tool_errors.VALIDATION_ERROR


async def test_missing_ctx_returns_internal_error():
    res = await subagent_tools.dispatch_subagent(
        goal="正常目標",
        identity=_debug_identity(),
        request_id="req-1",
    )
    assert tool_errors.extract_error_class(res) == tool_errors.INTERNAL_ERROR


# --------------------------------------------------------------------------- #
# 5. Phase 2：結構化回報接線
# --------------------------------------------------------------------------- #


def test_prompt_includes_report_instruction():
    """子代理任務指示需內含結構化回報格式要求（尾部 json 塊）。"""
    prompt = subagent_tools._subagent_user_prompt("整理摘要")
    assert REPORT_INSTRUCTION in prompt


async def test_deliver_payload_carries_structured_report(monkeypatch):
    """子代理輸出含回報塊 → deliver payload 載結構化欄位（需求 4 程式化裁決）。"""

    async def _fake_run(**_kw: Any) -> str:
        return (
            "摘要完成，已寫入 out.md。\n\n"
            "```json\n"
            '{"status": "done", "claims": ["完成 A"],'
            ' "artifact_refs": ["out.md"], "confidence": 0.9}\n'
            "```\n"
        )

    monkeypatch.setattr(subagent_tools, "_run_subagent", _fake_run)

    res = await subagent_tools.dispatch_subagent(
        goal="整理摘要",
        identity=_debug_identity(),
        request_id="req-1",
        _ctx={},
    )
    token = json.loads(res)["callback_token"]
    result = await get_callback_store().wait_for_result(token, timeout=2)

    assert result.get("status") == "DONE"  # 傳輸層：子 loop 是否完成
    assert result.get("report_status") == "done"  # 自我宣稱層：模型說做完了
    assert result.get("claims") == ["完成 A"]
    assert result.get("artifact_refs") == ["out.md"]
    assert result.get("confidence") == 0.9
    assert result.get("structured") is True
    assert "摘要完成" in str(result.get("result_summary", ""))


async def test_deliver_payload_raw_fallback(monkeypatch):
    """子代理不遵守回報格式 → 誠實 fallback（unverified、structured=False）。"""

    async def _fake_run(**_kw: Any) -> str:
        return "我做完了，但我不會輸出 JSON。"

    monkeypatch.setattr(subagent_tools, "_run_subagent", _fake_run)

    res = await subagent_tools.dispatch_subagent(
        goal="整理摘要",
        identity=_debug_identity(),
        request_id="req-1",
        _ctx={},
    )
    token = json.loads(res)["callback_token"]
    result = await get_callback_store().wait_for_result(token, timeout=2)

    assert result.get("report_status") == "unverified"
    assert result.get("structured") is False
    assert "不會輸出 JSON" in str(result.get("result_summary", ""))


async def test_await_renders_structured_report():
    """await_dispatch_result 需把結構化回報渲染給主 agent（非只 result_summary）。"""
    store = get_callback_store()
    token = store.generate_token()
    store.register(token)
    store.deliver_result(
        token,
        {
            "task_id": "t9",
            "status": "DONE",
            "result_summary": "已完成摘要",
            "report_status": "done",
            "claims": ["完成 A", "完成 B"],
            "artifact_refs": ["out.md"],
            "confidence": 0.9,
            "structured": True,
        },
    )
    out = str(
        await mission_tools(
            action="await_dispatch_result",
            callback_token=token,
            timeout=2,
            identity=_debug_identity(),
        )
    )
    assert "完成 A" in out
    assert "out.md" in out
    assert "done" in out


# --------------------------------------------------------------------------- #
# 6. 參數／接線防呆（executor）
# --------------------------------------------------------------------------- #


async def test_executor_injects_ctx_into_call_kwargs():
    """executor 接線：call_kwargs 需含 _ctx（工具以 **kwargs 取用、不需要者忽略）。"""
    seen: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    executor = ToolExecutor(None, {"capture": _capture}, {}, {}, enforce_advertised=False)
    ctx = {"identity": _debug_identity(), "auth_level": "Manual"}
    queue: asyncio.Queue = asyncio.Queue()
    with patch("src.services.tool_executor.permission_service") as perm:
        perm.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
        await executor.execute_to_queue("capture", {"a": 1}, "req-exec", ctx, queue)

    assert "_ctx" in seen
    assert seen["_ctx"] is ctx


# --------------------------------------------------------------------------- #
# 批⑤-D E74（ADR-0134）：子 loop 600s 硬上限——wait_for 真的包住 drain
# 既有測試 fake stream 立即 [DONE]／monkeypatch _run_subagent＝逾時包裹從不真跑；
# 唯一直呼 _drain_text 者繞過 wait_for。刪逾時＝背景 Task 洩漏至進程重啟。
# --------------------------------------------------------------------------- #


def _never_done_factory():
    """仿 factory：stream 吐一筆 chunk 後永不完成（模擬卡死的子 loop）。"""

    async def _factory(
        model_name: str,
        request: Any,
        identity: AgentIdentity,
        request_id: str,
        start_time: float,
        context: Any,
        session_id: str | None = None,
    ):
        async def _gen():
            yield 'data: {"type": "ping"}\n\n'
            await asyncio.sleep(3600)  # 永不 [DONE]

        return _gen()

    return _factory


def _patch_hanging_subagent(monkeypatch) -> None:
    """共用佈局：永不完成 stream＋假組裝＋不落盤 brain＋極小硬上限。"""
    monkeypatch.setattr(subagent_tools, "get_stream_generator", _never_done_factory())

    async def _fake_assemble(**_kw: Any):
        return _FakeRequest()

    monkeypatch.setattr(subagent_tools, "_assemble_subagent_request", _fake_assemble)
    monkeypatch.setattr("src.utils.brain_persistence.record_working_brain", MagicMock())
    monkeypatch.setattr(subagent_tools, "_SUB_TIMEOUT_SECONDS", 0.05)


async def test_run_subagent_enforces_hard_timeout(monkeypatch):
    """永不完成的 stream＋極小 _SUB_TIMEOUT → _run_subagent 迅速拋 TimeoutError。

    外層 wait_for(3s) 是**測試護欄**（防變異後永掛），時長斷言負責判別：生產逾時
    0.05s 內就炸；變異 :206 拆掉 asyncio.wait_for → 只剩護欄 3s 才炸 → 時長紅。
    """
    _patch_hanging_subagent(monkeypatch)
    t0 = get_current_epoch()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            subagent_tools._run_subagent(
                goal="會卡死的任務",
                parent_identity=_debug_identity(),
                parent_request_id="req-t74",
                base_ctx={},
                model=None,
            ),
            timeout=3,
        )
    elapsed = get_current_epoch() - t0
    assert elapsed < 2, f"逾時來自測試護欄而非生產 _SUB_TIMEOUT（{elapsed:.2f}s）"


async def test_subagent_timeout_delivers_failed_via_dispatch(monkeypatch):
    """逾時經完整派發鏈 → deliver FAILED（等待者不空等、與 600s 硬上限同一條保險絲）。"""
    _patch_hanging_subagent(monkeypatch)
    res = await subagent_tools.dispatch_subagent(
        goal="會逾時的任務",
        identity=_debug_identity(),
        request_id="req-t74b",
        _ctx={},
    )
    token = json.loads(res)["callback_token"]
    result = await get_callback_store().wait_for_result(token, timeout=5)
    assert result.get("status") == "FAILED"
    assert "TimeoutError" in str(result.get("result_summary", ""))
