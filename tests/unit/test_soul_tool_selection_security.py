"""T2：靈魂與自訂模型的工具清單（ADR-0281）。

- 比對規則是一個函式（SSOT）：內建與記憶工具比名稱；MCP 只認恰好一個斜線的 ``伺服器/工具``，
  比（伺服器, 工具）這一對；舊的長名 ``伺服器__工具`` 照舊逐字比廣告名；舊短名、只寫伺服器名、
  萬用字元、兩個以上斜線都不生效，不會打開沒勾的工具。
- 勾了「執行指令」自動帶上「取結果」「停止指令」，兩者照樣經過角色權限與政策。
- 舊短名每個靈魂只記一次警告。
- ``/v1/tools_stats`` 為 MCP 工具附加分開的伺服器與工具欄位。

全部走 conftest 沙盒與 mock：不連外、不讀 data/。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
import src.main as main_module
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.middleware.layers import tool as tool_layer_module
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.permissions import PermissionsConfig, RoleConfig
from src.protocols.manager import ProtocolManager
from src.protocols.mcp.handles import LegacyMcpHandle
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.routers import observability
from src.services.permission_service import permission_service
from src.services.prompt_blocks import MCP_UNAVAILABLE
from src.utils.soul_tool_selection import TOOL_COMPANIONS, ToolSelection, companions_of

_SOUL = AgentIdentity.for_role(agent_id="PickSoul", role="Owner")
_HUMAN = AgentIdentity.for_role(agent_id="human-pick", role="admin", is_human=True)


# --- 1. 比對函式 ----------------------------------------------------------------


def test_server_slash_tool_selects_exactly_that_pair():
    selection = ToolSelection.parse(["srv/lookup"])

    assert selection.selects("srv__lookup", server="srv", tool="lookup")
    assert not selection.selects("other__lookup", server="other", tool="lookup")
    assert not selection.selects("lookup")  # 同名的內建工具不會被打開
    assert not selection.selects("srv__lookup")  # 沒有伺服器資訊時不比對


def test_pair_match_does_not_use_the_advertised_name():
    """廣告名都是 a__b__c，但這是不同的（伺服器, 工具）。"""
    selection = ToolSelection.parse(["a/b__c"])

    assert selection.selects("a__b__c", server="a", tool="b__c")
    assert not selection.selects("a__b__c", server="a__b", tool="c")


def test_legacy_long_name_still_matches_the_advertised_name():
    selection = ToolSelection.parse(["srv__lookup"])

    assert selection.selects("srv__lookup", server="srv", tool="lookup")
    assert not selection.selects("srv__other", server="srv", tool="other")


@pytest.mark.parametrize("entry", ["lookup", "srv", "*", "srv/*", "*/lookup"])
def test_short_names_server_names_and_wildcards_open_no_mcp_tool(entry):
    selection = ToolSelection.parse([entry])

    assert not selection.selects("srv__lookup", server="srv", tool="lookup")


@pytest.mark.parametrize(
    "entry", ["srv/*", "*/lookup", "*/*", "srv/look*", "s*v/x", "srv/*x", "srv__*", "*"]
)
def test_wildcard_entries_are_dropped_entirely(entry):
    """含萬用字元的整條不收（兩種寫法都是）：否則會被當成「勾了這台」去連線、列進說明區塊。"""
    selection = ToolSelection.parse([entry])

    assert selection == ToolSelection()
    assert not selection.mentions_server("srv")


@pytest.mark.parametrize("entry", ["a/b/c", "/lookup", "srv/", "/", "a//b"])
def test_malformed_slash_entries_select_nothing(entry):
    selection = ToolSelection.parse([entry])

    assert selection.names == frozenset()
    assert selection.mcp_pairs == frozenset()


def test_non_string_entries_are_ignored():
    selection = ToolSelection.parse(["read", None, 3, ["srv/x"], {"srv": "x"}])

    assert selection.names == frozenset({"read"})
    assert selection.mcp_pairs == frozenset()


def test_companions_follow_only_a_listed_execute_command():
    """配套跟著「這一輪真的列出的」執行指令走；舊名 bash 不列給模型，不帶配套。"""
    assert TOOL_COMPANIONS == {"execute_command": ("get_command_result", "stop_command")}
    assert companions_of({"execute_command", "read"}) == {"get_command_result", "stop_command"}
    assert companions_of({"bash", "read"}) == frozenset()
    assert ToolSelection.parse(["execute_command"]).names == frozenset({"execute_command"})


def test_mentions_server_reads_both_forms_but_not_a_bare_server_name():
    assert ToolSelection.parse(["srv/x"]).mentions_server("srv")
    assert ToolSelection.parse(["srv__x"]).mentions_server("srv")
    assert not ToolSelection.parse(["srv"]).mentions_server("srv")
    assert not ToolSelection.parse(["srvx__y", "other/srv"]).mentions_server("srv")


def test_servers_for_connecting():
    selection = ToolSelection.parse(["a/x", "b__y", "read"])

    assert selection.pair_servers() == frozenset({"a"})
    assert selection.names >= {"b__y", "read"}


def test_short_names_are_the_plain_names_no_known_tool_has():
    selection = ToolSelection.parse(["read", "lookup", "srv__x", "srv/y", "openDiff"])

    assert selection.short_names(frozenset({"read", "openDiff"})) == frozenset({"lookup"})


# --- 2. ToolLayer ---------------------------------------------------------------


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


def _manager(tmp_path, catalogs: dict[str, list[str]], offline: tuple[str, ...] = ()):
    """真的 ProtocolManager：catalogs 裡的伺服器已連線，offline 的只有設定。"""
    registry = ProtocolRegistry(str(tmp_path / "server_config.json"))
    registry.servers = {
        name: ProtocolServerConfig(command="echo") for name in (*catalogs, *offline)
    }
    manager = ProtocolManager(registry)
    for name, tools in catalogs.items():
        manager.handle_pool.put(
            manager.handle_key_for(name, None), LegacyMcpHandle(session=_CatalogSession(tools))
        )
    return manager


def _ctx(**extra: Any) -> PipelineContext:
    ctx = PipelineContext(identity=_SOUL, agent_id=_SOUL.agent_id)
    ctx["headers"] = {}
    for key, value in extra.items():
        ctx[key] = value
    return ctx


def _request() -> OpenAIChatRequest:
    return OpenAIChatRequest(model="PickSoul", messages=[OpenAIMessage(role="user", content="hi")])


async def _listed(ctx: PipelineContext, request: OpenAIChatRequest | None = None):
    await ToolLayer().process(request or _request(), ctx)
    return ctx["advertised_tools"]


async def test_soul_list_in_server_slash_tool_form_lists_the_mcp_tool(tmp_path):
    manager = _manager(tmp_path, {"srv": ["lookup", "write"], "other": ["lookup"]})

    advertised = await _listed(_ctx(protocol_manager=manager, desired_tools=["srv/lookup"]))

    assert advertised.mcp == frozenset({("srv", "lookup")})


async def test_legacy_long_names_keep_working(tmp_path):
    manager = _manager(tmp_path, {"srv": ["lookup", "write"]})

    advertised = await _listed(_ctx(protocol_manager=manager, desired_tools=["srv__lookup"]))

    assert advertised.mcp == frozenset({("srv", "lookup")})


async def test_short_name_equal_to_a_builtin_opens_only_the_builtin(tmp_path):
    manager = _manager(tmp_path, {"fs": ["read"], "srv": ["lookup"]})

    advertised = await _listed(
        _ctx(protocol_manager=manager, desired_tools=["read", "lookup", "srv", "fs/*"])
    )

    assert "read" in advertised.native
    assert advertised.mcp == frozenset()


async def test_terminal_companions_are_listed_with_it(tmp_path):
    advertised = await _listed(_ctx(desired_tools=["execute_command"]))

    assert {"execute_command", "get_command_result", "stop_command"} <= advertised.native


async def test_no_companions_when_execute_command_is_not_listed(monkeypatch):
    """被政策擋下的執行指令不帶配套；只勾舊名 bash（不列給模型）也不帶。"""
    real = permission_service.evaluate_policy

    def _deny_terminal(policy_context: Any) -> Any:
        if policy_context.resource == "execute_command":
            return SimpleNamespace(effect="DENY", reason="test")
        return real(policy_context)

    monkeypatch.setattr(permission_service, "evaluate_policy", _deny_terminal)
    denied = await _listed(_ctx(desired_tools=["execute_command"]))
    monkeypatch.setattr(permission_service, "evaluate_policy", real)
    legacy = await _listed(_ctx(desired_tools=["bash"]))

    for advertised in (denied, legacy):
        assert not {"execute_command", "get_command_result", "stop_command"} & advertised.native


async def test_no_companions_when_the_client_brings_its_own_execute_command():
    """客戶端自帶同名工具時，伺服器端的執行指令不會列出，配套也不帶。"""
    request = _request()
    request.tools = [
        {"type": "function", "function": {"name": "execute_command", "parameters": {}}}
    ]

    advertised = await _listed(_ctx(desired_tools=["execute_command"]), request)

    assert not {"execute_command", "get_command_result", "stop_command"} & advertised.native


async def test_pair_entries_still_need_the_mcp_role_grant(tmp_path, monkeypatch):
    manager = _manager(tmp_path, {"srv": ["lookup"]})
    monkeypatch.setattr(permission_service, "is_mcp_tool_allowed", lambda *_a: False)

    soul = await _listed(_ctx(protocol_manager=manager, desired_tools=["srv/lookup"]))
    custom = await _listed(
        _ctx(protocol_manager=manager, is_custom=True, desired_tools=["srv/lookup"])
    )

    assert soul.mcp == frozenset()
    assert custom.mcp == frozenset()


async def test_companions_still_pass_the_role_grants(monkeypatch):
    monkeypatch.setattr(
        permission_service,
        "roles_config",
        PermissionsConfig(
            roles={"Owner": RoleConfig(allowed_tools=["execute_command", "get_command_result"])}
        ),
    )
    monkeypatch.setattr(permission_service, "_auto_reload_check", lambda: None)

    advertised = await _listed(_ctx(desired_tools=["execute_command"]))
    # 自己勾了也一樣：工具清單不能繞過角色權限
    ticked = await _listed(_ctx(desired_tools=["execute_command", "stop_command"]))

    assert "get_command_result" in advertised.native
    assert "stop_command" not in advertised.native
    assert "stop_command" not in ticked.native


async def test_custom_model_connects_the_server_of_a_pair_entry(tmp_path):
    manager = _manager(tmp_path, {"srv": ["lookup"]})
    connected: list[str] = []

    async def _ensure(name: str, request_context: Any = None) -> None:
        connected.append(name)

    manager.ensure_connected = _ensure  # type: ignore[method-assign]

    advertised = await _listed(
        _ctx(protocol_manager=manager, is_custom=True, desired_tools=["srv/lookup"])
    )

    assert connected == ["srv"]
    assert advertised.mcp == frozenset({("srv", "lookup")})


async def test_custom_model_pair_entries_do_not_need_dynamic_mounting(tmp_path):
    """自訂模型勾的工具照舊不受動態掛載限制（原本長名的行為），`伺服器/工具` 也一樣。"""
    manager = _manager(tmp_path, {"srv": ["lookup", "write"]})

    advertised = await _listed(
        _ctx(
            protocol_manager=manager,
            is_custom=True,
            mcp_dispatcher=True,
            desired_tools=["srv/lookup"],
        )
    )

    assert advertised.mcp == frozenset({("srv", "lookup")})


async def test_unavailable_note_follows_pair_entries(tmp_path):
    manager = _manager(tmp_path, {"srv": ["lookup"]}, offline=("wanted", "unrelated"))
    request = _request()

    await _listed(_ctx(protocol_manager=manager, desired_tools=["wanted/lookup"]), request)

    system = "\n".join(str(m.content) for m in request.messages if m.role == "system")
    note = system.split(f"<{MCP_UNAVAILABLE}>", 1)[1].split(f"</{MCP_UNAVAILABLE}>", 1)[0]
    assert "wanted" in note
    assert "unrelated" not in note


@pytest.fixture
def warnings_seen(monkeypatch, caplog):
    monkeypatch.setattr(tool_layer_module, "_SHORT_NAME_WARNED", set())
    caplog.set_level(logging.WARNING, logger=tool_layer_module.logger.name)
    return lambda: [r for r in caplog.records if "工具清單" in r.getMessage()]


async def test_short_names_are_warned_once_per_soul(warnings_seen):
    await _listed(_ctx(is_toukasoul=True, desired_tools=["read", "lookup", "search"]))
    await _listed(_ctx(is_toukasoul=True, desired_tools=["read", "lookup", "search"]))

    (warning,) = warnings_seen()
    assert "PickSoul" in warning.getMessage()
    assert "2" in warning.getMessage()


async def test_no_warning_for_known_names_and_mcp_entries(warnings_seen):
    entries = ["read", "srv/lookup", "srv__write", "execute_command"]
    await _listed(_ctx(is_toukasoul=True, desired_tools=entries))

    assert not warnings_seen()


async def test_no_warning_for_a_name_this_turn_listed(monkeypatch, warnings_seen):
    """ToukaUI 這類工具的廣告名沒有伺服器前綴：這一輪有列出就不算舊短名。"""
    ui = {"server": "ToukaUI", "type": "function", "function": {"name": "openDiff"}}
    protocols = SimpleNamespace(
        get_all_tools=lambda **_k: _async([dict(ui)]),
        registry=SimpleNamespace(get_server=lambda _n: None),
    )
    monkeypatch.setattr(permission_service, "is_mcp_tool_allowed", lambda *_a: True)

    advertised = await _listed(
        _ctx(is_toukasoul=True, protocol_manager=protocols, desired_tools=["openDiff"])
    )

    assert ("ToukaUI", "openDiff") in advertised.mcp
    assert not warnings_seen()


@pytest.mark.parametrize(
    "run",
    [
        {"is_toukasoul": True, "subagent_run": True},
        {"is_toukasoul": True, "identity": _HUMAN},
        {"is_custom": True},
        {},
    ],
)
async def test_short_names_are_warned_only_for_a_soul_s_own_run(run, warnings_seen):
    """子代理每次身分都不同、自訂模型的身分是人類：只在靈魂自己的執行記警告。"""
    await _listed(_ctx(desired_tools=["lookup"], **run))

    assert not warnings_seen()


async def _async(value: Any) -> Any:
    return value


# --- 3. /v1/tools_stats ---------------------------------------------------------


async def test_tools_stats_adds_server_and_tool_fields(tmp_path, monkeypatch):
    manager = _manager(tmp_path, {"srv": ["lookup"], "a": ["b__c"]})
    monkeypatch.setattr(main_module, "protocol_manager", manager)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(context={})))

    body = await observability.get_tools_stats(request, _SOUL)

    entries = {(e["server"], e["tool"]): e for e in body["mcp_schemas"]}
    assert set(entries) == {("srv", "lookup"), ("a", "b__c")}
    # 舊欄位不拿掉
    assert entries[("srv", "lookup")]["function"]["raw_name"] == "srv__lookup"
    assert entries[("srv", "lookup")]["category"] == "MCP: srv"
    # 畫面用來標「隨執行指令一起開」；來源是同一份 TOOL_COMPANIONS
    assert body["tool_companions"] == {"execute_command": ["get_command_result", "stop_command"]}
