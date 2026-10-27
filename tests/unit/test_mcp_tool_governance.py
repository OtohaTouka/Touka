"""MCP per-tool 治理兩軸的行為級測試（ADR-0195）。

此前 Touka 的 MCP 治理只有 **per-server** 粒度，且有兩個各自獨立的破口：

- `get_allowed_tools` 對 Owner 短路回 ``["*"]``、`is_mcp_server_allowed` 對 Owner 恆 True
  → **Owner ＋ 任一已安裝伺服器 ＝ 該伺服器全部工具**，沒有任何收斂旋鈕；出廠貨架已含
  含 ``create_refund`` 的 Stripe 條目。
- 執行面對 MCP **完全沒有身分閘**（`permission_service.is_authorized` 生產零呼叫端）
  → 收斂只存在於廣告面，模型叫出一個未被廣告的工具名即可直接執行。

本檔逐條釘死兩軸的**執法點**，而不只是 SSOT 比對函式（後者在 test_mcp_tool_policy.py）。
關鍵不變式：
1. 軸 A（per-installation）**連 Owner 都綁得住**——它不是權限邊界，故刻意不走 permission_service。
2. 廣告面與執行面**各自**執法；只做廣告面等於沒做。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from src.core.circuit_breaker import CircuitBreaker
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.permissions import PermissionsConfig, RoleConfig
from src.protocols.manager import ProtocolManager, ToolDisabledError
from src.protocols.mcp.canonical import McpRequestContext
from src.protocols.registry import ProtocolRegistry
from src.services import tool_errors
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor

_OWNER = AgentIdentity.for_role(agent_id="OwnerProbe", role="Owner", is_human=False)
_BASE = AgentIdentity.for_role(agent_id="BaseProbe", role="Base", is_human=False)


def _context(server: str, identity: AgentIdentity = _OWNER) -> McpRequestContext:
    return McpRequestContext(
        request_id="req-governance",
        session_id="session-governance",
        identity=identity,
        installation_id=server,
        auth_level="SafeAuto",
        origin="foreground",
    )


# --- 測試替身 -------------------------------------------------------------


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"desc-{name}"
        self.input_schema: dict[str, Any] = {"type": "object", "properties": {}}


class _FakeToolsResult:
    def __init__(self, names: list[str]) -> None:
        self.tools = [_FakeTool(n) for n in names]


class _FakeSession:
    """最小 MCP session：記錄實際被派發的工具名（用來證明「零執行」而非「有擋但仍打過去」）。"""

    def __init__(self, names: list[str]) -> None:
        self._names = names
        self.called: list[str] = []

    async def list_tools(self) -> _FakeToolsResult:
        return _FakeToolsResult(self._names)

    async def call_tool(self, tool_name: str, _arguments: dict) -> Any:
        self.called.append(tool_name)

        class _Res:
            content = []

        return _Res()


_TOOLS = ["list_customers", "create_refund", "stripe_api_write"]


def _manager(
    tmp_path,
    *,
    cfg_extra: dict[str, Any] | None = None,
) -> tuple[ProtocolManager, _FakeSession]:
    """真 ProtocolManager ＋ 假 session：registry 走真的 Pydantic 解析與真的過濾邏輯。"""
    server: dict[str, Any] = {"command": "echo", "args": ["stripe"]}
    server.update(cfg_extra or {})
    path = tmp_path / "server_config.json"
    path.write_text(json.dumps({"mcpServers": {"stripe": server}}), encoding="utf-8")
    mgr = ProtocolManager(ProtocolRegistry(str(path)))
    session = _FakeSession(_TOOLS)
    mgr._active_sessions["stripe"] = session  # noqa: SLF001
    # 真斷路器：`call_tool_on_server` 以「有無 breaker」判定伺服器存在，缺席即 ServerNotFoundError。
    mgr._circuit_breakers["stripe"] = CircuitBreaker(  # noqa: SLF001
        failure_threshold=3, recovery_timeout=30.0
    )
    return mgr, session


async def _advertised(mgr: ProtocolManager) -> list[str]:
    return [t["function"]["name"] for t in await mgr.get_all_tools()]


# --- 軸 A：per-installation（供給面，所有身分一律適用） ---------------------


@pytest.mark.asyncio
async def test_axis_a_unset_advertises_everything(tmp_path):
    """未設 ``enabled_tools`` ＝全開：既有條目升級後不得整批消失。"""
    mgr, _ = _manager(tmp_path)
    assert await _advertised(mgr) == [f"stripe__{n}" for n in _TOOLS]


@pytest.mark.asyncio
async def test_axis_a_allowlist_filters_advertising(tmp_path):
    mgr, _ = _manager(tmp_path, cfg_extra={"enabled_tools": ["list_customers"]})
    assert await _advertised(mgr) == ["stripe__list_customers"]


@pytest.mark.asyncio
async def test_axis_a_denylist_beats_allowlist_in_advertising(tmp_path):
    mgr, _ = _manager(
        tmp_path,
        cfg_extra={"enabled_tools": ["*"], "disabled_tools": ["create_refund"]},
    )
    names = await _advertised(mgr)
    assert "stripe__create_refund" not in names
    assert "stripe__list_customers" in names


@pytest.mark.asyncio
async def test_axis_a_projects_bare_tool_name(tmp_path):
    """廣告項須帶裸名（軸 B 授權與設定面都用裸名）——缺席會逼各消費端自己 split("__")。"""
    mgr, _ = _manager(tmp_path)
    tools = await mgr.get_all_tools()
    assert {t["tool"] for t in tools} == set(_TOOLS)


@pytest.mark.asyncio
async def test_axis_a_blocks_execution_not_just_advertising(tmp_path):
    """執行面咽喉：廣告面看不到的工具，直接呼叫也必須零派發。

    這是本軸最重要的一條——只做廣告面過濾時，模型從歷史脈絡叫出 ``create_refund``
    照樣會打到真實金流端點。
    """
    mgr, session = _manager(tmp_path, cfg_extra={"disabled_tools": ["create_refund"]})
    with pytest.raises(ToolDisabledError):
        await mgr.call_tool_on_server(
            "stripe",
            "create_refund",
            {},
            request_context=_context("stripe"),
        )
    assert session.called == []


@pytest.mark.asyncio
async def test_axis_a_allows_enabled_tool_execution(tmp_path):
    """陽性對照：未被關閉的工具照常派發（防「全擋」式假綠）。"""
    mgr, session = _manager(tmp_path, cfg_extra={"disabled_tools": ["create_refund"]})
    await mgr.call_tool_on_server(
        "stripe",
        "list_customers",
        {},
        request_context=_context("stripe"),
    )
    assert session.called == ["list_customers"]


@pytest.mark.asyncio
async def test_axis_a_binds_owner(tmp_path):
    """**核心不變式**：軸 A 對 Owner 同樣生效。

    軸 A 若被接進 `permission_service`，Owner 會於 `is_mcp_server_allowed`／
    `get_allowed_tools` 短路而完全繞過——那正是這條治理線唯一的實際受害者。
    """
    mgr, session = _manager(tmp_path, cfg_extra={"disabled_tools": ["create_refund"]})
    assert permission_service.is_mcp_tool_allowed("stripe", "create_refund", _OWNER) is True
    with pytest.raises(ToolDisabledError):
        await mgr.call_tool_on_server(
            "stripe",
            "create_refund",
            {},
            request_context=_context("stripe"),
        )
    assert session.called == []


@pytest.mark.asyncio
async def test_axis_a_disabled_tool_does_not_trip_circuit_breaker(tmp_path):
    """政策關閉不是故障：不得累積斷路器失敗計數，否則關掉一個工具會拖垮整台。"""
    mgr, _ = _manager(tmp_path, cfg_extra={"disabled_tools": ["create_refund"]})
    mgr._circuit_breakers["stripe"] = _CountingBreaker()  # noqa: SLF001
    with pytest.raises(ToolDisabledError):
        await mgr.call_tool_on_server(
            "stripe",
            "create_refund",
            {},
            request_context=_context("stripe"),
        )
    assert mgr._circuit_breakers["stripe"].attempts == 0  # noqa: SLF001


class _CountingBreaker:
    def __init__(self) -> None:
        self.attempts = 0

    async def call_async(self, fn):
        self.attempts += 1
        return await fn()


@pytest.mark.asyncio
async def test_list_server_tools_is_unfiltered(tmp_path):
    """治理 UI 的資料源必須是**未過濾**的完整工具面，否則取消勾選後就再也勾不回來。"""
    mgr, _ = _manager(tmp_path, cfg_extra={"enabled_tools": []})
    assert await _advertised(mgr) == []
    listed = await mgr.list_server_tools("stripe")
    assert [t["name"] for t in listed] == _TOOLS


# --- 軸 A × 執行層：結構化錯誤而非泛用例外 --------------------------------


@pytest.mark.asyncio
async def test_executor_reports_disabled_tool_as_permission_denied(tmp_path, monkeypatch):
    """真 ToolExecutor 打穿 MCP 分支：軸 A 攔截須回結構化 PERMISSION_DENIED。

    掉進泛用 ``except Exception`` 會變成「工具執行失敗：ToolDisabledError: ...」——模型分不出
    「政策關閉」與「伺服器故障」，會換個名字重試。
    """
    mgr, session = _manager(tmp_path, cfg_extra={"disabled_tools": ["create_refund"]})
    monkeypatch.setattr(
        permission_service, "evaluate_policy", lambda _c: _Allow()
    )
    executor = ToolExecutor(
        protocol_manager=mgr, tool_registry={}, memory_tool_registry={}, plugin_registry={},
        enforce_advertised=False,
    )
    queue: asyncio.Queue = asyncio.Queue()
    result = await executor.execute_to_queue(
        "stripe__create_refund",
        {},
        "req-gov",
        {"identity": _OWNER, "auth_level": "Manual", "session_id": "s"},
        queue,
    )
    assert tool_errors.is_tool_error(result)
    assert "mcp_tool_disabled" in result
    assert session.called == []


class _Allow:
    effect = "ALLOW"
    reason = "test"


# --- 軸 B：per-role（身分面） ---------------------------------------------


@pytest.fixture
def _roles(monkeypatch):
    """注入測試角色表；所有入口仍走真的 ``resolve_grants``。"""

    def _install(mapping: dict[str, RoleConfig]) -> None:
        monkeypatch.setattr(permission_service, "roles_config", PermissionsConfig(roles=mapping))
        monkeypatch.setattr(permission_service, "_auto_reload_check", lambda: None)

    return _install


def test_axis_b_tool_scoped_grant(_roles):
    """角色只被授予 ``server/tool`` 時：該工具通、同台其他工具不通。"""
    _roles({"Base": RoleConfig(allowed_mcp_servers=["stripe/list_customers"])})
    assert permission_service.is_mcp_tool_allowed("stripe", "list_customers", _BASE) is True
    assert permission_service.is_mcp_tool_allowed("stripe", "create_refund", _BASE) is False


def test_axis_b_tool_scoped_grant_still_allows_mount(_roles):
    """只授予單一工具者仍須能掛載該台——否則連線建不起來，工具面授權形同虛設。"""
    _roles({"Base": RoleConfig(allowed_mcp_servers=["stripe/list_customers"])})
    assert permission_service.is_mcp_server_allowed("stripe", _BASE) is True
    assert permission_service.is_mcp_server_allowed("github", _BASE) is False


def test_axis_b_denied_mcp_tools_binds_owner(_roles):
    """deny 面對 Owner 生效——與 `denied_tools` 在 `is_authorized` 的既有語意一致。"""
    _roles(
        {
            "Owner": RoleConfig(
                allowed_mcp_servers=["*"],
                denied_mcp_tools=["stripe/create_refund"],
            )
        }
    )
    assert permission_service.is_mcp_tool_allowed("stripe", "create_refund", _OWNER) is False
    assert permission_service.is_mcp_tool_allowed("stripe", "list_customers", _OWNER) is True


def test_axis_b_server_wide_deny(_roles):
    _roles({"Base": RoleConfig(allowed_mcp_servers=["*"], denied_mcp_tools=["stripe/*"])})
    assert permission_service.is_mcp_tool_allowed("stripe", "list_customers", _BASE) is False
    assert permission_service.is_mcp_tool_allowed("github", "list_issues", _BASE) is True


def test_denied_mcp_tools_bare_wildcard_is_role_local_deny_all(_roles):
    """裸 ``*`` 是該角色的 deny-all，不再沿繼承鏈擴散。"""
    _roles(
        {
            "Owner": RoleConfig(
                allowed_mcp_servers=["*"],
                denied_mcp_tools=["*"],
            )
        }
    )
    assert permission_service.is_mcp_server_allowed("stripe", _OWNER) is True
    assert permission_service.is_mcp_tool_allowed("stripe", "list_customers", _OWNER) is False


def test_denied_mcp_tools_are_self_only_in_inheritance(_roles):
    """子角色繼承父角色 allow，但不繼承父角色 deny。"""
    _roles(
        {
            "Parent": RoleConfig(
                allowed_mcp_servers=["*"],
                denied_mcp_tools=["github/delete_repo"],
            ),
            "Child": RoleConfig(
                inherits=["Parent"],
                denied_mcp_tools=["stripe/create_refund"],
            ),
        }
    )
    grants = permission_service.resolve_grants("Child")
    assert grants.mcp.allows("github", "delete_repo") is True
    assert grants.mcp.allows("stripe", "create_refund") is False
    assert grants.mcp.allows("stripe", "list_customers") is True


# --- 軸 B × 執行層：此前完全不存在的閘 ------------------------------------


@pytest.mark.asyncio
async def test_axis_b_blocks_unadvertised_tool_at_execution(tmp_path, monkeypatch, _roles):
    """**接線缺口**：未廣告的 MCP 工具名，執行面必須自己擋下來。

    修復前執行面對 MCP 零 RBAC——廣告面藏起來的工具，模型直接叫名字就執行了。
    """
    _roles({"Base": RoleConfig(allowed_mcp_servers=["stripe/list_customers"])})
    mgr, session = _manager(tmp_path)
    monkeypatch.setattr(permission_service, "evaluate_policy", lambda _c: _Allow())
    executor = ToolExecutor(
        protocol_manager=mgr, tool_registry={}, memory_tool_registry={}, plugin_registry={},
        enforce_advertised=False,
    )
    queue: asyncio.Queue = asyncio.Queue()
    result = await executor.execute_to_queue(
        "stripe__create_refund",
        {},
        "req-rbac",
        {"identity": _BASE, "auth_level": "Manual", "session_id": "s"},
        queue,
    )
    assert tool_errors.is_tool_error(result)
    assert "mcp_tool_rbac" in result
    assert session.called == []


@pytest.mark.asyncio
async def test_axis_b_allows_granted_tool_at_execution(tmp_path, monkeypatch, _roles):
    """陽性對照：獲授權的工具照常派發（防「全擋」式假綠）。"""
    _roles({"Base": RoleConfig(allowed_mcp_servers=["stripe/list_customers"])})
    mgr, session = _manager(tmp_path)
    monkeypatch.setattr(permission_service, "evaluate_policy", lambda _c: _Allow())
    executor = ToolExecutor(
        protocol_manager=mgr, tool_registry={}, memory_tool_registry={}, plugin_registry={},
        enforce_advertised=False,
    )
    queue: asyncio.Queue = asyncio.Queue()
    await executor.execute_to_queue(
        "stripe__list_customers",
        {},
        "req-rbac-ok",
        {"identity": _BASE, "auth_level": "Manual", "session_id": "s"},
        queue,
    )
    assert session.called == ["list_customers"]


# --- 軸 B × 廣告面 --------------------------------------------------------


@pytest.mark.asyncio
async def test_axis_b_filters_advertising_per_tool(tmp_path, monkeypatch, _roles):
    """真 ToolLayer：同一台伺服器內，只廣告角色獲授權的那個工具。"""
    _roles({"Base": RoleConfig(allowed_mcp_servers=["stripe/list_customers"])})
    mgr, _ = _manager(tmp_path)

    class _Req:
        tools: list[Any] = []
        project_id = None

    ctx = PipelineContext(identity=_BASE)
    ctx["agent_id"] = _BASE.agent_id
    ctx["is_custom"] = False
    ctx["is_toukasoul"] = False
    ctx["headers"] = {}
    ctx["protocol_manager"] = mgr
    monkeypatch.setattr(permission_service, "evaluate_policy", lambda _c: _Allow())

    req = _Req()
    await ToolLayer().process(req, ctx)
    names = [
        t.get("function", {}).get("name") for t in req.tools if isinstance(t, dict)
    ]
    assert "stripe__list_customers" in names
    assert "stripe__create_refund" not in names
