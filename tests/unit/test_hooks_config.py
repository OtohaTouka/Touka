"""Hook 設定模型與比對規則（ADR-0292）。

- 只收已接線的事件、command／http 兩種類型；拼錯或 Claude Code 才有的欄位
  （shell、if、async）一律拒收。
- 預設關閉；範本 ``hooks.json.example`` 通過模型。
- http 只收 http／https；標頭值不能寫成 ``$VAR``／``${VAR}``（不替換環境變數，寫了會誤以為有效）。
- 比對規則照 Claude Code：空字串與 ``*`` 全中；只有字母、數字、``_``、``-``、空白、``,``、``|``
  時是精確字串或清單；其他字元當正規表示式、不錨定、區分大小寫。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from src.models.hooks import WIRED_EVENTS, HooksConfig
from src.services.hooks.matcher import matcher_matches

_EXAMPLE = Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "hooks.json.example"


def _config(event: str, handler: dict, matcher: str = "") -> dict:
    return {"enabled": True, "hooks": {event: [{"matcher": matcher, "hooks": [handler]}]}}


_COMMAND = {"type": "command", "command": "/usr/bin/true"}


def test_default_is_disabled_and_example_validates():
    assert HooksConfig().enabled is False
    example = HooksConfig.model_validate(json.loads(_EXAMPLE.read_text(encoding="utf-8")))
    assert example.enabled is False


def test_wired_events_are_the_first_four():
    assert set(WIRED_EVENTS) == {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}


def test_command_and_http_hooks_validate():
    cfg = HooksConfig.model_validate(_config("PreToolUse", {**_COMMAND, "args": ["-x"]}, "Bash"))
    handler = cfg.hooks["PreToolUse"][0].hooks[0]
    assert handler.args == ["-x"]
    assert handler.timeout == 30
    http = HooksConfig.model_validate(
        _config("Stop", {"type": "http", "url": "https://example.test/hook",
                         "headers": {"Authorization": "Bearer literal"}})
    )
    assert http.hooks["Stop"][0].hooks[0].headers == {"Authorization": "Bearer literal"}


@pytest.mark.parametrize(
    ("event", "handler"),
    [
        ("SessionStart", _COMMAND),  # 還沒接線的事件
        ("pretooluse", _COMMAND),  # 事件名稱區分大小寫
        ("PreToolUse", {"type": "prompt", "prompt": "x"}),  # 這次只做 command、http
        ("PreToolUse", {**_COMMAND, "shell": "bash"}),  # 不經 shell
        ("PreToolUse", {**_COMMAND, "if": "Bash(rm *)"}),
        ("PreToolUse", {**_COMMAND, "async": True}),
        ("PreToolUse", {**_COMMAND, "command": ""}),
        ("PreToolUse", {**_COMMAND, "timeout": 0}),
        ("PreToolUse", {**_COMMAND, "timeout": 301}),
        ("PreToolUse", {"type": "http", "url": "file:///etc/passwd"}),
        ("PreToolUse", {"type": "http", "url": "ftp://example.test/x"}),
        ("PreToolUse", {"type": "http", "url": "https://example.test:99999/x"}),  # 埠號超出範圍
        ("PreToolUse", {"type": "http", "url": "https://example.test:abc/x"}),
        ("PreToolUse", {"type": "http", "url": "https://example.test:0/x"}),
        ("PreToolUse", {"type": "http", "url": "https://[::1/x"}),
        ("PreToolUse", {"type": "http", "url": "https://example.test",
                        "headers": {"Authorization": "Bearer $TOKEN"}}),
        ("PreToolUse", {"type": "http", "url": "https://example.test",
                        "headers": {"X-Key": "${API_KEY}"}}),
        # 標頭名稱要是 HTTP 的 token；Touka 自己送的標頭不能改（不分大小寫）
        ("PreToolUse", {"type": "http", "url": "https://example.test",
                        "headers": {"X-Key: injected": "v"}}),
        ("PreToolUse", {"type": "http", "url": "https://example.test",
                        "headers": {"X Key": "v"}}),
        ("PreToolUse", {"type": "http", "url": "https://example.test",
                        "headers": {"content-type": "text/plain"}}),
        ("PreToolUse", {"type": "http", "url": "https://example.test",
                        "headers": {"Content-Length": "1"}}),
        ("PreToolUse", {"type": "http", "url": "https://example.test",
                        "headers": {"HOST": "evil.test"}}),
        ("PreToolUse", {"type": "http", "url": "https://example.test",
                        "headers": {"Transfer-Encoding": "chunked"}}),
    ],
)
def test_rejects_unsupported_or_unsafe_entries(event, handler):
    with pytest.raises(ValidationError):
        HooksConfig.model_validate(_config(event, handler))


def test_rejects_unknown_top_level_keys_and_bad_regex():
    with pytest.raises(ValidationError):
        HooksConfig.model_validate({"enabled": True, "disableAllHooks": True})
    with pytest.raises(ValidationError):
        HooksConfig.model_validate(_config("PreToolUse", _COMMAND, "Edit(["))


def test_rejects_too_many_hooks_for_one_event():
    groups = [{"matcher": "", "hooks": [_COMMAND]} for _ in range(21)]
    with pytest.raises(ValidationError):
        HooksConfig.model_validate({"enabled": True, "hooks": {"PreToolUse": groups}})


@pytest.mark.parametrize(
    ("matcher", "values", "expected"),
    [
        ("", ["anything"], True),
        ("*", ["anything"], True),
        ("terminal_execute", ["terminal_execute"], True),
        ("terminal_execute", ["terminal"], False),
        ("Edit|Write", ["Write"], True),
        ("Edit, Write", ["Edit"], True),
        ("edit", ["Edit"], False),  # 區分大小寫
        ("Edit.*", ["NotebookEdit"], True),  # 正規表示式不錨定
        ("^Edit$", ["NotebookEdit"], False),
        ("mcp__memory__.*", ["vault_read", "mcp__memory__create_entities"], True),
        ("mcp__memory", ["mcp__memory__create_entities"], False),  # 精確字串
        ("Bash", [], False),
    ],
)
def test_matcher_follows_claude_code(matcher, values, expected):
    assert matcher_matches(matcher, values) is expected
