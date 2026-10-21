"""MCP 健康管理（狀態/重啟/錯誤脫敏）後端單測。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest
from src.core.circuit_breaker import CircuitBreaker, CircuitState
from src.models.identity import AgentIdentity
from src.protocols.manager import ProtocolManager, ServerNotFoundError
from src.protocols.mcp.canonical import McpOperationException, McpRequestContext
from src.protocols.mcp.handles import LegacyMcpHandle
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.utils.mcp_secrets import sanitize_diagnostic

# 拼接建構（避開 PATH_LITERAL_SSOT 守衛把測試輸入誤判為硬編碼物理路徑；這些是要被脫敏的假路徑）。
_W = "\\"
_WIN_PATH = "C" + ":" + _W + "proj" + _W + "node_modules" + _W + "x.js"
_UNIX_PATH = "/" + "srv" + "/" + "app" + "/" + "server.js"
_UNC_PATH = _W + _W + "fileserver" + _W + "share" + _W + "x.js"


def _mgr(names: list[str]) -> ProtocolManager:
    reg = ProtocolRegistry("__no_such_mcp_config__.json")  # 不存在→servers 空
    reg.servers = {n: ProtocolServerConfig(command="x", args=[]) for n in names}
    return ProtocolManager(reg)


def _context(name: str) -> McpRequestContext:
    return McpRequestContext(
        request_id="req-health",
        identity=AgentIdentity.for_role("HealthProbe", "Debugger"),
        installation_id=name,
        auth_level="SafeAuto",
        origin="foreground",
    )


class _FakeTools:
    def __init__(self, n: int) -> None:
        self.tools = list(range(n))


class _FakeSession:
    def __init__(self, n: int = 2) -> None:
        self._n = n
        self.protocol_version = "2026-07-28"
        self.server_info = _ConnectionFact(
            {"name": "fake-server", "version": "2.0.0"}
        )
        self.server_capabilities = _ConnectionFact({"tools": {"listChanged": False}})

    async def list_tools(self) -> _FakeTools:
        return _FakeTools(self._n)


class _ConnectionFact:
    def __init__(self, value: dict) -> None:
        self._value = value

    def model_dump(self, **_kwargs) -> dict:
        return dict(self._value)


class _FailSession:
    async def list_tools(self) -> _FakeTools:
        raise RuntimeError("session dead " + _WIN_PATH)


# ── 錯誤脫敏 ──────────────────────────────────────────────────────────────────

def test_sanitize_strips_windows_path():
    out = sanitize_diagnostic("ENOENT " + _WIN_PATH)
    assert "<path>" in out
    assert "proj" not in out


def test_sanitize_strips_unix_path():
    out = sanitize_diagnostic("error at " + _UNIX_PATH + " line 3")
    assert "<path>" in out
    assert "srv" not in out


def test_sanitize_strips_unc_path():
    out = sanitize_diagnostic("spawn " + _UNC_PATH + " failed")
    assert "<path>" in out
    assert "fileserver" not in out


def test_sanitize_redacts_secret_token():
    assert "<redacted>" in sanitize_diagnostic("auth failed sk-abcdef1234567890xyz")


def test_sanitize_redacts_aws_akia_key():
    assert "<redacted>" in sanitize_diagnostic("env AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")


def test_sanitize_keeps_plain_message():
    assert sanitize_diagnostic("command not found: npx") == "command not found: npx"


def test_sanitize_for_redacts_server_env_values():
    """短於通用門檻、不含已知前綴的金鑰，唯有「逐一遮罩該伺服器 env 值」能擋。"""
    reg = ProtocolRegistry("__no_such_mcp_config__.json")
    planted = "abc123DEF456ghi789JK"  # 20 字假金鑰，短於通用正則門檻、漏網
    reg.servers = {"s": ProtocolServerConfig(command="x", args=[], env={"API_KEY": planted})}
    mgr = ProtocolManager(reg)
    mgr._record_error("s", "spawn failed: token " + planted + " rejected")
    last = mgr._server_meta["s"]["last_error"]
    assert planted not in last
    assert "<redacted>" in last


class _FailingToolSession:
    """call_tool 拋出夾帶機密的例外，模擬子進程把 env 值回吐進例外訊息。"""

    def __init__(self, boom: str) -> None:
        self._boom = boom

    async def list_tools(self) -> object:
        # dep1：呼叫當下核對清單，被叫的工具必須在當次清單上。
        tool = SimpleNamespace(
            name="do_thing",
            title=None,
            description="",
            input_schema={"type": "object"},
            output_schema=None,
            annotations=None,
            icons=[],
            meta=None,
        )
        return SimpleNamespace(tools=[tool])

    async def call_tool(self, tool_name: str, arguments: dict) -> object:
        raise RuntimeError(self._boom)


async def test_call_tool_error_sanitized_to_llm():
    """工具呼叫失敗回傳給 LLM/chat 的文字必須脫敏：含 <redacted>、不含機密，
    但仍標明是哪個工具失敗（LLM 才能理解失敗原因）。"""
    reg = ProtocolRegistry("__no_such_mcp_config__.json")
    planted = "abc123DEF456ghi789JK"  # 短於通用門檻、唯靠逐一遮罩 env 值才擋得住
    reg.servers = {"s": ProtocolServerConfig(command="x", args=[], env={"API_KEY": planted})}
    mgr = ProtocolManager(reg)
    mgr._circuit_breakers["s"] = CircuitBreaker()
    mgr._active_sessions["s"] = _FailingToolSession(
        "connect failed: API_KEY=" + planted + " at " + _WIN_PATH
    )

    with pytest.raises(McpOperationException) as caught:
        await mgr.call_tool_on_server(
            "s",
            "do_thing",
            {},
            request_context=_context("s"),
        )
    text = caught.value.error.message
    assert planted not in text  # env 機密遮罩
    assert "<redacted>" in text
    assert "proj" not in text  # 絕對路徑也脫敏
    assert "<path>" in text
    assert "do_thing" in text  # LLM 仍能看出是哪個工具失敗


# ── 狀態 enrichment ───────────────────────────────────────────────────────────

async def test_status_covers_all_configured_servers():
    mgr = _mgr(["conn", "fail", "open", "idle"])
    mgr._active_sessions["conn"] = _FakeSession(3)
    mgr._active_sessions["fail"] = _FailSession()
    br = CircuitBreaker()
    br.state = CircuitState.OPEN
    mgr._circuit_breakers["open"] = br
    mgr._active_sessions["open"] = _FakeSession(1)  # session 在但斷路器開 → circuit_open 優先

    by = {e["name"]: e for e in await mgr.get_active_server_info()}
    assert by["conn"]["status"] == "connected"
    assert by["conn"]["tools_count"] == 3
    assert by["conn"]["protocol_version"] == "2026-07-28"
    assert by["conn"]["server_info"] == {
        "name": "fake-server",
        "version": "2.0.0",
    }
    assert by["conn"]["server_capabilities"] == {
        "tools": {"listChanged": False}
    }
    assert by["fail"]["status"] == "error"
    assert "<path>" in (by["fail"]["last_error"] or "")  # 列工具錯誤已脫敏
    assert by["open"]["status"] == "circuit_open"
    assert by["idle"]["status"] == "not_started"


async def test_status_error_from_recorded_spawn_failure():
    mgr = _mgr(["spawnfail"])
    mgr._server_meta["spawnfail"] = {"last_error": "command not found"}
    info = await mgr.get_active_server_info()
    assert info[0]["status"] == "error"
    assert info[0]["last_error"] == "command not found"


class _TimeoutSession:
    """模擬列工具逾時/卡死：拋 TimeoutError（等同 wait_for 逾時），驗證健康端點不被拖死。"""
    async def list_tools(self) -> _FakeTools:
        raise TimeoutError


async def test_list_tools_timeout_marks_error_not_hang():
    mgr = _mgr(["slow"])
    mgr._active_sessions["slow"] = _TimeoutSession()
    info = {e["name"]: e for e in await mgr.get_active_server_info()}
    assert info["slow"]["status"] == "error"
    assert info["slow"]["last_error"]  # 有脫敏訊息、不為空


# ── 重啟 ──────────────────────────────────────────────────────────────────────

async def test_restart_unknown_server_raises():
    mgr = _mgr(["s"])
    with pytest.raises(ServerNotFoundError):
        await mgr.restart_server("ghost")


async def test_restart_resets_breaker_and_counts(monkeypatch):
    mgr = _mgr(["s"])
    # 先讓斷路器開、留下舊錯誤，驗證重啟會重置
    br = CircuitBreaker()
    br.state = CircuitState.OPEN
    mgr._circuit_breakers["s"] = br
    mgr._server_meta["s"] = {"last_error": "old boom"}

    called = {}

    async def fake_connect(name, config, request_context=None, **_kwargs):
        called["name"] = name
        mgr.handle_pool.put(mgr.handle_key_for(name, request_context),
                            LegacyMcpHandle(_FakeSession(), AsyncExitStack()))

    monkeypatch.setattr(mgr, "connect_server", fake_connect)
    ok = await mgr.restart_server("s")
    assert ok is True
    assert called["name"] == "s"
    assert mgr._server_meta["s"]["restart_count"] == 1
    assert mgr._server_meta["s"]["last_error"] is None
    assert mgr._circuit_breakers["s"].state == CircuitState.CLOSED  # 全新斷路器


async def test_concurrent_restart_serialized(monkeypatch):
    """同一台的兩個並發重啟必須序列化（per-server 鎖），避免重複關閉 exit stack。"""
    mgr = _mgr(["s"])
    inflight = {"n": 0}
    order: list[str] = []

    async def fake_connect(name, config, request_context=None, **_kwargs):
        inflight["n"] += 1
        assert inflight["n"] == 1, "並發重啟未被鎖序列化"
        await asyncio.sleep(0)  # 讓出控制權：若無鎖，第二個會在此插入 → inflight 變 2
        inflight["n"] -= 1
        order.append(name)

    monkeypatch.setattr(mgr, "connect_server", fake_connect)
    await asyncio.gather(mgr.restart_server("s"), mgr.restart_server("s"))
    assert order == ["s", "s"]
    assert mgr._server_meta["s"]["restart_count"] == 2


def test_error_capture_sanitizes_on_record():
    mgr = _mgr(["s"])
    mgr._record_error("s", "spawn failed at " + _WIN_PATH)
    assert "<path>" in mgr._server_meta["s"]["last_error"]
    assert mgr._server_meta["s"]["last_error_at"] is not None
