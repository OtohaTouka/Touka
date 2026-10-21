"""dep1：呼叫 MCP 工具當下核對伺服器的工具清單（縱深防禦；ADR-0203 dep1 補記）。

Touka 驗過的 schema 與 SDK 驗證工具結果時用的不是同一份（GHSA-rwrf-2pqf-9j8j）：伺服器可以
在呼叫當下換掉 schema、或乾脆不列這個工具。manager 在呼叫前重列，以下情況一律拒絕、不呼叫 SDK：

- 同名工具有任何隔離紀錄（``McpIcon:`` 開頭的除外，icon 問題只記紀錄）→ ``permission_denied``；
- 同名不只一筆（SDK 只留最後一筆 schema）→ 同上；
- 當次清單找不到、伺服器不支援列工具 → ``resource_not_found``。

拒絕不是上游故障：不計入斷路器、不寫健康頁的 last_error，只記 warning。全部走 conftest 沙盒與替身。

模型給的參數放進標頭後超過上限（單值 2048 bytes、總和 8192 bytes）同樣是拒絕：
回 ``validation_error``，同一工具錯 3 次，同一台伺服器的其他工具仍叫得到。
拒絕對斷路器中立：不記失敗，也不記成功（清單可能走快取、根本沒碰到伺服器，
記成功會把真正的失敗計數歸零、把半開的斷路器關回去）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from src.core.circuit_breaker import BreakerNeutralError, CircuitBreaker, CircuitState
from src.models.advertised_tools import AdvertisedTools
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.protocols.manager import McpToolCatalogRefusal, ProtocolError, ProtocolManager
from src.protocols.mcp.canonical import McpOperationException, McpRequestContext
from src.protocols.mcp.handles import LegacyMcpHandle
from src.protocols.mcp.icons import McpIconService
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor

_OWNER = AgentIdentity.for_role(agent_id="CatalogOwner", role="Owner")
_EXTERNAL_OUTPUT = {"$ref": "http://ghsa-rwrf-2pqf-9j8j.invalid/schema.json"}
_HEADER_INPUT = {
    "type": "object",
    "properties": {"token": {"type": "string", "x-mcp-header": "Token"}},
}
_ROUTED_INPUT = {
    "type": "object",
    "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
}
_FIVE_HEADERS_INPUT = {
    "type": "object",
    "properties": {f"r{i}": {"type": "string", "x-mcp-header": f"R{i}"} for i in range(5)},
}
_MARKER = "PLANTEDVALUE"
_OVERSIZED = [
    {"region": _MARKER * 200},  # 單值 2400 bytes
    {"region": "區" * 600},  # 非 ASCII 會 base64 包起來，600 個中文字就超過
    {f"r{i}": _MARKER * 150 for i in range(5)},  # 每個 1800 bytes，總和超過 8192
]
_OVERSIZED_IDS = ["value", "cjk-value", "aggregate"]


def _deep_input(levels: int = 40) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object"}
    for _ in range(levels):
        schema = {"type": "object", "properties": {"x": schema}}
    return schema


def _tool(name: str = "probe", **overrides: Any) -> SimpleNamespace:
    values = {
        "name": name,
        "title": None,
        "description": name,
        "input_schema": {"type": "object"},
        "output_schema": None,
        "annotations": None,
        "icons": [],
        "meta": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Session:
    protocol_version = "2026-07-28"
    server_capabilities = SimpleNamespace(
        tools=SimpleNamespace(list_changed=False), resources=None, prompts=None
    )

    def __init__(self, tools: list[SimpleNamespace], *, list_error: Exception | None = None):
        self.tools = tools
        self.list_error = list_error
        self.calls: list[str] = []

    async def list_tools(self, *, cursor: str | None = None) -> Any:
        if self.list_error is not None:
            raise self.list_error
        return SimpleNamespace(tools=list(self.tools), next_cursor=None)

    async def call_tool(self, name: str, _arguments: dict[str, Any]) -> Any:
        self.calls.append(name)
        if name == "boom":
            raise ConnectionError
        return SimpleNamespace(
            content=[], structured_content={"ok": True}, is_error=False, meta=None
        )


class _NoListSession(_Session):
    list_tools = None  # type: ignore[assignment]


class _NoToolsCapabilitySession(_Session):
    server_capabilities = SimpleNamespace(tools=None, resources=None, prompts=None)


def _context() -> McpRequestContext:
    return McpRequestContext(
        request_id="catalog-request",
        session_id="catalog-session",
        identity=_OWNER,
        human_principal_id="human-catalog",
        installation_id="srv",
        auth_level="Manual",
        origin="foreground",
    )


def _manager(session: Any, *, url: str | None = None) -> ProtocolManager:
    registry = ProtocolRegistry("__missing_call_time_catalog__.json")
    config = ProtocolServerConfig(url=url) if url else ProtocolServerConfig(command="echo")
    registry.servers = {"srv": config}
    manager = ProtocolManager(registry)
    manager.handle_pool.put(
        manager.handle_key_for("srv", _context()), LegacyMcpHandle(session=session)
    )
    return manager


async def _refusal(manager: ProtocolManager, tool: str = "probe") -> McpToolCatalogRefusal:
    with pytest.raises(McpToolCatalogRefusal) as caught:
        await manager.call_tool_on_server("srv", tool, {}, request_context=_context())
    return caught.value


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"output_schema": _EXTERNAL_OUTPUT}, "McpSchemaReferenceError"),
        (
            {
                "input_schema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                }
            },
            "McpSchemaDialectError",
        ),
        ({"input_schema": _deep_input()}, "McpSchemaLimitError"),
        ({"input_schema": {"type": "array"}}, "McpSchemaShapeError"),
    ],
    ids=["reference", "dialect", "limit", "shape"],
)
async def test_quarantined_schema_is_refused_before_the_sdk_call(overrides, code):
    session = _Session([_tool(**overrides)])
    manager = _manager(session)
    catalog = await manager.list_server_tool_descriptors("srv", request_context=_context())
    assert [issue.code for issue in catalog.issues] == [code]

    refusal = await _refusal(manager)

    assert (refusal.quarantined, refusal.reason) == (True, "mcp_tool_quarantined")
    assert session.calls == []


async def test_header_quarantine_is_a_refusal_not_an_upstream_error():
    session = _Session([_tool(input_schema=_HEADER_INPUT)])
    refusal = await _refusal(_manager(session))
    assert (refusal.quarantined, refusal.reason) == (True, "mcp_tool_quarantined")
    assert session.calls == []


@pytest.mark.parametrize(
    "tools",
    [[_tool(), _tool(output_schema=_EXTERNAL_OUTPUT)], [_tool(), _tool()]],
    ids=["one-of-two-quarantined", "two-clean-duplicates"],
)
async def test_duplicate_names_are_refused(tools):
    session = _Session(tools)
    refusal = await _refusal(_manager(session))
    assert (refusal.quarantined, refusal.reason) == (True, "mcp_tool_quarantined")
    assert session.calls == []


async def test_tool_missing_from_the_current_list_is_refused():
    session = _Session([_tool("other")])
    refusal = await _refusal(_manager(session))
    assert (refusal.quarantined, refusal.reason) == (False, "mcp_tool_not_listed")
    assert session.calls == []


@pytest.mark.parametrize(
    "session",
    [
        _NoToolsCapabilitySession([_tool()]),
        _NoListSession([_tool()]),
        _Session([_tool()], list_error=MCPError(-32601, "Method not found")),
    ],
    ids=["no-tools-capability", "no-list-method", "method-not-found"],
)
async def test_server_that_cannot_list_tools_is_refused(session):
    refusal = await _refusal(_manager(session))
    assert (refusal.quarantined, refusal.reason) == (False, "mcp_tool_not_listed")
    assert session.calls == []


async def _failing_icon_fetch(_url: str, _headers: dict[str, str]) -> tuple[int, str, bytes]:
    return 500, "image/png", b""


@pytest.mark.parametrize(
    ("url", "icon", "code"),
    [
        (None, "https://cdn.example.test/icon.png", "McpIcon:cross_origin"),
        (
            "https://mcp.example.test/mcp",
            "https://mcp.example.test/icon.png",
            "McpIcon:fetch_failed",
        ),
    ],
    ids=["cross-origin-icon", "icon-fetch-failed"],
)
async def test_icon_only_issues_never_block_the_call(url, icon, code):
    """陽性對照：icon 問題只記紀錄，工具照常叫得到。"""
    session = _Session([_tool(icons=[{"src": icon, "mimeType": "image/png"}])])
    manager = _manager(session, url=url)
    manager._icon_service = McpIconService(fetch=_failing_icon_fetch)
    catalog = await manager.list_server_tool_descriptors("srv", request_context=_context())
    assert [issue.code for issue in catalog.issues] == [code]

    outcome = await manager.call_tool_on_server("srv", "probe", {}, request_context=_context())

    assert outcome.is_error is False
    assert session.calls == ["probe"]


async def test_refusals_do_not_trip_the_breaker_or_record_errors():
    session = _Session([_tool("other"), _tool(input_schema=_HEADER_INPUT, name="header")])
    manager = _manager(session)
    for tool in ("probe", "header", "probe", "header"):
        await _refusal(manager, tool)

    breaker = manager._breaker_for(manager.handle_key_for("srv", _context()))
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0
    assert manager._installations["srv"].last_error is None
    assert manager._server_meta.get("srv", {}).get("last_error") is None
    assert session.calls == []


async def test_relisting_failure_still_fails_closed():
    session = _Session([_tool()], list_error=RuntimeError("listing exploded"))
    with pytest.raises(McpOperationException):
        await _manager(session).call_tool_on_server("srv", "probe", {}, request_context=_context())
    assert session.calls == []


# --- 模型給的標頭參數超過上限 -------------------------------------------------


def _routed_session() -> _Session:
    return _Session(
        [
            _tool("routed", input_schema=_ROUTED_INPUT),
            _tool("fanout", input_schema=_FIVE_HEADERS_INPUT),
            _tool(),
            _tool("boom"),
        ]
    )


async def _call(manager: ProtocolManager, tool: str, arguments: dict[str, Any]) -> Any:
    return await manager.call_tool_on_server("srv", tool, arguments, request_context=_context())


async def _argument_refusal(manager: ProtocolManager, arguments: dict[str, Any]) -> ProtocolError:
    tool = "fanout" if "r0" in arguments else "routed"
    with pytest.raises(ProtocolError) as caught:
        await _call(manager, tool, arguments)
    assert caught.value.reason == "mcp_header_argument_invalid"
    return caught.value


@pytest.mark.parametrize("arguments", _OVERSIZED, ids=_OVERSIZED_IDS)
async def test_oversized_header_argument_leaves_other_tools_callable(arguments, caplog):
    session = _routed_session()
    manager = _manager(session)
    breaker = manager._breaker_for(manager.handle_key_for("srv", _context()))
    for _ in range(3):
        refusal = await _argument_refusal(manager, arguments)

    outcome = await _call(manager, "probe", {})

    assert outcome.is_error is False
    assert session.calls == ["probe"]  # 壞參數一次都沒送出
    assert (breaker.state, breaker.failure_count) == (CircuitState.CLOSED, 0)
    assert manager._installations["srv"].last_error is None
    assert _MARKER not in str(refusal) and "區" not in str(refusal)
    assert _MARKER not in caplog.text and "區" not in caplog.text


async def test_value_refusal_names_the_header():
    refusal = await _argument_refusal(_manager(_routed_session()), _OVERSIZED[0])
    assert "Mcp-Param-Region" in str(refusal)


async def test_header_arguments_within_limits_are_sent():
    """陽性對照：上限內照常呼叫。"""
    session = _routed_session()
    outcome = await _call(_manager(session), "routed", {"region": "eu-west"})
    assert outcome.is_error is False
    assert session.calls == ["routed"]


async def test_call_time_policy_failure_is_not_reported_as_an_argument_error(monkeypatch):
    """只有參數超限才轉成參數錯誤：呼叫開始時沒核准、列清單時已核准（競態）不改分類。"""
    session = _Session([_tool(input_schema=_HEADER_INPUT)])
    manager = _manager(session)
    approved = ProtocolServerConfig(command="echo", approved_header_tools=["probe"])
    ensure_handle = manager._ensure_handle

    async def _approve_then_ensure(*args: Any, **kwargs: Any) -> Any:
        manager.registry.servers["srv"] = approved
        return await ensure_handle(*args, **kwargs)

    monkeypatch.setattr(manager, "_ensure_handle", _approve_then_ensure)
    with pytest.raises(McpOperationException) as caught:
        await _call(manager, "probe", {"token": "short"})

    assert "owner approval" in caught.value.error.message
    assert session.calls == []


async def test_refusals_neither_count_nor_reset_failures():
    """2 次真的失敗＋兩種拒絕＋1 次失敗 → 熔斷；拒絕不能把失敗計數歸零。"""
    manager = _manager(_routed_session())
    breaker = manager._breaker_for(manager.handle_key_for("srv", _context()))
    for _ in range(2):
        with pytest.raises(McpOperationException):
            await _call(manager, "boom", {})

    await _argument_refusal(manager, _OVERSIZED[0])
    assert (breaker.state, breaker.failure_count) == (CircuitState.CLOSED, 2)
    await _refusal(manager, "missing")
    assert (breaker.state, breaker.failure_count) == (CircuitState.CLOSED, 2)

    with pytest.raises(McpOperationException):
        await _call(manager, "boom", {})
    assert breaker.state == CircuitState.OPEN


@pytest.mark.parametrize("kind", ["argument", "catalog"])
async def test_refusal_does_not_close_a_half_open_breaker(kind):
    manager = _manager(_routed_session())
    breaker = manager._breaker_for(manager.handle_key_for("srv", _context()))
    breaker.state, breaker.failure_count = CircuitState.HALF_OPEN, 3

    if kind == "argument":
        await _argument_refusal(manager, _OVERSIZED[0])
    else:
        await _refusal(manager, "missing")

    assert (breaker.state, breaker.failure_count) == (CircuitState.HALF_OPEN, 3)


# --- 真 ToolExecutor＋真 manager：錯誤分類（禁令 59） -------------------------


@dataclass
class _Allow:
    effect: str = "ALLOW"
    reason: str = "test"


@pytest.fixture
def mcp_allowed(monkeypatch):
    """角色權限與 PaC 都放行：證明擋下的是呼叫當下的清單核對。"""
    monkeypatch.setattr(permission_service, "is_mcp_tool_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(permission_service, "evaluate_policy", lambda _context: _Allow())


async def _execute(
    manager: ProtocolManager,
    *,
    enforce: bool,
    tool: str = "probe",
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ctx = PipelineContext(
        identity=_OWNER,
        agent_id=_OWNER.agent_id,
        auth_level="YOLO",
        advertised_tools=AdvertisedTools(mcp=frozenset({("srv", tool)})),
    )
    executor = ToolExecutor(manager, {}, {}, {}, enforce_advertised=enforce)
    queue: asyncio.Queue = asyncio.Queue()
    result = await executor.execute_to_queue(
        f"srv__{tool}", arguments or {}, "req-catalog", ctx, queue, call_id="c1"
    )
    return json.loads(result)


@pytest.mark.parametrize("enforce", [True, False], ids=["model-loop", "debug-execute"])
@pytest.mark.parametrize(
    ("tools", "error_class", "reason"),
    [
        ([_tool(output_schema=_EXTERNAL_OUTPUT)], "permission_denied", "mcp_tool_quarantined"),
        ([_tool("other")], "resource_not_found", "mcp_tool_not_listed"),
    ],
    ids=["quarantined", "not-listed"],
)
async def test_executor_classifies_refusals_on_every_entry(
    mcp_allowed, enforce, tools, error_class, reason
):
    session = _Session(tools)
    result = await _execute(_manager(session), enforce=enforce)

    assert result["error_class"] == error_class
    assert result["reason"] == reason
    assert result["denied_tool"] == "srv__probe"
    assert session.calls == []


@pytest.mark.parametrize("enforce", [True, False], ids=["model-loop", "debug-execute"])
@pytest.mark.parametrize("arguments", _OVERSIZED, ids=_OVERSIZED_IDS)
async def test_executor_reports_oversized_header_arguments_as_validation_errors(
    mcp_allowed, enforce, arguments
):
    session = _routed_session()
    tool = "fanout" if "r0" in arguments else "routed"
    result = await _execute(_manager(session), enforce=enforce, tool=tool, arguments=arguments)

    assert result["error_class"] == "validation_error"
    assert result["reason"] == "mcp_header_argument_invalid"
    assert "base64" in result["suggestion"]
    assert _MARKER not in json.dumps(result) and "區" not in json.dumps(result)
    assert session.calls == []


async def test_breaker_neutral_error_records_neither_success_nor_failure():
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.failure_count = 2

    async def _refuse() -> None:
        raise BreakerNeutralError

    with pytest.raises(BreakerNeutralError):
        await breaker.call_async(_refuse)
    assert (breaker.state, breaker.failure_count) == (CircuitState.CLOSED, 2)
