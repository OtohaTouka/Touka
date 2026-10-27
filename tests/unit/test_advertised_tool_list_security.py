"""h2：執行工具前核對「這一輪實際列給模型的工具」。

ToolLayer 把實際注入（去重後）的伺服器端工具記成不可變的 ``AdvertisedTools``，寫進
``PipelineContext.advertised_tools``；模型迴圈建立的執行器（``enforce_advertised=True``）
在原生角色權限之後、軸 B／PaC／審查員／審批卡之前核對：

- 完全找不到的名稱 → ``resource_not_found``
- 清單不可用（接線問題）→ ``internal_error``／``advertised_tools_unavailable``
- 這一輪沒列出 → ``permission_denied``／``not_advertised``

三種都不呼叫審查員、不跳審批卡、不執行；也不進 iso-c2 的隔離層級判定（``policy_level``）、
不寫同意帳本（D 組）。全部走 conftest 沙盒與 mock：不連外、不讀 data/。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from memory_server.tools import memory_tool_registry
from memory_server.tools.schemas import MEMORY_TOOLS_SCHEMA_LIST
from pydantic import ValidationError
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.core.exceptions import ErrorCode, ToukaException
from src.handlers import react_core
from src.handlers.adapters.base import ProviderAdapter, ToolCall, Turn
from src.middleware.layers.tool import ToolLayer
from src.models.advertised_tools import AdvertisedTools
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.permissions import PermissionsConfig, RoleConfig
from src.models.souls import SoulConfig
from src.protocols.manager import ProtocolManager
from src.protocols.mcp.handles import LegacyMcpHandle
from src.protocols.mcp.sdk_adapter import adapt_call_tool_result
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.routers import debug as debug_router
from src.routers import heartbeat
from src.services import approval_flow, proxy_service
from src.services import tool_executor as executor_module
from src.services.auto_reviewer import auto_reviewer
from src.services.isolation.consent import consent_ledger
from src.services.permission_service import permission_service
from src.services.soul_service import SoulService
from src.services.tool_executor import ToolExecutor
from src.tools import subagent_tools
from src.tools.builtin import LEGACY_ADVERTISED_SUCCESSORS, builtin_tools_schema, tool_registry
from src.utils.async_db import SessionSnapshot

from tests.tool_eval import live_episode
from tests.tool_eval.cases import EvalCase

_ROOT = Path(__file__).resolve().parents[2]
_OWNER = AgentIdentity.for_role(agent_id="AdvertOwner", role="Owner")


def _native(name: str, description: str = "") -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": {}},
    }


def _mcp(server: str, tool: str) -> dict:
    return {
        "server": server,
        "tool": tool,
        "type": "function",
        "function": {
            "name": f"{server}__{tool}",
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        },
    }


# --- 1. 清單資料結構（B11 的型別部分） --------------------------------------


def test_from_schemas_splits_native_names_and_mcp_pairs():
    tools = AdvertisedTools.from_schemas(
        [
            _native("read"),
            _mcp("a__b", "c"),
            # ToukaUI 條目沒有裸名：用廣告名
            {"server": "ToukaUI", "type": "function", "function": {"name": "openDiff"}},
            {"type": "function", "function": {}},  # 沒名字：不算
            "not-a-schema",
        ]
    )
    assert tools.native == frozenset({"read"})
    assert tools.mcp == frozenset({("a__b", "c"), ("ToukaUI", "openDiff")})


def test_inputs_become_frozensets_detached_from_caller():
    native = ["read"]
    pairs = [["srv", "lookup"]]
    tools = AdvertisedTools(native=native, mcp=pairs)
    native.append("execute_command")
    pairs.append(["srv", "delete_everything"])

    assert isinstance(tools.native, frozenset)
    assert isinstance(tools.mcp, frozenset)
    assert tools.native == frozenset({"read"})
    assert tools.mcp == frozenset({("srv", "lookup")})
    with pytest.raises(FrozenInstanceError):
        tools.native = frozenset({"execute_command"})  # type: ignore[misc]


def test_permits_native_requires_every_successor():
    successors = {"legacy": frozenset({"a", "b"}), "hollow": frozenset()}
    partial = AdvertisedTools(native=frozenset({"a"}))
    full = AdvertisedTools(native=frozenset({"a", "b"}))

    assert partial.permits_native("a", successors)
    assert not partial.permits_native("legacy", successors)  # 少一個後繼就不放行
    assert full.permits_native("legacy", successors)
    assert not full.permits_native("hollow", successors)  # 空的後繼集合不放行
    assert not full.permits_native("unlisted", successors)


def test_permits_mcp_matches_the_exact_pair():
    tools = AdvertisedTools(mcp=frozenset({("a__b", "c")}))
    assert tools.permits_mcp("a__b", "c")
    # 廣告名同為 a__b__c，但這是另一台伺服器的另一個工具
    assert not tools.permits_mcp("a", "b__c")
    assert tools.has_server("a__b")
    assert not tools.has_server("a")


@pytest.mark.parametrize(
    "value",
    [{"native": ["read"], "mcp": []}, frozenset({"read"}), ["read"], "read"],
    ids=["dict", "frozenset", "list", "str"],
)
def test_context_field_rejects_anything_but_advertised_tools(value):
    with pytest.raises(ValidationError):
        PipelineContext(advertised_tools=value)


def test_context_field_accepts_instance_and_defaults_to_none():
    tools = AdvertisedTools(native=frozenset({"read"}))
    assert PipelineContext(advertised_tools=tools).advertised_tools is tools
    assert PipelineContext().advertised_tools is None


# --- 2. 舊名工具的後繼對應表（C7） ------------------------------------------

_FILE_VERBS = frozenset({"read", "list", "find", "grep", "write", "edit", "delete"})


def test_legacy_successors_are_pinned():
    """舊名要後繼「全部」列出才放行；表本身也釘死（含 update_my_soul 的 list）。"""
    assert dict(LEGACY_ADVERTISED_SUCCESSORS) == {
        "bash": frozenset({"execute_command"}),
        "vault_tools": _FILE_VERBS,
        "project_tools": _FILE_VERBS,
        "update_my_soul": frozenset({"edit_soul", "read", "list"}),
        "sns_control": frozenset({"send_message"}),
        "propose_new_capability": frozenset({"evolution_define"}),
        "write_mcp_implementation": frozenset({"evolution_implement_and_verify"}),
        "verify_mcp_locally": frozenset({"evolution_implement_and_verify"}),
        "request_mcp_creation": frozenset({"evolution_implement_and_verify"}),
    }
    with pytest.raises(TypeError):
        LEGACY_ADVERTISED_SUCCESSORS["bash"] = frozenset()  # type: ignore[index]


def test_legacy_successor_keys_equal_guard_baseline_legacy_tools():
    baseline = json.loads(
        (_ROOT / "scripts" / "native_tool_contract_baseline.json").read_text(encoding="utf-8")
    )
    assert set(LEGACY_ADVERTISED_SUCCESSORS) == set(baseline["legacy_tools"])


def test_legacy_successors_are_registered_current_tools():
    legacy = set(LEGACY_ADVERTISED_SUCCESSORS)
    for name, successors in LEGACY_ADVERTISED_SUCCESSORS.items():
        assert successors, name
        assert successors <= set(tool_registry), name
        assert not successors & legacy, name


def test_every_registered_tool_has_a_schema_or_a_successor_entry():
    """註冊名不是有 builtin／記憶 schema，就必須在對應表裡（否則模型永遠叫不到）。"""
    schemas = [*builtin_tools_schema, *MEMORY_TOOLS_SCHEMA_LIST]
    advertisable = {schema["function"]["name"] for schema in schemas}
    registered = set(tool_registry) | set(memory_tool_registry)
    assert registered - advertisable - set(LEGACY_ADVERTISED_SUCCESSORS) == set()


# --- 3. ToolLayer 寫入清單（A 組）與各入口沿用（C4、C5、C8、C9） ------------


class _Allow:
    effect = "ALLOW"
    reason = "test"


class _Registry:
    def __init__(self, servers: list[str]) -> None:
        self.servers = dict.fromkeys(servers, object())

    def get_server(self, name: str) -> Any:
        return self.servers.get(name)

    def find_server_by_tool_name(self, _name: str) -> None:
        return None


class _Protocols:
    """最小 MCP 管理者替身：工具清單照給定順序；切名沿用正式 ProtocolManager.resolve_tool。"""

    resolve_tool = ProtocolManager.resolve_tool

    def __init__(self, tools: list[dict], servers: list[str]) -> None:
        self._tools = tools
        self.registry = _Registry(servers)
        self.calls: list[tuple[str, str]] = []

    async def get_all_tools(self, request_context: Any = None) -> list[dict]:
        return [dict(tool) for tool in self._tools]

    async def ensure_connected(self, _server: str) -> None:
        return None

    async def call_tool_on_server(self, server, tool, _args, *, request_context):
        self.calls.append((server, tool))
        return _complete()


def _complete() -> Any:
    result = SimpleNamespace(
        content=[SimpleNamespace(model_dump=lambda **_k: {"type": "text", "text": "ok"})],
        structured_content=None,
        is_error=False,
        meta=None,
    )
    return adapt_call_tool_result(result)


class _CatalogSession:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=name,
                    title=None,
                    description=name,
                    input_schema={"type": "object"},
                    output_schema=None,
                    annotations=None,
                    icons=[],
                    meta=None,
                )
                for name in self.names
            ]
        )


def _request(tools: list[dict] | None = None) -> OpenAIChatRequest:
    return OpenAIChatRequest(
        model="m", messages=[OpenAIMessage(role="user", content="hi")], tools=tools
    )


def _layer_ctx(identity: AgentIdentity = _OWNER, **extra: Any) -> PipelineContext:
    ctx = PipelineContext(identity=identity, agent_id=identity.agent_id)
    ctx["headers"] = {}
    for key, value in extra.items():
        ctx[key] = value
    return ctx


async def _assemble(request: Any, ctx: Any) -> AdvertisedTools:
    await ToolLayer().process(request, ctx)
    advertised = ctx["advertised_tools"]
    assert isinstance(advertised, AdvertisedTools)
    return advertised


def _names(tools: list | None) -> set[str]:
    return {(tool.get("function") or {}).get("name") for tool in tools or []}


@pytest.fixture
def grant_roles(monkeypatch):
    """注入測試角色表；所有入口仍走真的 resolve_grants。"""

    def _install(mapping: dict[str, RoleConfig]) -> None:
        monkeypatch.setattr(permission_service, "roles_config", PermissionsConfig(roles=mapping))
        monkeypatch.setattr(permission_service, "_auto_reload_check", lambda: None)

    return _install


async def test_a1_list_equals_injected_tools_after_desired_tools_filter():
    request = _request()
    advertised = await _assemble(request, _layer_ctx(desired_tools=["read", "list"]))
    assert advertised.native == _names(request.tools)
    assert {"read", "list"} <= advertised.native
    assert "execute_command" not in advertised.native
    assert advertised.mcp == frozenset()


@pytest.mark.parametrize("incognito", [True, False])
async def test_a2_incognito_leaves_memory_tools_off_the_list(incognito):
    ctx = _layer_ctx(memory_tools_schema=MEMORY_TOOLS_SCHEMA_LIST, incognito=incognito)
    advertised = await _assemble(_request(), ctx)
    assert ("memory_tools" in advertised.native) is (not incognito)


@pytest.mark.parametrize("present", [True, False])
async def test_a3_ask_user_listed_only_when_a_human_is_present(present):
    advertised = await _assemble(_request(), _layer_ctx(resumable_stream=present))
    assert ("ask_user" in advertised.native) is present


async def test_a4_dynamic_mount_lists_only_mounted_servers():
    protocols = _Protocols([_mcp("mounted", "x"), _mcp("idle", "y")], ["mounted", "idle"])
    ctx = _layer_ctx(
        protocol_manager=protocols, mcp_dispatcher=True, skill_required_mcp=["mounted"]
    )
    advertised = await _assemble(_request(), ctx)
    assert advertised.mcp == frozenset({("mounted", "x")})


async def test_a5_client_schemas_never_count_as_server_tools():
    """客戶端同名 schema 蓋掉的伺服器端工具不算列出；客戶端獨有的工具也不算。"""
    request = _request(tools=[_native("read", "client copy"), _native("client_only")])
    advertised = await _assemble(request, _layer_ctx())
    assert "read" not in advertised.native
    assert "client_only" not in advertised.native
    assert "list" in advertised.native


async def test_a6_layer_always_overwrites_a_stale_list():
    stale = AdvertisedTools(native=frozenset({"stale_tool"}), mcp=frozenset({("old", "x")}))
    advertised = await _assemble(_request(), _layer_ctx(advertised_tools=stale))
    assert advertised is not stale
    assert "stale_tool" not in advertised.native

    # 這一輪一個工具都沒列：仍要覆寫成空清單，不能留著舊的
    empty_ctx = _layer_ctx(advertised_tools=stale, is_custom=True, desired_tools=["no_such"])
    assert await _assemble(_request(), empty_ctx) == AdvertisedTools()


async def test_a7_mcp_pairs_use_the_projected_bare_name():
    protocols = _Protocols([_mcp("a__b", "c"), _mcp("plain", "x__y")], ["a__b", "plain"])
    advertised = await _assemble(_request(), _layer_ctx(protocol_manager=protocols))
    assert advertised.mcp == frozenset({("a__b", "c"), ("plain", "x__y")})


def _real_manager(tmp_path: Path) -> ProtocolManager:
    # 設定檔放 tmp_path（不存在＝空設定），不讀目前工作目錄
    registry = ProtocolRegistry(str(tmp_path / "server_config.json"))
    registry.servers = {
        "shared": ProtocolServerConfig(command="echo"),
        "private": ProtocolServerConfig(command="echo", auth_mode="oauth_user"),
        "offline": ProtocolServerConfig(command="echo"),
    }
    manager = ProtocolManager(registry)
    manager.handle_pool.put(
        manager.handle_key_for("shared", None),
        LegacyMcpHandle(session=_CatalogSession(["lookup"])),
    )
    return manager


async def test_a8_user_authorized_and_unconnected_servers_are_never_listed(tmp_path):
    """Gate 1 從來不列各自授權（oauth_user）與還沒連線的共享伺服器（另開任務 T1）。"""
    manager = _real_manager(tmp_path)
    advertised = await _assemble(_request(), _layer_ctx(protocol_manager=manager))
    assert advertised.mcp == frozenset({("shared", "lookup")})


async def test_a9_short_mcp_names_and_plugins_in_desired_tools_stay_unlisted():
    """SoulStudio 存的是 MCP 短名、外掛名：Gate 1 比長名，從來不列（另開任務 T2）。"""
    protocols = _Protocols([_mcp("srv", "lookup")], ["srv"])
    ctx = _layer_ctx(
        protocol_manager=protocols,
        desired_tools=["read", "lookup", "my_plugin"],
        plugin_schemas=[_native("my_plugin")],
    )
    advertised = await _assemble(_request(), ctx)
    assert advertised.mcp == frozenset()
    assert "my_plugin" not in advertised.native
    assert "read" in advertised.native


async def test_a10_plain_dict_context_receives_the_list():
    ctx: dict[str, Any] = {"identity": _OWNER, "agent_id": _OWNER.agent_id, "headers": {}}
    request = _request()
    await ToolLayer().process(request, ctx)
    assert ctx["advertised_tools"] == AdvertisedTools.from_schemas(request.tools)


class _ToolLayerOnly:
    async def execute(self, request: Any, ctx: Any) -> None:
        await ToolLayer().process(request, ctx)


async def _no_chunks():
    for _ in ():
        yield ""


async def test_c4_subagent_uses_its_own_list_without_dispatch_tools(monkeypatch, grant_roles):
    grant_roles(
        {
            "Subagent": RoleConfig(
                allowed_tools=["read", "dispatch_subagent", "dispatch_subagent_group"]
            )
        }
    )
    monkeypatch.setattr(permission_service, "evaluate_policy", lambda _c: _Allow())
    monkeypatch.setattr(proxy_service, "pipeline", _ToolLayerOnly())
    captured: dict[str, Any] = {}

    async def _stream(_model, request, _identity, _request_id, _t0, ctx):
        captured.update(request=request, ctx=ctx)
        return _no_chunks()

    monkeypatch.setattr(subagent_tools, "get_stream_generator", _stream)
    parent_list = AdvertisedTools(native=frozenset({"execute_command", "dispatch_subagent"}))
    parent = PipelineContext(
        identity=_OWNER, agent_id=_OWNER.agent_id, auth_level="Manual", advertised_tools=parent_list
    )

    await subagent_tools._run_subagent(
        goal="g", parent_identity=_OWNER, parent_request_id="p", base_ctx=parent, model="m"
    )

    sent = _names(captured["request"].tools)
    assert "read" in sent
    assert not sent & {"dispatch_subagent", "dispatch_subagent_group"}
    assert captured["ctx"].advertised_tools == AdvertisedTools(native=frozenset(sent))
    assert parent.advertised_tools.native == {"execute_command", "dispatch_subagent"}


async def test_c5_every_heartbeat_pulse_dispatches_a_fresh_list(monkeypatch):
    captured: list[tuple[Any, Any]] = []

    async def _dispatch(**kwargs: Any) -> Any:
        captured.append((kwargs["request"], kwargs["context"].advertised_tools))
        return MagicMock(body_iterator=_no_chunks())

    soul = object.__new__(SoulService)  # 不污染 singleton
    soul.get_soul_data = lambda _identity: ("靈魂", SoulConfig().model_dump())
    session = {"id": "s", "title": "t", "messages": [{"role": "user", "content": "hi"}]}
    monkeypatch.setattr(
        heartbeat, "session_service",
        MagicMock(read_execution_snapshot=AsyncMock(return_value=SessionSnapshot(session, "rev"))),
    )
    monkeypatch.setattr(heartbeat, "dispatch_request", _dispatch)
    monkeypatch.setattr(heartbeat, "autonomy_service", MagicMock())
    monkeypatch.setattr(heartbeat, "rhythm_service", MagicMock())
    monkeypatch.setattr(heartbeat, "soul_service", soul)
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    stale = AdvertisedTools(native=frozenset({"stale_tool"}))
    ctx = PipelineContext(advertised_tools=stale)

    for _pulse in range(2):
        await heartbeat._execute_single_pulse("s", identity, "claude-sonnet-4-6", ctx, 1, 0.0, "")
        ctx["advertised_tools"] = stale

    assert len(captured) == 2
    for request, advertised in captured:
        assert advertised == AdvertisedTools.from_schemas(request.tools)
        assert "stale_tool" not in advertised.native
        assert "ask_user" not in advertised.native  # 心跳無人在場


def test_c8_group_model_copy_keeps_the_list():
    listed = AdvertisedTools(native=frozenset({"read"}))
    assert PipelineContext(advertised_tools=listed).model_copy().advertised_tools is listed


class _PrimaryDownError(Exception):
    pass


async def test_c9_fallback_brain_shares_context_and_list(monkeypatch):
    listed = AdvertisedTools(native=frozenset({"read"}))
    seen: list[tuple[str, Any]] = []

    async def _execute(_request, ctx):
        ctx["advertised_tools"] = listed

    async def _dispatch(model_name, _request, _identity, _request_id, _t0, ctx):
        seen.append((model_name, ctx.get("advertised_tools")))
        if len(seen) == 1:
            raise _PrimaryDownError
        return "ok"

    monkeypatch.setattr(proxy_service.pipeline, "execute", _execute)
    monkeypatch.setattr(proxy_service, "dispatch_request", _dispatch)
    monkeypatch.setattr(proxy_service, "get_last_working_brain", lambda _identity: "claude-b")
    monkeypatch.setattr(proxy_service, "same_route_family", lambda _a, _b: True)
    ctx = PipelineContext(identity=_OWNER, is_toukasoul=True, request_id="r", incognito=True)

    assert await proxy_service.process_proxy_request(SimpleNamespace(model="claude-a"), ctx) == "ok"
    assert seen == [("claude-a", listed), ("claude-b", listed)]


# --- 4. 執行器核對（B 組；開關打開） ------------------------------------------


@dataclass
class _Decision:
    effect: str
    reason: str = "test"


@pytest.fixture
def gates(monkeypatch):
    """PaC、審查員、審批卡的替身：記錄有沒有被叫到。PaC 預設 ALLOW。"""
    state = SimpleNamespace(effect="ALLOW", policy_calls=[])

    def _evaluate(context):
        state.policy_calls.append(context)
        return _Decision(state.effect)

    state.review = AsyncMock(return_value=SimpleNamespace(verdict="allow", reason="r", code="c"))
    state.card = AsyncMock(return_value="approved")
    monkeypatch.setattr(permission_service, "evaluate_policy", _evaluate)
    monkeypatch.setattr(auto_reviewer, "review", state.review)
    monkeypatch.setattr(approval_flow, "await_human_approval", state.card)
    return state


@pytest.fixture
def ordinary_bindings(monkeypatch):
    """替身 handler 走一般分類：拿掉本檢查時它們真的會被呼叫（突變看得出來）。"""
    monkeypatch.setattr(executor_module, "BUILTIN_EXECUTION_BINDINGS", {})


def _executor(
    tools: dict | None = None,
    *,
    plugins: dict | None = None,
    protocols: Any = None,
    enforce: bool = True,
) -> ToolExecutor:
    return ToolExecutor(protocols, tools or {}, {}, plugins or {}, enforce_advertised=enforce)


def _listed_ctx(
    native: set[str] | frozenset[str] = frozenset(),
    mcp: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
    *,
    identity: AgentIdentity = _OWNER,
    auth: str = "Manual",
    **extra: Any,
) -> PipelineContext:
    ctx = PipelineContext(
        identity=identity,
        agent_id=identity.agent_id,
        auth_level=auth,
        advertised_tools=AdvertisedTools(native=frozenset(native), mcp=frozenset(mcp)),
    )
    for key, value in extra.items():
        ctx[key] = value
    return ctx


async def _call(
    executor: ToolExecutor,
    name: str,
    ctx: Any,
    args: dict | None = None,
    used: set[str] | None = None,
) -> tuple[dict, list[dict]]:
    queue: asyncio.Queue = asyncio.Queue()
    result = await executor.execute_to_queue(
        name, dict(args or {"q": "x"}), "req-h2", ctx, queue, call_id="c1", actually_used_tools=used
    )
    events = []
    while not queue.empty():
        event = queue.get_nowait()
        if event is not None:
            events.append(event)
    try:
        return json.loads(result), events
    except json.JSONDecodeError:
        return {"text": result}, events


def _blocked(result: dict, events: list[dict], error_class: str, reason: str | None) -> None:
    assert result.get("error_class") == error_class, result
    assert result.get("reason") == reason, result
    assert [event["type"] for event in events] == ["tool_result"]
    assert events[0]["status"] == "error"
    assert events[0]["error_class"] == error_class


async def test_b1_yolo_unlisted_terminal_command_is_blocked(gates, ordinary_bindings):
    gates.effect = "ALLOW"  # YOLO 會直接放行
    handler = AsyncMock(return_value="ran")
    ctx = _listed_ctx({"read"}, auth="YOLO")
    result, events = await _call(
        _executor({"execute_command": handler}), "execute_command", ctx, {"command": "echo hi"}
    )
    _blocked(result, events, "permission_denied", "not_advertised")
    handler.assert_not_awaited()


@pytest.mark.parametrize("listed", [False, True])
async def test_b2_manual_read_only_list_needs_to_be_listed(listed):
    """真政策：唯讀的 list 在 Manual 直接放行——所以沒列出時只剩本檢查擋得住。"""
    handler = AsyncMock(return_value="files")
    ctx = _listed_ctx({"read", "list"} if listed else {"read"})
    result, events = await _call(_executor({"list": handler}), "list", ctx, {"scope": "vault"})
    if listed:
        handler.assert_awaited_once()
    else:
        _blocked(result, events, "permission_denied", "not_advertised")
        handler.assert_not_awaited()


async def test_b3_incognito_memory_query_is_blocked_before_the_backend():
    backend = AsyncMock(return_value="memories")
    ctx = _listed_ctx({"read"}, incognito=True)
    result, events = await _call(
        _executor({"memory_tools": backend}),
        "memory_tools",
        ctx,
        {"action": "query", "query": "secret"},
    )
    _blocked(result, events, "permission_denied", "not_advertised")
    backend.assert_not_awaited()


async def test_b4_headless_ask_user_is_refused_immediately(gates):
    ask = AsyncMock(return_value="answer")
    result, events = await _call(
        _executor({"ask_user": ask}), "ask_user", _listed_ctx({"read"}), {"questions": []}
    )
    _blocked(result, events, "permission_denied", "not_advertised")
    assert not [event for event in events if event["type"] == "ask_user_request"]
    ask.assert_not_awaited()


async def test_b5_unmounted_mcp_is_blocked_and_listed_one_dispatches(gates):
    protocols = _Protocols([], ["mounted", "idle"])
    ctx = _listed_ctx(mcp={("mounted", "x")})
    executor = _executor(protocols=protocols)

    result, events = await _call(executor, "idle__y", ctx)
    _blocked(result, events, "permission_denied", "not_advertised")
    assert protocols.calls == []

    await _call(executor, "mounted__x", ctx)
    assert protocols.calls == [("mounted", "x")]


@pytest.mark.parametrize(
    ("registry", "expected"),
    [(["a__b"], "resource_not_found"), (["a__b", "a"], "not_advertised")],
    ids=["i-unknown-server", "i-prime-known-server"],
)
async def test_b6_listed_pair_never_covers_the_other_split(gates, registry, expected):
    """只列 (a__b, c)：a__b__c 執行時切成 (a, b__c)，不是模型看到的那一個。"""
    protocols = _Protocols([], registry)
    ctx = _listed_ctx(mcp={("a__b", "c")})
    result, _events = await _call(_executor(protocols=protocols), "a__b__c", ctx)
    if expected == "resource_not_found":
        assert result["error_class"] == "resource_not_found"
    else:
        assert result["reason"] == "not_advertised"
    assert protocols.calls == []


@pytest.mark.parametrize("first", ["a__b", "a"])
async def test_b6_collision_follows_the_schema_actually_injected(gates, first):
    """兩台都列出、廣告名相同：清單只留先注入（模型看到）的那一對。"""
    order = [_mcp("a__b", "c"), _mcp("a", "b__c")]
    protocols = _Protocols(order if first == "a__b" else order[::-1], ["a__b", "a"])
    ctx = _layer_ctx(protocol_manager=protocols)
    await ToolLayer().process(_request(), ctx)

    result, _events = await _call(_executor(protocols=protocols), "a__b__c", ctx)
    if first == "a__b":
        assert result["reason"] == "not_advertised"
        assert protocols.calls == []
    else:
        assert protocols.calls == [("a", "b__c")]


_LEGACY_CASES = [
    (name, missing)
    for name, successors in sorted(LEGACY_ADVERTISED_SUCCESSORS.items())
    for missing in [None, *sorted(successors)]
]


@pytest.mark.parametrize(("name", "missing"), _LEGACY_CASES)
async def test_b7_legacy_name_needs_every_successor_listed(gates, ordinary_bindings, name, missing):
    handler = AsyncMock(return_value="done")
    listed = set(LEGACY_ADVERTISED_SUCCESSORS[name]) - {missing}
    result, events = await _call(
        _executor({name: handler}), name, _listed_ctx(listed), {"action": "list"}
    )
    if missing is None:
        handler.assert_awaited_once()
    else:
        _blocked(result, events, "permission_denied", "not_advertised")
        handler.assert_not_awaited()


@pytest.mark.parametrize(
    ("auth", "effect", "interactive"),
    [("Auto", "REVIEW", False), ("Manual", "CONFIRM", True)],
)
@pytest.mark.parametrize("name", ["nosuchtool", "nosuchserver__x"])
async def test_b8_unknown_names_skip_policy_reviewer_and_card(
    gates, auth, effect, interactive, name
):
    gates.effect = effect
    protocols = _Protocols([], ["existingserver"])
    ctx = _listed_ctx({"read"}, auth=auth, resumable_stream=interactive)
    result, events = await _call(_executor(protocols=protocols), name, ctx)
    _blocked(result, events, "resource_not_found", None)
    assert gates.policy_calls == []
    gates.review.assert_not_awaited()
    gates.card.assert_not_awaited()


async def test_b8_known_server_with_unlisted_tool_is_not_advertised(gates):
    gates.effect = "REVIEW"
    protocols = _Protocols([], ["existingserver"])
    ctx = _listed_ctx({"read"}, auth="Auto")
    result, events = await _call(_executor(protocols=protocols), "existingserver__unlisted", ctx)
    _blocked(result, events, "permission_denied", "not_advertised")
    assert gates.policy_calls == []
    assert protocols.calls == []


async def test_b9_listed_tools_still_need_role_grants(gates, grant_roles):
    subagent = AgentIdentity.for_role(agent_id="SubProbe", role="Subagent")
    grant_roles(
        {
            "Subagent": RoleConfig(allowed_tools=["read"], allowed_mcp_servers=["srv/ok"]),
        }
    )
    handler = AsyncMock(return_value="ran")
    ctx = _listed_ctx({"read", "execute_command"}, {("srv", "other")}, identity=subagent)
    result, _events = await _call(_executor({"execute_command": handler}), "execute_command", ctx)
    assert result["reason"] == "native_tool_rbac"
    handler.assert_not_awaited()

    protocols = _Protocols([], ["srv"])
    result, _events = await _call(_executor(protocols=protocols), "srv__other", ctx)
    assert result["reason"] == "mcp_tool_rbac"
    assert protocols.calls == []


async def test_b9b_role_grants_are_checked_before_the_list(gates, grant_roles):
    """順序釘子：沒列出也沒授權的原生工具回 native_tool_rbac（ADR-0229），不是 not_advertised。"""
    subagent = AgentIdentity.for_role(agent_id="SubProbe", role="Subagent")
    grant_roles({"Subagent": RoleConfig(allowed_tools=["read"])})
    handler = AsyncMock(return_value="ran")
    ctx = _listed_ctx({"read"}, identity=subagent)
    result, events = await _call(_executor({"execute_command": handler}), "execute_command", ctx)
    _blocked(result, events, "permission_denied", "native_tool_rbac")
    handler.assert_not_awaited()
    assert gates.policy_calls == []


@pytest.mark.parametrize(
    ("auth", "effect", "interactive"),
    [("Auto", "REVIEW", False), ("Manual", "CONFIRM", True), ("YOLO", "ALLOW", True)],
)
async def test_b10_unlisted_known_tool_never_reaches_policy_reviewer_or_card(
    gates, ordinary_bindings, auth, effect, interactive
):
    gates.effect = effect
    handler = AsyncMock(return_value="ran")
    ctx = _listed_ctx({"read"}, auth=auth, resumable_stream=interactive)
    result, events = await _call(_executor({"write": handler}), "write", ctx)
    _blocked(result, events, "permission_denied", "not_advertised")
    assert gates.policy_calls == []
    gates.review.assert_not_awaited()
    gates.card.assert_not_awaited()
    handler.assert_not_awaited()


def _plain_ctx(value: Any) -> PipelineContext:
    ctx = PipelineContext(identity=_OWNER, agent_id=_OWNER.agent_id, auth_level="YOLO")
    if value is not _MISSING:
        ctx["advertised_tools"] = value
    return ctx


_MISSING = object()


def _assigned_ctx(value: Any) -> PipelineContext:
    """建好之後才用屬性指派：Pydantic 不驗證事後指派，由執行器的 isinstance 擋下。"""
    ctx = _plain_ctx(_MISSING)
    ctx.advertised_tools = value
    return ctx


@pytest.mark.parametrize(
    "ctx_factory",
    [
        lambda: _plain_ctx({"native": ["write"], "mcp": []}),
        lambda: _assigned_ctx({"native": ["write"], "mcp": []}),
        lambda: _plain_ctx(frozenset({"write"})),
        lambda: _plain_ctx(None),
        lambda: _plain_ctx(_MISSING),
        lambda: {"identity": _OWNER, "auth_level": "YOLO", "advertised_tools": ["write"]},
        lambda: {"identity": _OWNER, "auth_level": "YOLO"},
    ],
    ids=[
        "dict",
        "assigned-dict",
        "frozenset",
        "none",
        "missing",
        "sns-dict-list",
        "sns-dict-missing",
    ],
)
async def test_b11_unusable_list_blocks_with_internal_error(gates, ctx_factory):
    handler = AsyncMock(return_value="ran")
    result, events = await _call(_executor({"write": handler}), "write", ctx_factory())
    _blocked(result, events, "internal_error", "advertised_tools_unavailable")
    handler.assert_not_awaited()
    assert gates.policy_calls == []


async def test_b11_model_arguments_cannot_supply_the_list(gates):
    handler = AsyncMock(return_value="ran")
    smuggled = {"advertised_tools": {"native": ["write"]}, "path": "x"}
    result, events = await _call(_executor({"write": handler}), "write", _listed_ctx(), smuggled)
    _blocked(result, events, "permission_denied", "not_advertised")
    handler.assert_not_awaited()


async def test_b12_plugin_is_blocked_even_in_yolo(gates):
    plugin = AsyncMock(return_value="ran")
    ctx = _listed_ctx({"read"}, auth="YOLO")
    result, events = await _call(_executor(plugins={"my_plugin": plugin}), "my_plugin", ctx)
    _blocked(result, events, "permission_denied", "not_advertised")
    plugin.assert_not_awaited()


async def test_b13_blocked_calls_still_count_as_attempted(gates):
    used: set[str] = set()
    await _call(
        _executor({"memory_tools": AsyncMock()}),
        "memory_tools",
        _listed_ctx({"read"}),
        {"action": "query", "query": "x"},
        used,
    )
    assert {"memory_tools", "memory_tools.query"} <= used


async def test_b15_b16_user_authorized_and_unconnected_mcp_never_connect(
    gates, monkeypatch, tmp_path
):
    manager = _real_manager(tmp_path)
    ctx = _layer_ctx(protocol_manager=manager)
    await ToolLayer().process(_request(), ctx)
    upstream = AsyncMock()
    connect = AsyncMock()
    monkeypatch.setattr(manager, "call_tool_on_server", upstream)
    monkeypatch.setattr(manager, "_ensure_handle", connect)

    for name in ("private__lookup", "offline__lookup"):
        result, events = await _call(_executor(protocols=manager), name, ctx)
        _blocked(result, events, "permission_denied", "not_advertised")
    upstream.assert_not_awaited()
    connect.assert_not_awaited()


async def test_b17_server_connected_mid_loop_waits_for_the_next_turn(gates):
    protocols = _Protocols([], ["late"])
    result, _events = await _call(_executor(protocols=protocols), "late__tool", _listed_ctx())
    assert result["reason"] == "not_advertised"
    assert "next turn" in result["suggestion"]
    assert protocols.calls == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("openDiff", "resource_not_found"),
        ("ToukaUI__openDiff", "dispatched"),
        ("ToukaUI__closeDiff", "not_advertised"),
    ],
)
async def test_b18_toukaui_pairs(gates, name, expected):
    """ToukaUI 不在 registry；清單有用它的一對就算「找得到」。"""
    protocols = _Protocols([], [])
    ctx = _listed_ctx(mcp={("ToukaUI", "openDiff")})
    result, _events = await _call(_executor(protocols=protocols), name, ctx)
    if expected == "dispatched":
        assert protocols.calls == [("ToukaUI", "openDiff")]
    elif expected == "resource_not_found":
        assert result["error_class"] == "resource_not_found"
        assert gates.policy_calls == []
    else:
        assert result["reason"] == "not_advertised"


async def test_b19_switch_off_keeps_the_old_behavior(gates):
    """開關關閉（/debug/execute）：不看清單；找不到的名稱照舊先過政策。"""
    handler = AsyncMock(return_value="ran")
    executor = _executor({"write": handler}, enforce=False)
    await _call(executor, "write", _listed_ctx({"read"}))
    handler.assert_awaited_once()

    result, _events = await _call(executor, "nosuchtool", _listed_ctx({"read"}))
    assert result["error_class"] == "resource_not_found"
    assert len(gates.policy_calls) == 2


# --- 4b. 和 iso-c2 隔離層的順序（D 組；合併 iso-c2 時加） -------------------------


@pytest.fixture
def isolation_spies(monkeypatch):
    """包住真的 policy_level 與 consent_ledger.record_policy：只記錄，照常執行。"""
    level = AsyncMock(wraps=executor_module.policy_level)
    record = Mock(wraps=consent_ledger.record_policy)
    monkeypatch.setattr(executor_module, "policy_level", level)
    monkeypatch.setattr(consent_ledger, "record_policy", record)
    return SimpleNamespace(level=level, record=record)


@pytest.mark.parametrize(
    ("name", "ctx_factory", "error_class", "reason"),
    [
        ("execute_command", lambda: _listed_ctx({"read"}, auth="YOLO"), "permission_denied",
         "not_advertised"),
        ("execute_command", lambda: _plain_ctx(_MISSING), "internal_error",
         "advertised_tools_unavailable"),
        ("nosuchserver__x", lambda: _listed_ctx({"read"}, auth="YOLO"), "resource_not_found",
         None),
    ],
    ids=["not_advertised", "advertised_tools_unavailable", "resource_not_found"],
)
async def test_d1_blocked_calls_never_reach_isolation_level_or_consent(
    gates, ordinary_bindings, isolation_spies, name, ctx_factory, error_class, reason
):
    """被擋的呼叫不進 policy_level，也就不會在同意帳本留下層級與 subject。"""
    handler = AsyncMock(return_value="ran")
    protocols = _Protocols([], ["existingserver"])
    executor = _executor({"execute_command": handler}, protocols=protocols)
    result, events = await _call(executor, name, ctx_factory(), {"command": "echo hi"})
    _blocked(result, events, error_class, reason)
    # (policy_level 次數, record_policy 次數)：紅燈時兩個數字一起看得到
    assert (isolation_spies.level.await_count, isolation_spies.record.call_count) == (0, 0)
    handler.assert_not_awaited()
    assert gates.policy_calls == []


async def test_d1_control_listed_terminal_call_records_its_isolation_level(
    gates, ordinary_bindings, isolation_spies
):
    """對照組：列出的終端呼叫真的經過 policy_level 並記進帳本（間諜接在真的路徑上）。"""
    handler = AsyncMock(return_value="ran")
    ctx = _listed_ctx({"execute_command"}, auth="YOLO")
    executor = _executor({"execute_command": handler})
    await _call(executor, "execute_command", ctx, {"command": "echo hi"})
    isolation_spies.level.assert_awaited_once()
    isolation_spies.record.assert_called_once()
    handler.assert_awaited_once()


# --- 5. 模型迴圈接線（C1、C1b、C1c、B14） --------------------------------------


@pytest.fixture
def quiet_loop(monkeypatch):
    """迴圈的輪數／預算替身：互動不限輪、不超預算。"""
    rhythm = react_core.rhythm_service
    monkeypatch.setattr(rhythm, "initialize_session", Mock())
    monkeypatch.setattr(rhythm, "get_effective_rounds", Mock(return_value=0))
    monkeypatch.setattr(rhythm, "consume_round", Mock(return_value=True))
    monkeypatch.setattr(rhythm, "get_total_rounds", Mock(return_value=0))
    monkeypatch.setattr(react_core.autonomy_service, "is_over_budget", Mock(return_value=False))


def _loop_adapter(turns: list[Turn]) -> Mock:
    adapter = Mock(spec=ProviderAdapter)
    adapter.prepare_request = AsyncMock(return_value=SimpleNamespace(chunk_model="test"))
    adapter.close = AsyncMock()
    adapter.append_tool_results = AsyncMock()
    adapter.parse_round.side_effect = turns
    adapter.extract_usage.return_value = ({}, 0, 0)
    adapter.partial_usage.return_value = None
    adapter.mid_loop_hook.return_value = None
    adapter.finalize.return_value = {}
    adapter.stop_reason.return_value = "stop"
    adapter.telemetry_model.return_value = "test"
    adapter.emits_usage_event.return_value = False
    adapter.resolve_provider_label = Mock(return_value="test")

    async def _send(_state):
        yield {}

    adapter.send_round.side_effect = _send
    return adapter


def _loop_request() -> Any:
    return SimpleNamespace(model="m", messages=[SimpleNamespace(role="user", content="hi")])


def _loop_ctx(**extra: Any) -> PipelineContext:
    ctx = PipelineContext(
        identity=_OWNER, agent_id=_OWNER.agent_id, request_id="h2-loop", t0=0.0, auth_level="YOLO"
    )
    for key, value in {"tool_registry": {}, "memory_tool_registry": {}, **extra}.items():
        ctx[key] = value
    return ctx


async def _drain(stream: Any) -> list[Any]:
    return [chunk async for chunk in stream]


async def test_c1_model_loop_builds_the_executor_with_enforcement_on(monkeypatch, quiet_loop):
    built = MagicMock()
    monkeypatch.setattr(react_core, "ToolExecutor", built)
    await _drain(
        react_core.run_react_loop(
            _loop_adapter([Turn(text_parts=["ok"])]), _loop_request(), _loop_ctx()
        )
    )
    assert built.call_args.kwargs["enforce_advertised"] is True


async def test_c1b_loop_refuses_an_executor_with_enforcement_off(monkeypatch, quiet_loop):
    monkeypatch.setattr(
        react_core, "ToolExecutor", lambda *_a, **_k: SimpleNamespace(enforce_advertised=False)
    )
    adapter = _loop_adapter([Turn(text_parts=["ok"])])
    with pytest.raises(ToukaException) as raised:
        await _drain(react_core.run_react_loop(adapter, _loop_request(), _loop_ctx()))
    assert raised.value.code == ErrorCode.SYSTEM_ERROR
    adapter.prepare_request.assert_not_awaited()
    adapter.send_round.assert_not_called()


async def test_c1c_truthy_mock_executor_keeps_the_loop_running(monkeypatch, quiet_loop):
    """執行期檢查用真假值：既有測試換成 MagicMock（屬性是真值、不等於 True）照常跑。"""
    monkeypatch.setattr(react_core, "ToolExecutor", MagicMock())
    adapter = _loop_adapter([Turn(text_parts=["ok"])])
    await _drain(react_core.run_react_loop(adapter, _loop_request(), _loop_ctx()))
    adapter.prepare_request.assert_awaited_once()


async def test_b14_same_round_runs_only_the_listed_call(gates, quiet_loop):
    read = AsyncMock(return_value="contents")
    write = AsyncMock(return_value="written")
    calls = [
        ToolCall(id="c1", name="read", args={"path": "a"}),
        ToolCall(id="c2", name="write", args={"path": "b"}),
    ]
    adapter = _loop_adapter([Turn(tool_calls=calls), Turn(text_parts=["done"])])
    ctx = _loop_ctx(
        tool_registry={"read": read, "write": write},
        advertised_tools=AdvertisedTools(native=frozenset({"read"})),
    )

    await _drain(react_core.run_react_loop(adapter, _loop_request(), ctx))

    read.assert_awaited_once()
    write.assert_not_awaited()
    sent_back = adapter.append_tool_results.await_args.args[1]
    results = {call_id: text for call_id, _name, text in sent_back}
    assert json.loads(results["c2"])["reason"] == "not_advertised"


# --- 6. /debug/execute（C6）與 live_episode ------------------------------------


async def test_c6_debug_execute_runs_unlisted_tools_with_the_switch_explicitly_off(
    auth_client, gates, monkeypatch
):
    created: list[dict] = []

    def _spy(*args: Any, **kwargs: Any) -> ToolExecutor:
        created.append(kwargs)
        return ToolExecutor(*args, **kwargs)

    monkeypatch.setattr(debug_router, "ToolExecutor", _spy)
    probe = AsyncMock(return_value="probe ran")
    registry = auth_client.app.state.context["tool_registry"]
    monkeypatch.setitem(registry, "h2_unlisted_probe", probe)

    response = auth_client.post(
        "/v1/debug/execute",
        json={"tool_name": "h2_unlisted_probe", "args": {"x": 1}, "auth_level": "YOLO"},
    )

    assert response.status_code == 200
    probe.assert_awaited_once()
    assert len(created) == 1
    assert "enforce_advertised" in created[0]
    assert created[0]["enforce_advertised"] is False


async def test_live_episode_hands_its_assembled_list_to_the_loop(monkeypatch):
    request = SimpleNamespace(model="fake-brain", max_tokens=8192, tools=[_native("read")])
    captured: dict[str, Any] = {}

    async def _assemble(**_kwargs: Any) -> Any:
        return request

    async def _factory(_model, _request, _identity, _request_id, _t0, context):
        captured["context"] = context
        return _no_chunks()

    monkeypatch.setattr(live_episode, "_assemble_episode_request", _assemble)
    case = EvalCase(id="h2", title="t", prompt="p", context={"agent_id": "DebugAgent"})
    await live_episode.run_case_live_episode(
        case,
        model="gpt-test",
        limits=live_episode.LiveEpisodeLimits(
            provider="openai", max_tokens=96, max_provider_requests=1
        ),
        stream_factory=_factory,
    )
    assert captured["context"]["advertised_tools"] == AdvertisedTools(native=frozenset({"read"}))


# --- 7. 開關預設開與結構測試（C2：誰能關開關；C3：清單欄位的讀寫者） ----------

_SCAN_ROOTS = ("ToukaProxy", "ToukaMemory", "ToukaHeartbeat", "ToukaDispatch", "scripts")
# 不進去的資料夾：測試、虛擬環境、產生物、快取；data／soul 一律不走（DATA_PROTECTION）
_SKIP_PARTS = frozenset(
    {
        "tests",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".git",
        "data",
        "soul",
    }
)
_REACT_CORE = "ToukaProxy/src/handlers/react_core.py"
_DEBUG = "ToukaProxy/src/routers/debug.py"
_EXECUTOR_MODULE = "ToukaProxy/src/services/tool_executor.py"
_SWITCH = "enforce_advertised"
_PRIVATE_SWITCH = "_enforce_advertised"
# 執行器裡負責核對的兩個方法：被覆寫或換掉，開關還是 True 但清單與在場檢查都沒了
_CHECK_METHODS = frozenset({"_offer_denial", "_user_scope_denial"})
_PRIVATE_NAMES = _CHECK_METHODS | {_PRIVATE_SWITCH}
_PROTECTED_STRINGS = _PRIVATE_NAMES | {
    _SWITCH,
    "__kwdefaults__",
    "ToolExecutor",
    "execute_to_queue",
}
# 換掉這兩個就能讓核對不跑：type(ex).execute_to_queue = f、ex.__class__.__init__ = g
_REPLACEABLE_METHODS = frozenset({"execute_to_queue", "__init__"})
_LIVE_EPISODE = "tests/tool_eval/live_episode.py"
_FIELD = "advertised_tools"
_FIELD_RULES = {  # 白名單：每個檔恰好一處、而且是這一種用法
    "ToukaProxy/src/models/context.py": "field",
    "ToukaProxy/src/middleware/layers/tool.py": "subscript-store",
    "ToukaProxy/src/tools/subagent_tools.py": "attribute-store",
    _EXECUTOR_MODULE: "get-read",
    _LIVE_EPISODE: "subscript-store",
}


@pytest.fixture(scope="module")
def production_sources() -> dict[str, str]:
    """全部正式程式（走訪時就剪掉排除的資料夾），只讀原始碼、不匯入、不呼叫 git。"""
    sources: dict[str, str] = {}
    for root in _SCAN_ROOTS:
        for folder, dirnames, filenames in (_ROOT / root).walk():
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in _SKIP_PARTS and not name.endswith(".egg-info")
            )
            for filename in sorted(name for name in filenames if name.endswith(".py")):
                path = folder / filename
                relative = path.relative_to(_ROOT).as_posix()
                sources[relative] = path.read_text(encoding="utf-8-sig")
    return sources


def test_c2_c3_scan_covers_every_production_python_file(production_sources):
    """掃描範圍：ToukaProxy／ToukaMemory 底下所有正式 .py，不只 src。"""
    for rel in (
        "ToukaProxy/plugin_manager.py",
        "ToukaProxy/scripts/__init__.py",
        "ToukaProxy/scripts/manage_backup.py",
        "ToukaMemory/scripts/reembed_memory.py",
        _REACT_CORE,
        "ToukaHeartbeat/awaken.py",
        "ToukaDispatch/server.py",
        "scripts/guard_native_tool_contracts.py",
    ):
        assert rel in production_sources, rel
    assert not [rel for rel in production_sources if _SKIP_PARTS & set(Path(rel).parts)]


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _function_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "<module>"


def test_c2_switch_defaults_on_and_is_keyword_only():
    """甲-3：開關預設開、只能用名稱指定；新入口忘了設定也照樣核對清單。"""
    param = inspect.signature(ToolExecutor.__init__).parameters[_SWITCH]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is True
    assert ToolExecutor(None, {}, {}, {}).enforce_advertised is True


async def test_c2_an_executor_built_without_the_switch_blocks_unlisted_tools(gates):
    handler = AsyncMock(return_value="ran")
    executor = ToolExecutor(None, {"write": handler}, {}, {})
    result, events = await _call(executor, "write", _listed_ctx())
    _blocked(result, events, "permission_denied", "not_advertised")
    handler.assert_not_awaited()


def _scope_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """最近的一層函式、lambda 或類別本體（keyword 寫在 lambda、類別本體裡不算在外層函式裡）."""
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return node
    return None


def _keyword_uses(node: ast.keyword, rel: str, parents: dict[ast.AST, ast.AST]) -> list[str]:
    if node.arg in _PRIVATE_NAMES:
        return [f"keyword {node.arg}"]  # 擋 vars(ex).update(_enforce_advertised=False)
    if node.arg != _SWITCH:
        return []
    value = node.value
    if not (isinstance(value, ast.Constant) and isinstance(value.value, bool)):
        return [f"keyword {_SWITCH} with a non-literal value"]
    scope = _scope_of(node, parents)
    in_debug_execute = (
        rel == _DEBUG
        and isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
        and scope.name == "debug_execute_tool"
        and isinstance(parents.get(scope), ast.Module)
    )
    if value.value is False and not in_debug_execute:
        return [f"keyword {_SWITCH}=False outside debug_execute_tool"]
    return []


def _executor_reference_uses(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[str]:
    """``ToolExecutor`` 只能被直接呼叫：當參數、基底類別、指派對象、取屬性都算違規.

    擋 partial、子類別（含 ``*[ToolExecutor]``）、``setattr``／``type.__setattr__``、換掉類別屬性、
    改綁名稱、交給變數。直接呼叫時不得 ``**`` 展開（值可能來自設定或 JSON）。
    """
    is_ref = (isinstance(node, ast.Name) and node.id == "ToolExecutor") or (
        isinstance(node, ast.Attribute) and node.attr == "ToolExecutor"
    )
    if not is_ref:
        return []
    parent = parents.get(node)
    called = isinstance(parent, ast.Call) and parent.func is node
    if not (called and isinstance(node.ctx, ast.Load)):
        return [f"ToolExecutor used as {type(parent).__name__}, not called"]
    if any(keyword.arg is None for keyword in parent.keywords):
        return ["** unpacking into ToolExecutor"]
    return []


def _executor_class(tree: ast.Module) -> ast.ClassDef | None:
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ToolExecutor"]
    return classes[0] if len(classes) == 1 else None


def _in_executor_init(
    node: ast.AST, parents: dict[ast.AST, ast.AST], cls: ast.ClassDef | None
) -> bool:
    """最內層函式就是模組層 ``class ToolExecutor`` 直接定義的 ``__init__``."""
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return (
                cls is not None
                and isinstance(node, ast.FunctionDef)
                and node.name == "__init__"
                and parents.get(node) is cls
            )
    return False


_GUARDED_METHODS = _CHECK_METHODS | {"__init__", "execute_to_queue"}


def _executor_shape_uses(
    node: ast.AST, parents: dict[ast.AST, ast.AST], cls: ast.ClassDef | None
) -> list[str]:
    """執行器檔：只有一個模組層的 ToolExecutor；核對方法只定義在它身上；關鍵方法與類別不加裝飾器."""
    if isinstance(node, ast.ClassDef) and node.name == "ToolExecutor":
        if node is not cls:
            return ["second or nested class ToolExecutor"]
        if node.decorator_list:
            return ["decorated class ToolExecutor"]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "ToolExecutor":
        return ["function named ToolExecutor"]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        owned = cls is not None and parents.get(node) is cls
        if node.name in _CHECK_METHODS and not owned:
            return [f"{node.name} defined outside ToolExecutor"]
        if owned and node.name in _GUARDED_METHODS and node.decorator_list:
            return [f"decorated ToolExecutor.{node.name}"]
    if isinstance(node, ast.Attribute):
        written = not isinstance(node.ctx, ast.Load)
        if node.attr == _PRIVATE_SWITCH and written and not _in_executor_init(node, parents, cls):
            return [f"{_PRIVATE_SWITCH} written outside ToolExecutor.__init__"]
        if node.attr in _CHECK_METHODS and not isinstance(node.ctx, ast.Load):
            return [f"attribute {node.attr} ({type(node.ctx).__name__})"]
    name_written = isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load)
    if name_written and node.id in _CHECK_METHODS:
        return [f"name {node.id} assigned"]  # 類別本體裡 _offer_denial = lambda ...
    return []


def _outside_executor_uses(node: ast.AST) -> list[str]:
    """執行器以外：私有名稱、核對方法不得以任何形式出現；不得另寫名為 ToolExecutor 的類別或函式."""
    if isinstance(node, ast.Name) and node.id in _PRIVATE_NAMES:
        return [f"name {node.id}"]
    if isinstance(node, ast.Attribute) and node.attr in _PRIVATE_NAMES:
        return [f"attribute {node.attr}"]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if node.name in _PRIVATE_NAMES:
            return [f"definition {node.name}"]
        if node.name == "ToolExecutor":
            return ["stand-in named ToolExecutor"]
    if isinstance(node, ast.arg) and node.arg in _PRIVATE_NAMES:
        return [f"parameter {node.arg}"]
    if isinstance(node, ast.alias):
        if {node.name, node.asname} & _PRIVATE_NAMES:
            return [f"import {node.name}"]
        if node.name == "ToolExecutor" and node.asname:
            return [f"import ToolExecutor as {node.asname}"]
    return []


def _switch_uses(
    node: ast.AST, rel: str, parents: dict[ast.AST, ast.AST], cls: ast.ClassDef | None
) -> list[str]:
    """只有 debug_execute_tool 能用字面 False 關開關；開關與核對方法不得被改寫、換掉或繞過."""
    if isinstance(node, ast.Constant) and node.value in _PROTECTED_STRINGS:
        return [f"string {node.value!r}"]  # 擋 **{...}、setattr、getattr、globals()[...]
    if isinstance(node, ast.keyword):
        return _keyword_uses(node, rel, parents)
    found = _executor_reference_uses(node, parents)
    if isinstance(node, ast.Attribute) and node.attr == "__kwdefaults__":
        found.append("attribute __kwdefaults__")  # 擋執行時把預設值翻成 False
    if (
        isinstance(node, ast.Attribute)
        and node.attr in _REPLACEABLE_METHODS
        and not isinstance(node.ctx, ast.Load)
    ):
        found.append(f"attribute {node.attr} replaced")  # 擋從實例取得類別再換掉
    renamed_in = isinstance(node, ast.alias) and node.asname == "ToolExecutor"
    if renamed_in and node.name != "ToolExecutor":
        found.append(f"import {node.name} as ToolExecutor")  # 擋把別的類別改名成執行器
    if isinstance(node, ast.arg) and node.arg == "ToolExecutor":
        found.append("parameter named ToolExecutor")
    attr_written = isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load)
    if attr_written and node.attr == _SWITCH:
        found.append(f"attribute {_SWITCH} ({type(node.ctx).__name__})")
    if rel == _EXECUTOR_MODULE:
        return found + _executor_shape_uses(node, parents, cls)
    return found + _outside_executor_uses(node)


def _switch_findings(sources: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for rel, source in sources.items():
        tree = ast.parse(source)
        parents = _parents(tree)
        cls = _executor_class(tree) if rel == _EXECUTOR_MODULE else None
        for node in ast.walk(tree):
            problems.extend(f"{rel}: {item}" for item in _switch_uses(node, rel, parents, cls))
    return problems


def test_c2_only_debug_execute_turns_the_switch_off(production_sources):
    """預設開以後，要守的只剩「誰能把它關掉」：只有人按的 /debug/execute，而且寫成字面 False."""
    assert _switch_findings(production_sources) == []


_IMPORT = "from src.services.tool_executor import ToolExecutor\n"
_C2_ELSEWHERE = "ToukaProxy/src/services/elsewhere.py"
_CANONICAL_REACT_CORE = (
    _IMPORT + "async def run_react_loop():\n"
    "    ex = ToolExecutor(1, 2, 3, 4, enforce_advertised=True)\n"
    "    if not ex.enforce_advertised:\n"
    "        raise RuntimeError\n"
)
_CANONICAL_DEBUG = (
    _IMPORT + "async def debug_execute_tool():\n"
    "    return ToolExecutor(1, 2, 3, 4, enforce_advertised=False)\n"
)


@pytest.mark.parametrize(
    ("rel", "source"),
    [
        (
            _C2_ELSEWHERE,
            _IMPORT + "from functools import partial\n"
            "build = partial(ToolExecutor, enforce_advertised=False)\n",
        ),
        (
            _C2_ELSEWHERE,
            _IMPORT + "class Loose(ToolExecutor):\n"
            "    def __init__(self, *args):\n"
            "        super().__init__(*args, enforce_advertised=False)\n",
        ),
        (
            _REACT_CORE,
            _IMPORT + "async def run_react_loop(flag):\n"
            "    ToolExecutor(1, 2, 3, 4, enforce_advertised=flag)\n",
        ),
        (
            _REACT_CORE,
            _IMPORT + "async def run_react_loop():\n"
            "    ToolExecutor(1, 2, 3, 4, enforce_advertised=False)\n",
        ),
        (
            _DEBUG,
            _CANONICAL_DEBUG + "def other():\n"
            "    return ToolExecutor(1, 2, 3, 4, enforce_advertised=False)\n",
        ),
        (_C2_ELSEWHERE, "opts = dict(enforce_advertised=False)\n"),
        (
            _C2_ELSEWHERE,
            _IMPORT + "ex = ToolExecutor(1, 2, 3, 4, **{'enforce_advertised': False})\n",
        ),
        (_C2_ELSEWHERE, "setattr(ex, '_enforce_advertised', False)\n"),
        (_C2_ELSEWHERE, "vars(ex)['_enforce_advertised'] = False\n"),
        (_C2_ELSEWHERE, "ex._enforce_advertised = False\n"),
        (_C2_ELSEWHERE, "flag = ex._enforce_advertised\n"),
        (_C2_ELSEWHERE, "ex.enforce_advertised = False\n"),
        (_C2_ELSEWHERE, "del ex.enforce_advertised\n"),
        (_C2_ELSEWHERE, "class Loose:\n    _enforce_advertised = False\n"),
        (_C2_ELSEWHERE, "vars(ex).update(_enforce_advertised=False)\n"),
        (_C2_ELSEWHERE, "ex.__dict__.update(_enforce_advertised=False)\n"),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n"
            "    @classmethod\n"
            "    def unchecked(cls, *registries):\n"
            "        return cls(*registries, enforce_advertised=False)\n",
        ),
        (
            _EXECUTOR_MODULE,
            "def unchecked(*registries):\n"
            "    return ToolExecutor(*registries, enforce_advertised=False)\n",
        ),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n"
            "    def loosen(self):\n"
            "        self._enforce_advertised = False\n",
        ),
        (
            _C2_ELSEWHERE,
            _IMPORT + "class Lenient(ToolExecutor):\n"
            "    def _offer_denial(self, *args, **kwargs):\n"
            "        return None\n",
        ),
        (_C2_ELSEWHERE, _IMPORT + "ToolExecutor._user_scope_denial = lambda *a, **k: None\n"),
        (_C2_ELSEWHERE, _IMPORT + "setattr(ToolExecutor, '_offer_denial', lambda *a, **k: None)\n"),
        (
            _C2_ELSEWHERE,
            _IMPORT + "def build(raw, *r):\n    return ToolExecutor(*r, **json.loads(raw))\n",
        ),
        (
            _C2_ELSEWHERE,
            "from src.services import tool_executor\n"
            "def build(opts, *r):\n    return tool_executor.ToolExecutor(*r, **opts)\n",
        ),
        (_C2_ELSEWHERE, "from src.services.tool_executor import ToolExecutor as Runner\n"),
        (
            _C2_ELSEWHERE,
            _IMPORT + "make = ToolExecutor\n"
            "ex = make(1, 2, 3, 4, **{'enforce_advertised': False})\n",
        ),
        (
            _C2_ELSEWHERE,
            _IMPORT + "d = ToolExecutor.__init__.__kwdefaults__\n"
            "d.update(dict.fromkeys(d, False))\n",
        ),
        (_C2_ELSEWHERE, "def build(_enforce_advertised=False):\n    return None\n"),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n    pass\n"
            "class UncheckedToolExecutor(ToolExecutor):\n"
            "    def _offer_denial(self, *args, **kwargs):\n"
            "        return None\n",
        ),
        (
            _EXECUTOR_MODULE,
            "class Loosener:\n"
            "    def __init__(self, ex):\n"
            "        ex._enforce_advertised = False\n",
        ),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n    pass\n"
            "ToolExecutor._offer_denial = lambda *a, **k: None\n",
        ),
        (
            _C2_ELSEWHERE,
            _IMPORT + "import functools\n"
            "def build(raw):\n    return functools.partial(ToolExecutor, **json.loads(raw))\n",
        ),
        (
            _C2_ELSEWHERE,
            _IMPORT + "class Passthrough(ToolExecutor):\n"
            "    def __init__(self, *a, **k):\n"
            "        super().__init__(*a, **k)\n",
        ),
        (
            _C2_ELSEWHERE,
            _IMPORT + "d = getattr(ToolExecutor.__init__, '__kwdefaults__')\n",
        ),
        (
            _C2_ELSEWHERE,
            _IMPORT + "import functools\n"
            "def loosen(raw):\n"
            "    ToolExecutor.__init__ = functools.partialmethod(\n"
            "        ToolExecutor.__init__, **json.loads(raw)\n"
            "    )\n",
        ),
        (_C2_ELSEWHERE, _IMPORT + "setattr(ToolExecutor, '__init__', lambda *a, **k: None)\n"),
        (_C2_ELSEWHERE, _IMPORT + "delattr(ToolExecutor, 'execute_to_queue')\n"),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n"
            "    def loosen(self):\n"
            "        def __init__():\n"
            "            self._enforce_advertised = False\n"
            "        __init__()\n",
        ),
        (_C2_ELSEWHERE, "opts = {'enforce_advertised': False}\n"),
        (
            _C2_ELSEWHERE,
            "def loosen(executor_cls):\n    return executor_cls.__init__.__kwdefaults__\n",
        ),
        (
            _C2_ELSEWHERE,
            _IMPORT + "import builtins\n"
            "builtins.setattr(ToolExecutor, 'execute_to_queue', lambda *a, **k: None)\n",
        ),
        (
            _C2_ELSEWHERE,
            _IMPORT + "type.__setattr__(ToolExecutor, 'execute_to_queue', lambda *a, **k: None)\n",
        ),
        (
            _C2_ELSEWHERE,
            _IMPORT + "object.__setattr__("
            "ToolExecutor, 'execute_to_queue', lambda *a, **k: None)\n",
        ),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n    pass\n"
            "ToolExecutor.execute_to_queue = lambda *a, **k: None\n",
        ),
        (_C2_ELSEWHERE, _IMPORT + "class Unchecked(*[ToolExecutor]):\n    pass\n"),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n    pass\n"
            "class Unchecked(*[ToolExecutor]):\n    pass\n",
        ),
        (_C2_ELSEWHERE, _IMPORT + "_Base = ToolExecutor\n"),
        (_C2_ELSEWHERE, _IMPORT + "class Open(ToolExecutor if True else object):\n    pass\n"),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n    pass\n"
            "def unchecked_executor():\n"
            "    class ToolExecutor:\n"
            "        pass\n"
            "    return ToolExecutor\n",
        ),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n    pass\n"
            "def unchecked_executor():\n"
            "    class Stand:\n"
            "        def __init__(self):\n"
            "            self._enforce_advertised = False\n"
            "    return Stand\n",
        ),
        (_EXECUTOR_MODULE, "class ToolExecutor:\n    pass\nclass ToolExecutor:\n    pass\n"),
        (_EXECUTOR_MODULE, "def ToolExecutor(*args):\n    return None\n"),
        (
            _EXECUTOR_MODULE,
            "def _mute(fn):\n    return fn\n"
            "class ToolExecutor:\n"
            "    @_mute\n"
            "    def _offer_denial(self):\n"
            "        return 'blocked'\n",
        ),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n"
            "    @staticmethod\n"
            "    async def execute_to_queue(*args):\n"
            "        return None\n",
        ),
        (_EXECUTOR_MODULE, "@decorate\nclass ToolExecutor:\n    pass\n"),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n    pass\n"
            "class Helper:\n"
            "    def _user_scope_denial(self):\n"
            "        return None\n",
        ),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n"
            "    def __init__(self):\n"
            "        self._offer_denial = lambda *a: None\n",
        ),
        (_C2_ELSEWHERE, "class Helper:\n    def _offer_denial(self):\n        return None\n"),
        (_C2_ELSEWHERE, "class _Open:\n    pass\nToolExecutor = _Open\n"),
        (_C2_ELSEWHERE, "globals()['ToolExecutor'] = object\n"),
        (_C2_ELSEWHERE, "import src.services.tool_executor as te\nte.ToolExecutor = object\n"),
        (_C2_ELSEWHERE, "class ToolExecutor:\n    enforce_advertised = True\n"),
        (
            _DEBUG,
            _IMPORT + "async def debug_execute_tool():\n"
            "    make = lambda *a: ToolExecutor(*a, enforce_advertised=False)\n"
            "    return make\n",
        ),
        (
            _DEBUG,
            _IMPORT + "async def debug_execute_tool():\n"
            "    class Box:\n"
            "        ex = ToolExecutor(1, 2, 3, 4, enforce_advertised=False)\n"
            "    return Box\n",
        ),
        (
            _DEBUG,
            _IMPORT + "def wrapper():\n"
            "    async def debug_execute_tool():\n"
            "        return ToolExecutor(1, 2, 3, 4, enforce_advertised=False)\n"
            "    return debug_execute_tool\n",
        ),
        (
            _C2_ELSEWHERE,
            _IMPORT + "async def _run(self, *args, **kwargs):\n    return None\n"
            "type(ToolExecutor(None, {}, {}, {})).execute_to_queue = _run\n",
        ),
        (_C2_ELSEWHERE, "def loosen(ex, f):\n    ex.__class__.__init__ = f\n"),
        (_C2_ELSEWHERE, "def loosen(ex):\n    return getattr(type(ex), 'execute_to_queue')\n"),
        (_C2_ELSEWHERE, "from src.services.lenient import Open as ToolExecutor\n"),
        (_C2_ELSEWHERE, "def build(ToolExecutor):\n    pass\n"),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n"
            "    def _user_scope_denial(self):\n"
            "        return 'blocked'\n"
            "    _user_scope_denial = lambda self, *a, **k: None\n",
        ),
    ],
    ids=[
        "partial-false",
        "subclass-super-false",
        "react-core-variable",
        "react-core-false",
        "debug-other-function-false",
        "dict-keyword-false",
        "dict-unpack-string",
        "setattr-string",
        "vars-string",
        "private-attribute-write",
        "private-attribute-read",
        "property-write",
        "property-delete",
        "class-attribute",
        "vars-update-keyword",
        "dict-update-keyword",
        "executor-classmethod-factory",
        "executor-module-factory",
        "executor-other-method-writes",
        "subclass-overrides-check",
        "monkeypatched-check",
        "setattr-check-string",
        "json-unpacked-into-executor",
        "options-unpacked-into-module-attribute",
        "renamed-import",
        "variable-call-unpack-string",
        "kwdefaults",
        "private-parameter",
        "executor-file-subclass",
        "executor-file-other-class-writes",
        "executor-file-monkeypatch",
        "partial-unpack",
        "passthrough-subclass",
        "kwdefaults-string",
        "init-replaced-with-partialmethod",
        "setattr-on-class",
        "delattr-on-class",
        "nested-init-in-another-method",
        "switch-name-in-dict-literal",
        "kwdefaults-via-variable",
        "builtins-setattr",
        "type-setattr",
        "object-setattr",
        "executor-file-class-attribute",
        "starred-base",
        "executor-file-starred-base",
        "class-handed-to-variable",
        "conditional-base",
        "executor-file-nested-class",
        "executor-file-stand-in-init",
        "executor-file-second-class",
        "executor-file-function-named-executor",
        "decorated-check",
        "decorated-execute",
        "decorated-class",
        "executor-file-check-elsewhere",
        "executor-file-instance-check-replaced",
        "check-defined-elsewhere",
        "name-rebound",
        "globals-rebound",
        "module-attribute-rebound",
        "stand-in-class",
        "debug-lambda-false",
        "debug-class-body-false",
        "debug-nested-function-false",
        "execute-replaced-via-instance-type",
        "init-replaced-via-instance-class",
        "execute-name-string",
        "other-class-imported-as-executor",
        "parameter-named-executor",
        "executor-file-check-name-assigned",
    ],
)
def test_c2_scanner_catches_every_way_to_turn_the_switch_off(rel, source):
    assert _switch_findings({rel: source}) != []


@pytest.mark.parametrize(
    ("rel", "source"),
    [
        (
            _C2_ELSEWHERE,
            '"""Docs mention ToolExecutor and enforce_advertised=False."""\n'
            'REASON = "advertised_tools_unavailable"\n',
        ),
        (_REACT_CORE, _CANONICAL_REACT_CORE),
        (_DEBUG, _CANONICAL_DEBUG),
        (_C2_ELSEWHERE, _IMPORT + "def build():\n    return ToolExecutor(1, 2, 3, 4)\n"),
        (
            _C2_ELSEWHERE,
            _IMPORT + "def build():\n"
            "    return ToolExecutor(1, 2, 3, 4, enforce_advertised=True)\n",
        ),
        (_C2_ELSEWHERE, "def check(ex):\n    return ex.enforce_advertised\n"),
        (
            _EXECUTOR_MODULE,
            "class ToolExecutor:\n"
            "    def __init__(self, *, enforce_advertised=True):\n"
            "        self._enforce_advertised = enforce_advertised\n"
            "    @property\n"
            "    def enforce_advertised(self):\n"
            "        return self._enforce_advertised\n"
            "    def _offer_denial(self, name):\n"
            "        return None\n"
            "    def run(self, name):\n"
            "        return self._offer_denial(name) if self._enforce_advertised else None\n",
        ),
    ],
    ids=[
        "docstring-and-reason-code",
        "canonical-react-core",
        "canonical-debug",
        "default-construction-elsewhere",
        "explicit-true-elsewhere",
        "property-read-elsewhere",
        "canonical-executor",
    ],
)
def test_c2_scanner_accepts_the_default_and_the_two_explicit_switches(rel, source):
    assert _switch_findings({rel: source}) == []


def _ctx_kind(context: ast.expr_context) -> str:
    return {ast.Store: "store", ast.Del: "del"}.get(type(context), "load")


def _field_uses(source: str) -> list[str]:
    """識別字或整串字串 advertised_tools 的每一處用法。"""
    tree = ast.parse(source)
    parents = _parents(tree)
    uses: list[str] = []
    for node in ast.walk(tree):
        parent = parents.get(node)
        if isinstance(node, ast.Constant) and node.value == _FIELD:
            if isinstance(parent, ast.Subscript) and parent.slice is node:
                uses.append(f"subscript-{_ctx_kind(parent.ctx)}")
            elif (
                isinstance(parent, ast.Call)
                and parent.args
                and parent.args[0] is node
                and isinstance(parent.func, ast.Attribute)
                and parent.func.attr == "get"
            ):
                uses.append("get-read")
            else:
                uses.append("string")
        elif isinstance(node, ast.Attribute) and node.attr == _FIELD:
            uses.append(f"attribute-{_ctx_kind(node.ctx)}")
        elif isinstance(node, ast.Name) and node.id == _FIELD:
            in_class = isinstance(parents.get(parent), ast.ClassDef)
            is_field = isinstance(parent, ast.AnnAssign) and parent.target is node and in_class
            uses.append("field" if is_field else "name")
        elif isinstance(node, ast.keyword) and node.arg == _FIELD:
            uses.append("keyword")
        elif isinstance(node, ast.arg) and node.arg == _FIELD:
            uses.append("parameter")
        elif isinstance(node, ast.alias) and _FIELD in (node.name, node.asname):
            uses.append("import")
        elif (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == _FIELD
        ):
            uses.append("definition")
    return uses


def _field_findings(sources: dict[str, str]) -> list[str]:
    findings = []
    for rel in sorted(sources.keys() | _FIELD_RULES.keys()):
        uses = _field_uses(sources[rel]) if rel in sources else ["<missing file>"]
        allowed = _FIELD_RULES.get(rel)
        if allowed is None and uses:
            findings.append(f"{rel}: {uses}")
        elif allowed is not None and uses != [allowed]:
            findings.append(f"{rel}: {uses}（應恰好是 [{allowed}]）")
    return findings


def test_c3_only_whitelisted_files_touch_the_list_field(production_sources):
    sources = {
        **production_sources,
        _LIVE_EPISODE: (_ROOT / _LIVE_EPISODE).read_text(encoding="utf-8-sig"),
    }
    assert _field_findings(sources) == []


_ELSEWHERE = "ToukaProxy/src/services/elsewhere.py"


@pytest.mark.parametrize(
    ("rel", "source"),
    [
        (_ELSEWHERE, "ctx['advertised_tools'] = x\n"),
        (_ELSEWHERE, "ctx.update({'advertised_tools': x})\n"),
        (_ELSEWHERE, "setattr(ctx, 'advertised_tools', x)\n"),
        (_ELSEWHERE, "ctx = ctx.model_copy(update={'advertised_tools': x})\n"),
        (_ELSEWHERE, "ctx = PipelineContext(advertised_tools=x)\n"),
        (_ELSEWHERE, "x = getattr(ctx, 'advertised_tools')\n"),
        (_ELSEWHERE, "x = ctx.get('advertised_tools')\n"),
        (_ELSEWHERE, "ctx.advertised_tools = x\n"),
        (_ELSEWHERE, "def build(advertised_tools):\n    return advertised_tools\n"),
        (_EXECUTOR_MODULE, "ctx['advertised_tools'] = x\n"),
        (
            "ToukaProxy/src/middleware/layers/tool.py",
            "ctx['advertised_tools'] = x\nctx['advertised_tools'] = y\n",
        ),
        ("ToukaProxy/src/tools/subagent_tools.py", "x = ctx.advertised_tools\n"),
    ],
    ids=[
        "subscript-write",
        "update-dict",
        "setattr",
        "model-copy-update",
        "constructor-keyword",
        "getattr",
        "get-outside-executor",
        "attribute-write",
        "parameter",
        "executor-writes",
        "layer-writes-twice",
        "subagent-reads",
    ],
)
def test_c3_scanner_catches_every_writer(rel, source):
    # 只看這一個檔（其他白名單檔不在 sources 裡，會另外報 missing，這裡濾掉）
    findings = [item for item in _field_findings({rel: source}) if item.startswith(rel)]
    assert findings != []


def test_c3_scanner_ignores_longer_strings_and_docstrings():
    source = '"""Mentions advertised_tools in prose."""\nREASON = "advertised_tools_unavailable"\n'
    assert _field_uses(source) == []
