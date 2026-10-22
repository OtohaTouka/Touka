"""ADR-0246 三模式（U2c-4）：舊值讀取時對照、模式授權純函式、Auto 決策、動態 shell 寫法、
safe_whitelist 退役、MCP MRTR 等級排序。

純函式／純模型測試：不碰 singleton、不寫磁碟；規則範本只讀 tracked ``.example``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext
from src.models.safety import AuthPoliciesConfig, SafetyRules
from src.protocols.manager import _stricter_auth_level
from src.utils.auth_mode import authorize_auth_mode, normalize_auth_mode
from src.utils.command_safety import has_dynamic_shell_syntax

_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "auth_policies.json.example"
)
_ACTOR = AgentIdentity.for_role(agent_id="ModeProbe", role="Owner", is_human=False)


def _config(**policies_override: object) -> AuthPoliciesConfig:
    data = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    data["policies"].update(policies_override)
    return AuthPoliciesConfig.model_validate(data)


def _effect(
    level: str,
    resource: str,
    *,
    action: str = "tool_call",
    tool_action: str | None = None,
    config: AuthPoliciesConfig | None = None,
) -> str:
    environment: dict[str, object] = {"auth_level": level, "isolation": "landlock"}
    if tool_action is not None:
        environment["tool_action"] = tool_action
    context = PolicyContext(
        actor=_ACTOR, resource=resource, action=action, environment=environment
    )
    return (config or _config()).evaluate(context).effect


# ── 讀取時對照（不遷移資料庫；存檔時自然寫新名稱）─────────────────────────────


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("Manual", "Manual"),
        ("Auto", "Auto"),
        ("YOLO", "YOLO"),
        ("ReadOnly", "Manual"),
        ("SafeAuto", "Manual"),  # 不會悄悄開始送審、也不自己多做事（ADR-0246 決定 3）
        ("FullYOLO", "YOLO"),
        ("", "Manual"),
        ("yolo", "Manual"),
        ("GodMode", "Manual"),
        (None, "Manual"),
        (3, "Manual"),
    ],
)
def test_stored_mode_read_mapping(stored: object, expected: str):
    assert normalize_auth_mode(stored) == expected


# ── 模式授權：只有 admin 或該靈魂的擁有者能設 Auto／YOLO ─────────────────────


@pytest.mark.parametrize(
    ("requested", "is_admin", "owns_agent", "expected"),
    [
        ("Auto", False, False, "Manual"),
        ("YOLO", False, False, "Manual"),
        ("Auto", True, False, "Auto"),
        ("YOLO", True, False, "YOLO"),
        ("Auto", False, True, "Auto"),
        ("YOLO", False, True, "YOLO"),
        ("Manual", False, False, "Manual"),
    ],
)
def test_authorize_auth_mode(requested: str, is_admin: bool, owns_agent: bool, expected: str):
    assert authorize_auth_mode(requested, is_admin=is_admin, owns_agent=owns_agent) == expected


# ── 動態 shell 寫法（ADR-0246 決定 4：只有人能核准的那一半）─────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "echo $HOME",
        "echo ${PATH}",
        "ls $(pwd)",
        "echo `id`",
        "echo %USERPROFILE%",
        "diff <(ls a) <(ls b)",
        "ls > out.txt",
        "ls >> log.txt",
        "pytest 2> err.txt",
        "eval ls",
        "Invoke-Expression 'ls'",
        "iex 'ls'",
        "Write-Output $env:PATH",
        "powershell -enc ZQBjAGgAbwA=",
        "pwsh -NoProfile -EncodedCommand ZQBjAGgAbwA=",
        "powershell.exe -e ZQBjAGgAbwA=",
    ],
)
def test_dynamic_shell_syntax_detected(command: str):
    assert has_dynamic_shell_syntax(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status | head -5",  # 簡單管線：之後交給審查員
        "grep -r 'foo bar' *.py",  # 引號、萬用字元：之後交給審查員
        "pytest tests/ 2>&1",
        "ls > /dev/null",
        "dir > nul",
        "grep -e pattern file.txt",
        "python evaluate.py",
        "powershell -ExecutionPolicy Bypass -File build.ps1",
        "grep 'end$' notes.txt",
    ],
)
def test_plain_commands_are_not_dynamic(command: str):
    assert has_dynamic_shell_syntax(command) is False


# ── Auto 決策（U2c-3：終端與 dangerous 工具交給審查員＝REVIEW）─────────────────────


@pytest.mark.parametrize(
    ("resource", "action", "tool_action", "expected"),
    [
        ("read", "tool_call", None, "ALLOW"),
        ("write", "tool_call", None, "ALLOW"),  # safe 類直接放行（ADR-0246 決定 7）
        ("memory_tools", "tool_call", "save", "ALLOW"),
        ("mission_tools", "tool_call", "request_review", "ALLOW"),
        ("delete", "tool_call", None, "REVIEW"),  # dangerous 工具交給審查員（ADR-0246 決定 3）
        ("some_unlisted_mcp_tool", "tool_call", None, "REVIEW"),
        ("request_skill_install", "tool_call", None, "HUMAN_ONLY"),  # F5（ADR-0246 決定 6）
        ("agent_control", "tool_call", "switch_brain", "HUMAN_ONLY"),  # F5
        ("send_message", "tool_call", None, "HUMAN_ONLY"),  # F6
        ("request_skill_install", "schema_injection", None, "HUMAN_ONLY"),  # 仍廣告
        ("ls -la", "terminal_execute", None, "REVIEW"),  # 交給審查員，不再有白名單
        ("pytest tests/", "terminal_execute", None, "REVIEW"),
        ("echo $HOME", "terminal_execute", None, "HUMAN_ONLY"),  # ADR-0246 決定 4
        ("ls > out.txt", "terminal_execute", None, "HUMAN_ONLY"),
        ("sudo echo $HOME", "terminal_execute", None, "DENY"),  # 黑名單仍優先
    ],
)
def test_auto_mode_decisions(resource: str, action: str, tool_action: str | None, expected: str):
    assert _effect("Auto", resource, action=action, tool_action=tool_action) == expected


@pytest.mark.parametrize(("level", "expected"), [("Manual", "CONFIRM"), ("YOLO", "ALLOW")])
def test_dynamic_shell_is_auto_only(level: str, expected: str):
    """動態 shell 寫法只在 Auto 下變成 HUMAN_ONLY；Manual 照常問人、YOLO 照常放行。"""
    assert _effect(level, "echo $HOME", action="terminal_execute") == expected


# ── safe_whitelist 退役：範本拿掉、舊檔留著的鍵讀得進來但不生效 ─────────────────


def test_example_has_no_safe_whitelist():
    data = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    assert "safe_whitelist" not in data["policies"]
    assert "safe_whitelist" not in SafetyRules.model_fields


def test_legacy_safe_whitelist_key_is_ignored():
    config = _config(safe_whitelist=[".*"])
    assert _effect("Auto", "ls -la", action="terminal_execute", config=config) == "REVIEW"
    assert _effect("Manual", "ls -la", action="terminal_execute", config=config) == "CONFIRM"


# ── MCP MRTR 重驗：有效等級只能維持或收緊（三級）────────────────────────────────


@pytest.mark.parametrize(
    ("original", "current", "expected"),
    [
        ("YOLO", "Auto", "Auto"),
        ("Auto", "YOLO", "Auto"),
        ("Auto", "Manual", "Manual"),
        ("YOLO", "YOLO", "YOLO"),
        ("YOLO", "FullYOLO", "Manual"),  # 舊名稱＝看不懂＝最嚴
        ("SafeAuto", "Auto", "Manual"),
    ],
)
def test_mrtr_stricter_auth_level(original: str, current: str, expected: str):
    assert _stricter_auth_level(original, current) == expected
