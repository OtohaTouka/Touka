"""vault-jailed MCP 的執行器接線——行為級（E66／ADR-0176；ADR-0293 起改成拒絕）。

ADR-0115 #1 鐵則：檔案型 MCP 存取**永不跨 agent**。ADR-0293 起設了 ``vault_jail_arg`` 的伺服器
（docgen）不再拿到任何根：它自己的讀寫不經 ResourceManager，交出的根是連結時會跟著走，而
``base_dir`` 同時是讀與寫的根，交別的資料夾會讓產出落錯地方。所以這類伺服器的工具在 Touka 這端
以固定原因「目前不能用」拒絕、不送出（stdio 與 HTTP 都一樣）；安全的讀寫另開任務。本檔經真
ToolExecutor.execute_to_queue＋fake ProtocolManager 釘死：受管伺服器零派發、回結構化
``permission_denied``；非受管伺服器原樣通過（陽性對照）。data 根換到 ``zone_data_root``。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from memory_server.utils.zone_paths import anchor_dirname
from src.models.identity import AgentIdentity
from src.models.policy import PolicyDecision
from src.protocols.mcp.canonical import McpCompleteOutcome
from src.protocols.mcp.sdk_adapter import UNKNOWN_PROTOCOL_VERSION
from src.services import tool_errors
from src.services.mcp_vault_jail import VAULT_JAILED_REASON
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.services.tool_executor import ToolExecutor

from tests.support.links import LINK_FOLLOWED, make_dir_link

if TYPE_CHECKING:
    from pathlib import Path

    from src.protocols.mcp.canonical import McpRequestContext

_IDENTITY = AgentIdentity.for_role(agent_id="JailProbe", role="Owner", is_human=False)


class _FakeRegistry:
    def __init__(self, servers: dict[str, Any]) -> None:
        self._servers = servers

    def get_server(self, name: str) -> Any:
        return self._servers.get(name)


class _FakeProtocolManager:
    """resolve_tool 固定命中 MCP 伺服器；call_tool_on_server 錄下實收 args。"""

    def __init__(self, server_id: str, cfg: Any) -> None:
        self._server_id = server_id
        self.registry = _FakeRegistry({server_id: cfg})
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def resolve_tool(self, name: str) -> tuple[str | None, str]:
        return self._server_id, name

    async def call_tool_on_server(
        self,
        server_id: str,
        tool: str,
        args: dict[str, Any],
        *,
        request_context: McpRequestContext,
    ) -> McpCompleteOutcome:
        self.calls.append((server_id, tool, dict(args)))
        return McpCompleteOutcome(
            source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
            content=[{"type": "text", "text": "ok"}],
        )


def _allow_policy(monkeypatch) -> None:
    # 政策閘在咽喉上游、非本檔標的——固定 ALLOW（同 test_hitl_broker_executor 慣例）。
    monkeypatch.setattr(
        permission_service,
        "evaluate_policy",
        lambda _ctx: PolicyDecision(effect="ALLOW", reason="test-allow"),
    )


async def _dispatch(pm: _FakeProtocolManager, args: dict[str, Any]) -> str:
    executor = ToolExecutor(
        protocol_manager=pm,
        tool_registry={},
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )
    queue: asyncio.Queue = asyncio.Queue()
    return await executor.execute_to_queue(
        "docgen_read",
        args,
        "req-jail",
        {"identity": _IDENTITY, "auth_level": "Manual", "session_id": None},
        queue,
    )


def _link_the_drive(zone_data_root: Path, tmp_path: Path) -> Path:
    """把 JailProbe 的個人空間換成連結，指到假的設定資料夾（舊程式會把它交給 docgen）。"""
    drive = resource_manager.agent_drive(_IDENTITY)
    folder = zone_data_root.joinpath(*anchor_dirname(drive.anchor).split("/"), *drive.parts)
    folder.parent.mkdir(parents=True, exist_ok=True)
    fake = tmp_path.resolve() / "fake_config"
    fake.mkdir()
    (fake / "users.json").write_text("{}", encoding="utf-8")
    make_dir_link(folder, fake)
    return fake


@pytest.mark.parametrize(
    "cfg",
    [
        SimpleNamespace(vault_jail_arg="base_dir", command="uvx", url=None),
        SimpleNamespace(vault_jail_arg="base_dir", command=None, url="http://127.0.0.1:9/mcp"),
    ],
    ids=["stdio", "http"],
)
async def test_a_vault_jailed_tool_is_refused_and_never_sent(
    zone_data_root, tmp_path, monkeypatch, cfg
):
    """受管伺服器（stdio 與 HTTP）：不送出、沒有任何根，回固定原因的 ``permission_denied``。"""
    _allow_policy(monkeypatch)
    _link_the_drive(zone_data_root, tmp_path)
    pm = _FakeProtocolManager("mcp-docgen", cfg)

    result = await _dispatch(pm, {"base_dir": "/etc", "file_ref": "outputs/uploads/x.pdf"})

    assert pm.calls == [], LINK_FOLLOWED  # 舊程式把個人空間根（這裡是連結）交給 docgen
    assert tool_errors.extract_error_class(result) == tool_errors.PERMISSION_DENIED
    assert f'"reason": "{VAULT_JAILED_REASON}"' in result


async def test_non_jailed_server_keeps_model_args(monkeypatch):
    """無 vault_jail_arg 的伺服器不受管：模型 args 原樣通過（陽性對照＝jail 是選擇性的）。"""
    _allow_policy(monkeypatch)
    pm = _FakeProtocolManager("mcp-fetch", SimpleNamespace(vault_jail_arg=None))

    await _dispatch(pm, {"url": "https://example.com"})

    assert len(pm.calls) == 1
    _sid, _tool, sent = pm.calls[0]
    assert sent["url"] == "https://example.com"
    assert sent["agent_id"] == "JailProbe"  # 派發層注入的 agent_id 保留（jail 路徑才 pop）
