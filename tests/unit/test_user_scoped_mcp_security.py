"""T1：使用者各自授權（``oauth_user``）的 MCP 只在人在場、和靈魂的前景對話時帶身分（ADR-0281）。

- 條件是一個純函式：已驗證的靈魂身分、人在場、不是群聊、有原始人類。
- 組清單時只有 ``oauth_user`` 的伺服器拿到身分；共用伺服器照舊不帶身分、不會因此連線。
- 先做只讀的授權查詢，沒授權不連線；連線逾時只略過這一輪、不取消背景連線；一台出錯只略過那一台。
- 技能或自訂模型需要這類伺服器時，請求不再失敗。
- 沒連上的共用伺服器寫進每輪會變動的說明區塊（決定 1 甲），不列成工具。
- 執行器再用同一個條件擋一次（清單核對之後、審查員與審批卡之前）。

全部走 conftest 沙盒與 mock：不連外、不讀 data/。
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.middleware.layers.tool import ToolLayer
from src.models.advertised_tools import AdvertisedTools
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.protocols import manager as manager_module
from src.protocols.manager import ProtocolError, ProtocolManager
from src.protocols.mcp.canonical import McpRequestOrigin
from src.protocols.mcp.handles import LegacyMcpHandle
from src.protocols.mcp.oauth.store import OAuthStoreCorruptionError
from src.protocols.mcp.sdk_adapter import adapt_call_tool_result
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.services import approval_flow
from src.services.auto_reviewer import auto_reviewer
from src.services.dispatchers import group
from src.services.guard_service import guard_service
from src.services.isolation.consent import consent_ledger
from src.services.permission_service import permission_service
from src.services.prompt_blocks import MCP_UNAVAILABLE, VOLATILE_SYSTEM_TAGS
from src.services.tool_executor import ToolExecutor
from src.utils.mcp_request_identity import (
    GROUP_MEMBER_FLAG,
    GROUP_MODEL,
    mcp_request_context,
    user_scoped_mcp_allowed,
)

_SOUL = AgentIdentity.for_role(agent_id="ScopeSoul", role="Owner")
_HUMAN = AgentIdentity.for_role(agent_id="human-a", role="admin", is_human=True)


def _foreground(**extra: Any) -> PipelineContext:
    """人在場、和靈魂的前景對話（前景代打後：身分是靈魂，原始人類另記）。"""
    ctx = PipelineContext(identity=_SOUL, agent_id=_SOUL.agent_id)
    ctx["headers"] = {}
    ctx["is_toukasoul"] = True
    ctx["resumable_stream"] = True
    ctx["human_principal_id"] = "human-a"
    for key, value in extra.items():
        ctx[key] = value
    return ctx


# --- 1. 條件函式 ----------------------------------------------------------------


def test_foreground_soul_with_a_human_present_is_allowed():
    assert user_scoped_mcp_allowed(_foreground()) is True


@pytest.mark.parametrize(
    "change",
    [
        {"heartbeat_run": True},
        {"subagent_run": True},
        {"dispatch_run": True},
        {"resumable_stream": False},  # SNS、情境快照、計分卡：沒有互動串流
        {"is_toukasoul": False},  # 直接和一般模型聊天
        {GROUP_MEMBER_FLAG: True},  # 群聊分給成員的執行
        {"human_principal_id": None},
        {"identity": _HUMAN},
        {"identity": "ScopeSoul"},  # 字串不是已驗證的身分
    ],
)
def test_every_other_run_is_refused(change):
    assert user_scoped_mcp_allowed(_foreground(**change)) is False


def test_group_request_is_refused():
    assert user_scoped_mcp_allowed(_foreground(), request_model=GROUP_MODEL) is False


def test_request_context_comes_from_the_verified_ctx():
    context = mcp_request_context(
        _foreground(session_id="s1", auth_level="Auto"),
        _SOUL,
        request_id="r1",
        installation_id="private",
    )
    assert context.identity == _SOUL
    assert context.human_principal_id == "human-a"
    assert context.origin == McpRequestOrigin.FOREGROUND
    assert (context.session_id, context.auth_level, context.installation_id) == (
        "s1",
        "Auto",
        "private",
    )


# --- 2. 組清單（真的 ProtocolManager） -------------------------------------------


class _CatalogSession:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=name, title=None, description=name, input_schema={"type": "object"},
                    output_schema=None, annotations=None, icons=[], meta=None,
                )
                for name in self.names
            ]
        )


class _OAuth:
    """OAuth 執行期的替身：只讀的授權查詢照設定回答，記下問了誰。"""

    def __init__(self, usable: bool = True, error: Exception | None = None) -> None:
        self.usable = usable
        self.error = error
        self.checked: list[tuple[str, str | None, str]] = []

    async def has_usable_grant(self, *, server_name, request_context, installation_generation):
        self.checked.append(
            (server_name, request_context.human_principal_id, request_context.identity.agent_id)
        )
        if self.error is not None:
            raise self.error
        return self.usable

    async def authorize_operation(self, **_kwargs: Any) -> None:
        return None


def _manager(tmp_path, servers: dict[str, ProtocolServerConfig], oauth: Any = None):
    registry = ProtocolRegistry(str(tmp_path / "server_config.json"))
    registry.servers = servers
    manager = ProtocolManager(registry)
    if oauth is not None:
        manager.set_oauth_auth_service(oauth)
    return manager


def _connector(manager: ProtocolManager, catalogs: dict[str, list[str]], calls: list):
    """``_ensure_handle`` 的替身：記下誰被連線、用什麼身分；連上就放進 pool。"""

    async def _ensure(name, request_context):
        calls.append((name, request_context))
        handle = LegacyMcpHandle(session=_CatalogSession(catalogs[name]))
        manager.handle_pool.put(manager.handle_key_for(name, request_context), handle)
        return handle

    return _ensure


def _listing_context() -> Any:
    return mcp_request_context(_foreground(), _SOUL, request_id="r1", installation_id="")


def _servers(tools: list[dict]) -> set[tuple[str, str]]:
    return {(tool["server"], tool.get("tool")) for tool in tools}


async def test_only_user_scoped_servers_get_the_identity(tmp_path, monkeypatch):
    oauth = _OAuth()
    manager = _manager(
        tmp_path,
        {
            "shared": ProtocolServerConfig(command="echo"),
            "private": ProtocolServerConfig(command="echo", auth_mode="oauth_user"),
        },
        oauth,
    )
    calls: list = []
    monkeypatch.setattr(
        manager, "_ensure_handle", _connector(manager, {"private": ["lookup"]}, calls)
    )

    tools = await manager.get_all_tools(request_context=_listing_context())

    assert _servers(tools) == {("private", "lookup")}
    assert [name for name, _context in calls] == ["private"]  # 沒連上的共用伺服器不會被連
    assert calls[0][1].installation_id == "private"
    assert oauth.checked == [("private", "human-a", "ScopeSoul")]


async def test_shared_server_keeps_its_identity_less_cache_partition(tmp_path, monkeypatch):
    manager = _manager(tmp_path, {"shared": ProtocolServerConfig(command="echo")}, _OAuth())
    manager.handle_pool.put(
        manager.handle_key_for("shared", None), LegacyMcpHandle(session=_CatalogSession(["a"]))
    )
    calls: list = []
    monkeypatch.setattr(manager, "_ensure_handle", _connector(manager, {}, calls))

    with_identity = await manager.get_all_tools(request_context=_listing_context())
    without_identity = await manager.get_all_tools()

    assert _servers(with_identity) == _servers(without_identity) == {("shared", "a")}
    assert calls == []


async def test_no_usable_grant_means_no_connection(tmp_path, monkeypatch):
    manager = _manager(
        tmp_path,
        {"private": ProtocolServerConfig(command="echo", auth_mode="oauth_user")},
        _OAuth(usable=False),
    )
    calls: list = []
    monkeypatch.setattr(
        manager, "_ensure_handle", _connector(manager, {"private": ["lookup"]}, calls)
    )

    assert await manager.get_all_tools(request_context=_listing_context()) == []
    assert calls == []


async def test_corrupt_grant_store_skips_the_server_and_logs_an_error(
    tmp_path, monkeypatch, caplog
):
    manager = _manager(
        tmp_path,
        {"private": ProtocolServerConfig(command="echo", auth_mode="oauth_user")},
        _OAuth(error=OAuthStoreCorruptionError()),
    )
    calls: list = []
    monkeypatch.setattr(
        manager, "_ensure_handle", _connector(manager, {"private": ["lookup"]}, calls)
    )

    with caplog.at_level(logging.ERROR):
        assert await manager.get_all_tools(request_context=_listing_context()) == []
    assert calls == []
    assert any(r.levelno >= logging.ERROR and "private" in r.getMessage() for r in caplog.records)


async def test_slow_first_connection_is_skipped_this_turn_and_not_cancelled(
    tmp_path, monkeypatch
):
    manager = _manager(
        tmp_path,
        {"private": ProtocolServerConfig(command="echo", auth_mode="oauth_user")},
        _OAuth(),
    )
    release = asyncio.Event()
    finished: list[str] = []

    async def _slow(name, request_context):
        await release.wait()
        finished.append(name)
        return LegacyMcpHandle(session=_CatalogSession(["lookup"]))

    monkeypatch.setattr(manager, "_ensure_handle", _slow)
    monkeypatch.setattr(manager_module, "USER_SCOPED_CONNECT_TIMEOUT_S", 0.01)

    assert await manager.get_all_tools(request_context=_listing_context()) == []
    release.set()
    for _ in range(20):
        await asyncio.sleep(0)
    assert finished == ["private"]  # 背景連線照樣跑完（ADR-0273：不從呼叫端取消）


class _PartlyBrokenOAuth(_OAuth):
    """授權查詢本身對 ``misconfigured`` 丟錯（例如設定壞掉），其他照常。"""

    async def has_usable_grant(self, *, server_name, request_context, installation_generation):
        if server_name == "misconfigured":
            raise ProtocolError("bad profile")
        return await super().has_usable_grant(
            server_name=server_name,
            request_context=request_context,
            installation_generation=installation_generation,
        )


async def test_one_failing_server_does_not_hide_the_others(tmp_path, monkeypatch):
    manager = _manager(
        tmp_path,
        {
            "broken": ProtocolServerConfig(command="echo", auth_mode="oauth_user"),
            "misconfigured": ProtocolServerConfig(command="echo", auth_mode="oauth_user"),
            "private": ProtocolServerConfig(command="echo", auth_mode="oauth_user"),
        },
        _PartlyBrokenOAuth(),
    )
    calls: list = []
    working = _connector(manager, {"private": ["lookup"]}, calls)

    async def _ensure(name, request_context):
        if name == "broken":  # 連線本身失敗
            raise ProtocolError("boom")
        return await working(name, request_context)

    monkeypatch.setattr(manager, "_ensure_handle", _ensure)

    tools = await manager.get_all_tools(request_context=_listing_context())

    assert _servers(tools) == {("private", "lookup")}


async def test_other_principal_scoped_modes_never_get_the_identity(tmp_path, monkeypatch):
    """只有 oauth_user 拿到身分；enterprise 代表其他要個人授權的模式（機器對機器同一條判斷）。"""
    mode = "enterprise"
    oauth = _OAuth()
    manager = _manager(
        tmp_path,
        {"other": ProtocolServerConfig(command="echo", auth_mode=mode)},
        oauth,
    )
    calls: list = []
    monkeypatch.setattr(manager, "_ensure_handle", _connector(manager, {"other": ["x"]}, calls))

    assert await manager.get_all_tools(request_context=_listing_context()) == []
    assert calls == []
    assert oauth.checked == []


async def test_one_humans_tools_are_not_listed_for_another(tmp_path, monkeypatch):
    manager = _manager(
        tmp_path,
        {"private": ProtocolServerConfig(command="echo", auth_mode="oauth_user")},
        _OAuth(),
    )
    calls: list = []
    monkeypatch.setattr(
        manager, "_ensure_handle", _connector(manager, {"private": ["lookup"]}, calls)
    )
    await manager.get_all_tools(request_context=_listing_context())

    other = mcp_request_context(
        _foreground(human_principal_id="human-b"), _SOUL, request_id="r2", installation_id=""
    )
    await manager.get_all_tools(request_context=other)

    keys = {manager.handle_key_for("private", c) for _n, c in calls}
    assert len(calls) == 2  # human-b 沒有沿用 human-a 的連線
    assert len(keys) == 2


# --- 3. ToolLayer -------------------------------------------------------------


class _Protocols:
    """ToolLayer 用的替身：記下 get_all_tools 收到的身分。"""

    def __init__(self) -> None:
        self.contexts: list[Any] = []
        self.registry = SimpleNamespace(get_server=lambda _name: None)

    async def get_all_tools(self, request_context: Any = None) -> list[dict]:
        self.contexts.append(request_context)
        return []


def _request(model: str = "ScopeSoul") -> OpenAIChatRequest:
    return OpenAIChatRequest(model=model, messages=[OpenAIMessage(role="user", content="hi")])


async def test_tool_layer_passes_the_identity_for_a_foreground_soul():
    protocols = _Protocols()
    await ToolLayer().process(_request(), _foreground(protocol_manager=protocols))

    (context,) = protocols.contexts
    assert context.identity == _SOUL
    assert context.human_principal_id == "human-a"
    assert context.origin == McpRequestOrigin.FOREGROUND


@pytest.mark.parametrize(
    ("change", "model"),
    [
        ({"heartbeat_run": True}, "ScopeSoul"),
        ({"subagent_run": True}, "ScopeSoul"),
        ({"dispatch_run": True}, "ScopeSoul"),
        ({"resumable_stream": False}, "ScopeSoul"),
        ({"is_toukasoul": False}, "gpt-x"),
        ({}, GROUP_MODEL),
        ({"human_principal_id": None}, "ScopeSoul"),
    ],
)
async def test_tool_layer_passes_no_identity_elsewhere(change, model):
    protocols = _Protocols()
    await ToolLayer().process(_request(model), _foreground(protocol_manager=protocols, **change))

    assert protocols.contexts == [None]


@pytest.mark.parametrize(
    "extra",
    [
        {"skill_required_mcp": ["private"]},
        {"is_custom": True, "desired_tools": ["private__lookup"]},
    ],
)
async def test_needing_a_user_scoped_server_without_identity_does_not_fail(tmp_path, extra):
    manager = _manager(
        tmp_path, {"private": ProtocolServerConfig(command="echo", auth_mode="oauth_user")}
    )
    ctx = _foreground(protocol_manager=manager, resumable_stream=False, **extra)

    await ToolLayer().process(_request(), ctx)  # 以前在這裡丟 ProtocolError

    assert ctx["advertised_tools"].mcp == frozenset()


def _system_text(request: OpenAIChatRequest) -> str:
    return "\n".join(str(m.content) for m in request.messages if m.role == "system")


async def test_unconnected_shared_servers_are_named_in_a_volatile_note(tmp_path, monkeypatch):
    manager = _manager(
        tmp_path,
        {
            "connected": ProtocolServerConfig(command="echo"),
            "offline": ProtocolServerConfig(command="echo"),
            "forbidden": ProtocolServerConfig(command="echo"),
            "private": ProtocolServerConfig(command="echo", auth_mode="oauth_user"),
        },
    )
    manager.handle_pool.put(
        manager.handle_key_for("connected", None),
        LegacyMcpHandle(session=_CatalogSession(["lookup"])),
    )
    real_allowed = permission_service.is_mcp_server_allowed
    monkeypatch.setattr(
        permission_service,
        "is_mcp_server_allowed",
        lambda server, identity: server != "forbidden" and real_allowed(server, identity),
    )
    request = _request()

    await ToolLayer().process(request, _foreground(protocol_manager=manager))

    system = _system_text(request)
    assert f"<{MCP_UNAVAILABLE}>" in system
    note = system.split(f"<{MCP_UNAVAILABLE}>", 1)[1].split(f"</{MCP_UNAVAILABLE}>", 1)[0]
    assert "offline" in note
    assert "connected" not in note
    assert "forbidden" not in note
    assert "private" not in note
    assert MCP_UNAVAILABLE in VOLATILE_SYSTEM_TAGS


async def test_no_note_when_every_shared_server_is_connected(tmp_path):
    manager = _manager(tmp_path, {"connected": ProtocolServerConfig(command="echo")})
    manager.handle_pool.put(
        manager.handle_key_for("connected", None),
        LegacyMcpHandle(session=_CatalogSession(["lookup"])),
    )
    request = _request()

    await ToolLayer().process(request, _foreground(protocol_manager=manager))

    assert f"<{MCP_UNAVAILABLE}>" not in _system_text(request)


# --- 4. 執行器的第二層防線 -------------------------------------------------------


class _ExecutorProtocols:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.registry = SimpleNamespace(
            get_server=lambda name: ProtocolServerConfig(command="echo", auth_mode="oauth_user")
            if name == "private"
            else None
        )

    def resolve_tool(self, name: str) -> tuple[str | None, str | None]:
        server, _, tool = name.partition("__")
        return (server, tool) if tool else (None, None)

    async def call_tool_on_server(self, server, tool, args, request_context=None):
        self.calls.append((server, tool))
        return adapt_call_tool_result(
            SimpleNamespace(
                content=[SimpleNamespace(model_dump=lambda **_k: {"type": "text", "text": "ok"})],
                structured_content=None, is_error=False, meta=None,
            )
        )


@pytest.fixture
def gates(monkeypatch):
    """PaC 一律放行；審查員、審批卡、同意帳本不應被叫到。"""
    monkeypatch.setattr(
        permission_service, "evaluate_policy", lambda _ctx: SimpleNamespace(effect="ALLOW")
    )
    monkeypatch.setattr(permission_service, "is_mcp_tool_allowed", lambda *_a: True)
    state = SimpleNamespace(
        review=AsyncMock(), card=AsyncMock(return_value="approved"), record=AsyncMock()
    )
    monkeypatch.setattr(auto_reviewer, "review", state.review)
    monkeypatch.setattr(approval_flow, "await_human_approval", state.card)
    monkeypatch.setattr(consent_ledger, "record_policy", state.record)
    return state


async def _execute(ctx: PipelineContext, *, enforce: bool = True) -> tuple[dict, list]:
    protocols = _ExecutorProtocols()
    ctx["advertised_tools"] = AdvertisedTools(native=frozenset(), mcp={("private", "lookup")})
    queue: asyncio.Queue = asyncio.Queue()
    executor = ToolExecutor(protocols, {}, {}, {}, enforce_advertised=enforce)
    result = await executor.execute_to_queue(
        "private__lookup", {"q": "x"}, "req-t1", ctx, queue, call_id="c1"
    )
    try:
        return json.loads(result), protocols.calls
    except json.JSONDecodeError:
        return {"text": result}, protocols.calls


@pytest.mark.parametrize(
    "change", [{"heartbeat_run": True}, {"resumable_stream": False}, {GROUP_MEMBER_FLAG: True}]
)
async def test_executor_blocks_user_scoped_tools_outside_a_foreground_soul(gates, change):
    result, calls = await _execute(_foreground(**change))

    assert result.get("error_class") == "permission_denied"
    assert result.get("reason") == "user_scoped_mcp_unavailable"
    assert calls == []
    gates.review.assert_not_awaited()
    gates.card.assert_not_awaited()
    gates.record.assert_not_awaited()


async def test_executor_lets_a_foreground_soul_call_it(gates):
    _result, calls = await _execute(_foreground())
    assert calls == [("private", "lookup")]


async def test_debug_execute_is_not_subject_to_the_check(gates):
    """人按的 /debug/execute（不核對清單）照 ADR-0269 不套用。"""
    _result, calls = await _execute(_foreground(heartbeat_run=True), enforce=False)
    assert calls == [("private", "lookup")]


# --- 5. 幻覺偵測認得這一輪請求帶的工具 ----------------------------------------------


async def test_known_tool_names_include_this_requests_tools():
    request_tools = [{"type": "function", "function": {"name": "private__lookup"}}]
    names = await guard_service.collect_known_tool_names(
        {"read": object()}, {}, {}, None, request_tools=request_tools
    )
    assert names == {"read", "private__lookup"}


# --- 6. 群聊分給成員的 ctx 有群聊旗標 ----------------------------------------------


async def test_group_members_are_marked_so_the_executor_refuses_user_scoped_mcp(monkeypatch):
    seen: list[Any] = []

    async def _fake_stream(_model, _request, _identity, _request_id, _start, ctx):
        seen.append(ctx)

        async def _gen():
            yield "data: [DONE]\n\n"

        return _gen()

    monkeypatch.setattr(group, "parse_group_roster", lambda *_a: [{"name": "A", "id": "SoulA"}])
    monkeypatch.setattr(group, "get_active_brain", lambda _who: "claude-a")
    monkeypatch.setattr(group, "get_stream_generator", _fake_stream)
    monkeypatch.setattr(group, "record_working_brain", lambda *_a: None)
    monkeypatch.setattr(group, "soul_service", MagicMock(inject_home_messages=AsyncMock()))
    request = OpenAIChatRequest(
        model=GROUP_MODEL, messages=[OpenAIMessage(role="user", content="@A 早")]
    )
    ctx = PipelineContext()
    ctx["resumable_stream"] = True

    async for _chunk in group.dispatch_group_stream(request, "req-g", 0.0, ctx):
        pass

    (member_ctx,) = seen
    assert member_ctx[GROUP_MEMBER_FLAG] is True
    assert GROUP_MEMBER_FLAG not in ctx  # 群聊本身的 ctx 不被改
