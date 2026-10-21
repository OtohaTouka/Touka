"""`GET /v1/mcp/{server_name}/tools` 的 HTTP 級行為（ADR-0195）。

`tests/unit/test_mcp_tool_governance.py` 只測到 `ProtocolManager.list_server_tools` 那層。
端點本身還有四件只在 HTTP 層才看得見的事，且各自都有無聲失效的形態：

1. **403 身分閘**——Guard #24 只證明「簽名裡有 `Depends`」，不證明它擋得住非特權身分。
   把 `_require_mcp_role` 換成 `get_verified_identity` 這個守衛照樣綠。
2. **未知伺服器回 422 而非 500 或空 200**——空 200 會讓 UI 渲染出「這台沒有工具」，
   使用者以為伺服器壞了，實際是打錯名字。
3. **已設定但未連線回 200＋空 tools**（文件化的非錯誤），且 `enabled_tools`／
   `disabled_tools` 仍原樣回傳——UI 靠這兩欄顯示既有政策，回 4xx 會讓「未連線的伺服器
   永遠改不了政策」。
4. **清單本身未經軸 A 過濾**——被關掉的工具必須仍在回應裡、只是 `enabled=false`。
   這正是本端點存在的唯一理由（複用廣告面的 `get_all_tools` 會讓勾掉的工具從清單消失、
   再也勾不回來）。

另加一條：`list_server_tools` 拋例外時端點吞掉回 200＋空清單（現行行為），釘住它不得
退化成靜默 500——但也不得因此把「例外」與「未連線」混為一談而讓政策欄位一起消失。
"""

from __future__ import annotations

from typing import Any

import pytest
import src.main as main_mod
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.protocols.manager import ProtocolManager
from src.protocols.registry import ProtocolRegistry
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager

_TOOLS = ["list_customers", "create_refund"]


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"desc-{name}"
        self.input_schema: dict[str, Any] = {"type": "object"}


class _FakeToolsResult:
    def __init__(self, names: list[str]) -> None:
        self.tools = [_FakeTool(n) for n in names]


class _FakeSession:
    async def list_tools(self) -> _FakeToolsResult:
        return _FakeToolsResult(_TOOLS)


class _ExplodingSession:
    async def list_tools(self) -> _FakeToolsResult:
        msg = "transport died"
        raise ConnectionError(msg)


def _install_manager(monkeypatch, tmp_path, *, session: Any, policy: dict[str, Any]) -> None:
    """把 `src.main.protocol_manager` 換成一台受控的真 ProtocolManager。

    端點以 `from src.main import protocol_manager` 於函式內取用，故 patch 該模組屬性即生效。
    設定經**真的 `ProtocolRegistry.load_config`** 從磁碟解析——`enabled:false` 的排除
    （ADR-0188）就發生在那裡，直接塞 `registry.servers` 會變成測試自己重刻那段邏輯。
    """
    server: dict[str, Any] = {"command": "echo", "args": ["stripe"], **policy}
    cfg = tmp_path / "server_config.json"
    resource_manager.write_json(cfg, {"mcpServers": {"stripe": server}})
    mgr = ProtocolManager(ProtocolRegistry(str(cfg)))
    if session is not None:
        mgr._active_sessions["stripe"] = session  # noqa: SLF001
    monkeypatch.setattr(main_mod, "protocol_manager", mgr)
    app.state.context["protocol_manager"] = mgr


@pytest.fixture
def _unprivileged_client():
    """非特權身分（Base agent）的 TestClient——用來證明 403 閘真的擋得住。"""
    base = AgentIdentity.for_role(agent_id="BaseProbe", role="Base")
    app.dependency_overrides[get_verified_identity] = lambda: base
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


# ── 身分閘 ────────────────────────────────────────────────────────────────


def test_unprivileged_identity_gets_403(_unprivileged_client, monkeypatch, tmp_path):
    """非 Owner/Debugger/admin → 403。

    工具清單會揭露一台伺服器的完整能力面（含 `create_refund` 這類金流動詞），不是公開資訊。
    """
    _install_manager(monkeypatch, tmp_path, session=_FakeSession(), policy={})
    resp = _unprivileged_client.get("/v1/mcp/stripe/tools")
    assert resp.status_code == 403


def test_privileged_identity_gets_200(auth_client, monkeypatch, tmp_path):
    """陽性對照：Owner 拿得到——防「全擋式假綠」。"""
    _install_manager(monkeypatch, tmp_path, session=_FakeSession(), policy={})
    assert auth_client.get("/v1/mcp/stripe/tools").status_code == 200


# ── 未知伺服器 ────────────────────────────────────────────────────────────


def test_unknown_server_returns_422_not_500_nor_empty_200(auth_client, monkeypatch, tmp_path):
    """打錯名字要說「找不到」，不能靜默回一份空清單讓人以為伺服器壞了。"""
    _install_manager(monkeypatch, tmp_path, session=_FakeSession(), policy={})
    resp = auth_client.get("/v1/mcp/no-such-server/tools")
    assert resp.status_code == 422


def test_disabled_server_is_also_unknown(auth_client, monkeypatch, tmp_path):
    """`enabled:false` 者不進 runtime registry（ADR-0188）→ 對本端點等同不存在。"""
    _install_manager(monkeypatch, tmp_path, session=_FakeSession(), policy={"enabled": False})
    assert auth_client.get("/v1/mcp/stripe/tools").status_code == 422


# ── 未連線 ────────────────────────────────────────────────────────────────


def test_not_connected_returns_200_with_empty_tools_but_keeps_policy(
    auth_client, monkeypatch, tmp_path
):
    """未連線＝非錯誤：回 200＋空 tools，但政策欄位必須原樣回傳。

    回 4xx 或吞掉政策欄位，都會讓「未連線的伺服器永遠改不了政策」。
    """
    _install_manager(
        monkeypatch, tmp_path, session=None, policy={"disabled_tools": ["create_refund"]}
    )
    resp = auth_client.get("/v1/mcp/stripe/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tools"] == []
    assert body["disabled_tools"] == ["create_refund"]
    assert body["server"] == "stripe"


# ── 未過濾的完整工具面（本端點存在的理由）────────────────────────────────


def test_response_is_unfiltered_and_marks_disabled_tools(auth_client, monkeypatch, tmp_path):
    """被關掉的工具**仍在清單裡**，只是 `enabled=false`。

    若改用廣告面的 `get_all_tools`，`create_refund` 會整筆消失 → 使用者取消勾選後就再也
    勾不回來。這是本端點與 `get_all_tools` 唯一且關鍵的差別。
    """
    _install_manager(
        monkeypatch, tmp_path, session=_FakeSession(), policy={"disabled_tools": ["create_refund"]}
    )
    body = auth_client.get("/v1/mcp/stripe/tools").json()

    by_name = {t["name"]: t for t in body["tools"]}
    assert set(by_name) == set(_TOOLS), "清單必須是全集，不得依政策濾除"
    assert by_name["create_refund"]["enabled"] is False
    assert by_name["list_customers"]["enabled"] is True
    assert by_name["create_refund"]["description"] == "desc-create_refund"


def test_allowlist_marks_unlisted_tools_disabled(auth_client, monkeypatch, tmp_path):
    """白名單形態同理：未列名者 `enabled=false`，但仍在清單中。"""
    _install_manager(
        monkeypatch,
        tmp_path,
        session=_FakeSession(),
        policy={"enabled_tools": ["list_customers"]},
    )
    body = auth_client.get("/v1/mcp/stripe/tools").json()

    by_name = {t["name"]: t for t in body["tools"]}
    assert set(by_name) == set(_TOOLS)
    assert by_name["list_customers"]["enabled"] is True
    assert by_name["create_refund"]["enabled"] is False
    assert body["enabled_tools"] == ["list_customers"]


def test_unset_policy_marks_everything_enabled(auth_client, monkeypatch, tmp_path):
    """未設政策＝全開（`enabled_tools` 為 null，不是空陣列）。"""
    _install_manager(monkeypatch, tmp_path, session=_FakeSession(), policy={})
    body = auth_client.get("/v1/mcp/stripe/tools").json()
    assert body["enabled_tools"] is None
    assert all(t["enabled"] for t in body["tools"])


# ── 例外吞噬的邊界 ────────────────────────────────────────────────────────


def test_list_failure_degrades_to_empty_not_500(auth_client, monkeypatch, tmp_path):
    """列工具失敗（連線半死）→ 200＋空清單，不得退化成靜默 500；政策欄位仍須回傳。"""
    _install_manager(
        monkeypatch,
        tmp_path,
        session=_ExplodingSession(),
        policy={"disabled_tools": ["create_refund"]},
    )
    resp = auth_client.get("/v1/mcp/stripe/tools")
    assert resp.status_code == 200
    assert resp.json()["tools"] == []
    assert resp.json()["disabled_tools"] == ["create_refund"]
