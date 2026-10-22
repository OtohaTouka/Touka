"""r55：用途標頭 ``X-Touka-Purpose`` 只能讓 Claude 少想（T4；禁令 16 精神）。

標頭由客戶端控制。``routers/proxy.py`` 只認白名單值（``inline-completion``），換成 context
專用鍵 ``thinking_profile``；其他值（偽造的用途名、變形、超長、非 ASCII）一律忽略、不報錯。
這個鍵只有 ClaudeAdapter 讀，不能影響路由、工具、權限、模式、身分、花費上限：

- 換鍵單元：白名單與偽造值；
- 端到端：同一請求有無標頭跑真的 pipeline，交給 dispatch 的 model／request／identity／context
  只差 headers 與 thinking_profile；
- 主對話帶標頭且這一輪觸發審查員：審查員那次 ``prepare_request`` 的 context 沒有這個鍵，
  審查員送出的內容一個 byte 都不變；
- 沒登入只帶標頭：兩個聊天端點（含根路徑）都 401、不進 pipeline；Depends 一行沒改；
- 靜態：鍵名與標頭名只出現在定義、寫入、讀取的那幾個檔。

全部離線：上游與審查員的 SDK 都是假的，不打 API（禁令 61）。
"""

from __future__ import annotations

import ast
import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from src.adapters.schemas import OpenAIChatRequest
from src.handlers import react_core
from src.handlers.adapters.base import THINKING_PROFILE_KEY
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.main import app
from src.models.advertised_tools import AdvertisedTools
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.safety import AuthPoliciesConfig
from src.routers import proxy as proxy_router
from src.services import auto_reviewer as reviewer_module
from src.services import completion, identity_router, proxy_service
from src.services import tool_executor as executor_module
from src.services.approval_service import approval_service
from src.services.auth_service import AuthService
from src.services.authorization_gateway import require_authenticated
from src.services.auto_reviewer import AutoReviewer
from src.services.completion import complete_once
from src.services.identity_router import get_verified_identity
from src.services.models.manager import ModelManager
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution
from src.services.permission_service import permission_service
from src.services.review.budget import _reset_review_ledger
from src.utils.user_intent import chat_intent
from starlette.requests import Request

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "ToukaProxy" / "src"
_HEADER = "X-Touka-Purpose"
_CHAT_PATHS = ["/v1/chat/completions", "/v1/messages", "/chat/completions", "/messages"]
_MODEL = "claude-haiku-5-5"


# ── 1. 換鍵：白名單 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [
    "inline-completion", "Inline-Completion", "INLINE-COMPLETION", " inline-completion\t",
    "Inline-Completion ",
])
def test_whitelisted_value_maps_to_minimal(value):
    assert proxy_router._client_thinking_profile({"x-touka-purpose": value}) == "minimal"


@pytest.mark.parametrize("value", [
    "judge", "summarizer", "minimal", "auto_reviewer", "critic", "inline_completion",
    "inline completion", "inline-completion-x", "xinline-completion", "inline-completion\x00",
    "inline-completion,inline-completion", pytest.param("x" * 100_000, id="huge"),
    "ìnline-completion",
    "\xa0inline-completion", "inline-completion　", "ｉｎｌｉｎｅ-completion", "", "   ",
])
def test_forged_or_unknown_values_are_ignored(value):
    assert proxy_router._client_thinking_profile({"x-touka-purpose": value}) is None


def test_missing_header_is_ignored():
    assert proxy_router._client_thinking_profile({}) is None
    assert proxy_router._client_thinking_profile({"x-other": "inline-completion"}) is None


# ── 2. 端到端：真 pipeline，dispatch 換成記錄器 ──────────────────────────────


def _payload(path: str) -> dict[str, Any]:
    body: dict[str, Any] = {"model": _MODEL, "messages": [{"role": "user", "content": "hi"}]}
    if path.endswith("/messages"):
        body["max_tokens"] = 64
    else:
        body["stop"] = ["\n\n"]
        body["max_tokens"] = 128
    return body


def _post_through_pipeline(path: str, identity: AgentIdentity,
                           header: str | None) -> tuple[Any, AsyncMock]:
    dispatch = AsyncMock(return_value={})
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(proxy_service, "dispatch_request", dispatch)
        app.dependency_overrides[get_verified_identity] = lambda: identity
        headers = {_HEADER: header} if header is not None else {}
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(path, json=_payload(path), headers=headers)
    return resp, dispatch


def _ctx_items(ctx: PipelineContext) -> dict[str, Any]:
    keys = [*type(ctx).model_fields, *(ctx.model_extra or {})]
    return {key: ctx[key] for key in keys}


_VOLATILE = {"request_id", "t0"}  # 每個請求都不同
_EXPECTED_DIFF = {"headers", THINKING_PROFILE_KEY}


def _identity() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")


@pytest.mark.parametrize("path", _CHAT_PATHS)
def test_header_changes_nothing_but_the_thinking_profile(path):
    identity = _identity()
    plain_resp, plain = _post_through_pipeline(path, identity, None)
    marked_resp, marked = _post_through_pipeline(path, identity, "inline-completion")
    assert (plain_resp.status_code, marked_resp.status_code) == (200, 200), marked_resp.text

    p_model, p_request, p_identity, _rid, _t0, p_ctx = plain.await_args.args
    m_model, m_request, m_identity, _rid, _t0, m_ctx = marked.await_args.args
    assert m_model == p_model
    # pipeline 會塞 dict 訊息進去：比對內容即可，不要序列化型別警告
    assert m_request.model_dump(warnings=False) == p_request.model_dump(warnings=False)
    assert m_identity == p_identity
    assert (m_identity.agent_id, m_identity.display_role, m_identity.capabilities) == (
        p_identity.agent_id, p_identity.display_role, p_identity.capabilities)

    p_items, m_items = _ctx_items(p_ctx), _ctx_items(m_ctx)
    changed = {key for key in p_items.keys() | m_items.keys()
               if key not in _VOLATILE and p_items.get(key) != m_items.get(key)}
    assert changed == _EXPECTED_DIFF
    assert (p_ctx[THINKING_PROFILE_KEY], m_ctx[THINKING_PROFILE_KEY]) == (None, "minimal")
    for key in ("model", "agent_id", "role", "capabilities", "identity", "auth_level",
                "desired_tools", "advertised_tools", "denied_tools", "tools_filtered",
                "session_id", "incognito", "is_toukasoul", "is_custom"):
        assert m_ctx.get(key) == p_ctx.get(key), key


@pytest.mark.parametrize("value", ["judge", "minimal", "summarizer",
                                   pytest.param("x" * 5000, id="huge")])
def test_forged_header_through_pipeline_sets_no_profile(value):
    resp, dispatch = _post_through_pipeline("/v1/chat/completions", _identity(), value)
    assert resp.status_code == 200, resp.text
    assert dispatch.await_args.args[5][THINKING_PROFILE_KEY] is None


# ── 3. 主對話帶標頭、這一輪觸發審查員：審查員照舊 ─────────────────────────────

_SOUL = AgentIdentity.for_role(agent_id="R55ReviewProbe", role="Owner", is_human=False)
_POLICY = AuthPoliciesConfig.model_validate(json.loads(
    (_ROOT / "ToukaProxy" / "config" / "auth_policies.json.example").read_text(encoding="utf-8")))
_REVIEWER_CONFIG = {"model": _MODEL, "max_tokens": 300, "timeout_s": 7.0}
_ALLOW = '{"verdict": "allow", "reason": "the user asked for it"}'


class _Block(SimpleNamespace):
    def model_dump(self) -> dict:
        return dict(vars(self))


class _Message(SimpleNamespace):
    def model_dump(self) -> dict:
        return {"id": "msg_r55", "model": _MODEL, "role": "assistant",
                "stop_reason": self.stop_reason,
                "content": [block.model_dump() for block in self.content]}


def _tool_turn() -> _Message:
    call = _Block(type="tool_use", id="toolu_1", name="execute_command",
                  input={"command": "pytest -q"})
    return _Message(content=[call], stop_reason="tool_use", usage=None)


def _text_turn(text: str) -> _Message:
    return _Message(content=[_Block(type="text", text=text)], stop_reason="end_turn", usage=None)


class _MainClient:
    """主迴圈的假 Anthropic client（非串流 create）。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._script = [_tool_turn(), _text_turn("done")]
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw: Any) -> Any:
        self.sent.append(copy.deepcopy(kw))
        return self._script.pop(0)


class _ReviewerAnthropic:
    """complete_once 自己建的假 client：審查員回 allow。"""

    sent: list[dict] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw: Any) -> Any:
        _ReviewerAnthropic.sent.append(copy.deepcopy(kw))
        usage = {"input_tokens": 10, "output_tokens": 5}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=_ALLOW)],
                               stop_reason="end_turn",
                               usage=SimpleNamespace(model_dump=lambda: dict(usage)))

    async def close(self) -> None:
        return None


class _Budget:
    def is_over_budget(self, _identity: AgentIdentity) -> bool:
        return False

    def would_exceed(self, _identity: AgentIdentity, _worst_usd: float) -> bool:
        return False


def _http_request(header: str | None) -> Request:
    headers = [(b"content-type", b"application/json")]
    if header is not None:
        headers.append((b"x-touka-purpose", header.encode("latin-1")))
    return Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                    "headers": headers, "query_string": b""})


@pytest.fixture
def review_rig(monkeypatch):
    """真的 ToolExecutor（Auto）＋真的審查員＋真的 complete_once；兩邊的 SDK 都是假的。"""
    approval_service.reset_for_test()
    _reset_review_ledger()
    _ReviewerAnthropic.sent = []
    seen: list[dict[str, Any]] = []
    original = ClaudeAdapter.prepare_request

    async def _spy(self, request, context):
        seen.append({"pipeline": isinstance(context, PipelineContext),
                     "has_key": THINKING_PROFILE_KEY in context,
                     "profile": context.get(THINKING_PROFILE_KEY)})
        return await original(self, request, context)

    ran: list[dict] = []

    async def _execute(**kwargs: Any) -> str:
        ran.append(kwargs)
        return json.dumps({"ok": True})

    bindings = dict(executor_module.BUILTIN_EXECUTION_BINDINGS)
    bindings["execute_command"] = replace(bindings["execute_command"], handler=_execute)
    monkeypatch.setattr(executor_module, "BUILTIN_EXECUTION_BINDINGS", bindings)
    monkeypatch.setattr(permission_service, "evaluate_policy", _POLICY.evaluate)
    reviewer = AutoReviewer(complete_once, config_loader=lambda: _REVIEWER_CONFIG)
    monkeypatch.setattr(executor_module, "auto_reviewer", reviewer)
    monkeypatch.setattr(reviewer_module.audit_service, "log_decision", lambda *_a: None)
    monkeypatch.setattr(ClaudeAdapter, "prepare_request", _spy)
    monkeypatch.setattr(completion, "AsyncAnthropic", _ReviewerAnthropic)
    monkeypatch.setattr(completion, "autonomy_service", _Budget())
    monkeypatch.setattr(completion, "accumulate_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(completion, "log_usage_audit", lambda *a, **k: None)
    monkeypatch.setattr(ModelManager, "get_models", lambda _self: [])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN))
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-anthropic", raising=False)
    rhythm = react_core.rhythm_service
    monkeypatch.setattr(rhythm, "initialize_session", Mock())
    monkeypatch.setattr(rhythm, "get_effective_rounds", Mock(return_value=0))
    monkeypatch.setattr(rhythm, "consume_round", Mock(return_value=True))
    monkeypatch.setattr(rhythm, "get_total_rounds", Mock(return_value=0))
    monkeypatch.setattr(react_core.autonomy_service, "is_over_budget", Mock(return_value=False))
    yield SimpleNamespace(seen=seen, ran=ran, execute=_execute)
    approval_service.reset_for_test()
    _reset_review_ledger()


async def _main_turn(rig: Any, header: str | None) -> _MainClient:
    """照聊天端點組 context（真的 _apply_chat_request_context），跑一輪會送審的主迴圈。"""
    request = OpenAIChatRequest(model=_MODEL, messages=[{"role": "user", "content": "跑測試"}],
                                tools=[{"type": "function", "function": {
                                    "name": "execute_command", "description": "run",
                                    "parameters": {"type": "object", "properties": {
                                        "command": {"type": "string"}}}}}])
    ctx = PipelineContext(request_id="req-r55-review", t0=0.0)
    proxy_router._apply_chat_request_context(ctx, request, _http_request(header), _SOUL)
    client = _MainClient()
    ctx.update({
        "auth_level": "Auto", "is_toukasoul": True, "anthropic_client": client,
        "tool_registry": {"execute_command": rig.execute}, "memory_tool_registry": {},
        "plugin_registry": {},
        "advertised_tools": AdvertisedTools(native=frozenset({"execute_command"})),
        "user_intent": chat_intent([{"role": "user", "content": "跑測試"}]),
    })
    async with asyncio.timeout(10):
        _chunks = [chunk async for chunk in react_core.run_react_loop(
            ClaudeAdapter(), request, ctx)]
    return client


async def test_reviewer_never_inherits_the_main_turn_profile(review_rig):
    main = await _main_turn(review_rig, "inline-completion")

    assert len(review_rig.ran) == 1  # 審查員放行、工具跑了一次
    assert review_rig.ran[0]["_approved_by"] == "auto_reviewer"
    (main_ctx, reviewer_ctx) = review_rig.seen
    assert main_ctx == {"pipeline": True, "has_key": True, "profile": "minimal"}
    assert reviewer_ctx == {"pipeline": False, "has_key": False, "profile": None}
    # 主迴圈少想；審查員照舊（模型預設思考、8,192 下限、只有 schema）
    assert (main.sent[0]["thinking"], main.sent[0]["output_config"]) == (
        {"type": "disabled"}, {"effort": "low"})
    (review,) = _ReviewerAnthropic.sent
    assert "thinking" not in review
    assert set(review["output_config"]) == {"format"}
    assert review["max_tokens"] == 8192


async def test_reviewer_wire_is_byte_identical_with_or_without_the_header(review_rig):
    await _main_turn(review_rig, None)
    without = list(_ReviewerAnthropic.sent)
    _ReviewerAnthropic.sent = []
    await _main_turn(review_rig, "inline-completion")
    assert _ReviewerAnthropic.sent == without
    assert json.dumps(_ReviewerAnthropic.sent, sort_keys=True, ensure_ascii=False) == json.dumps(
        without, sort_keys=True, ensure_ascii=False)


# ── 4. 沒登入只帶標頭：401、不進 pipeline；Depends 不變 ─────────────────────────


@pytest.mark.parametrize("origin", ["external", "local_after_claim"])
@pytest.mark.parametrize("path", _CHAT_PATHS)
def test_header_without_credentials_is_401_before_pipeline(path, origin, monkeypatch):
    if origin == "external":
        monkeypatch.setattr(identity_router, "is_request_from_trusted_local", lambda _r: False)
    else:
        monkeypatch.setattr(AuthService, "any_claimed_user", lambda _self: True)
    entered = AsyncMock(return_value={})
    monkeypatch.setattr(proxy_router, "process_proxy_request", entered)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(path, json=_payload(path), headers={_HEADER: "inline-completion"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"
    entered.assert_not_called()


def _identity_dependency(handler: str) -> str:
    tree = ast.parse((_SRC / "routers" / "proxy.py").read_text(encoding="utf-8"))
    (func,) = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == handler]
    (arg,) = [a for a in func.args.args if a.arg == "identity"]
    return ast.unparse(arg.annotation)


@pytest.mark.parametrize("handler", ["proxy_anthropic_messages", "proxy_chat_completions"])
def test_chat_endpoints_keep_require_authenticated(handler):
    assert _identity_dependency(handler) == (
        "Annotated[AgentIdentity, Depends(_require_authenticated)]")
    assert proxy_router._require_authenticated is require_authenticated


# ── 5. 靜態：誰寫、誰讀 ───────────────────────────────────────────────────────


def _src_files_containing(needle: str) -> set[str]:
    return {p.relative_to(_SRC).as_posix() for p in _SRC.rglob("*.py")
            if needle in p.read_text(encoding="utf-8")}


def test_profile_key_only_in_definition_writers_and_reader():
    assert _src_files_containing("THINKING_PROFILE_KEY") == {
        "handlers/adapters/base.py",  # 定義
        "routers/proxy.py", "services/completion.py",  # 寫
        "handlers/adapters/claude_adapter.py",  # 讀
    }
    assert _src_files_containing('"thinking_profile"') == {"handlers/adapters/base.py"}


def test_purpose_header_name_only_in_the_router():
    assert _src_files_containing("x-touka-purpose") == {"routers/proxy.py"}
    assert {p for p in _src_files_containing("X-Touka-Purpose") if p != "routers/proxy.py"} == set()
