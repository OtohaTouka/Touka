"""畫面上的小提示（可視性第四批 PR 2b，ADR-0322 決定 9）.

工具計時、攔截、工具輪額度、中斷、警告這類系統說明，以前是一行文字混在 AI 的回覆裡（存成 AI
說的話，SNS 還會送到外面）。現在改送 ``turn_notice`` 事件，不進回覆文字；組歷史送給模型時，
舊對話 content 裡的這些行也拿掉（前後端同一份樣式與黃金案例）。
全部離線：工具執行器用假工具與假 hook，不讀 data/。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from src.models.policy import PolicyDecision
from src.services import tool_errors
from src.services.permission_service import permission_service
from src.utils.history_manager import sanitize_history
from src.utils.interjection_notes import annotate_node
from src.utils.turn_notices import (
    LEGACY_LOG_LINES,
    LEGACY_LOG_MAX_MIDDLE,
    MAX_NOTICE_CHARS,
    MAX_NOTICES_PER_NODE,
    NOTICE_APPROVAL_BLOCKED,
    NOTICE_HOOK_DENIED,
    NOTICE_KINDS,
    NOTICE_TOOL_BLOCKED,
    NOTICE_TOOL_EXCEPTION,
    NOTICE_TOOL_FAILED,
    NOTICE_TOOL_FINISHED,
    TURN_NOTICE_EVENT,
    strip_legacy_log_lines,
    tool_exception_notice,
    turn_notice_event,
)

from tests.support.hook_wiring import Recorder, executor_with, group, install_hooks, run_tool

_CHAT = Path(__file__).resolve().parents[2] / "ToukaChat" / "src" / "lib" / "chat"
_FRONTEND = _CHAT / "turnNotices.ts"
_GOLDEN = _CHAT / "__tests__" / "legacyLogLines.golden.json"
_READ = {"scope": "personal", "path": "a.txt"}
_WRITE = {"scope": "personal", "path": "a.txt", "content": "x"}


def _frontend_list(name: str) -> str:
    source = _FRONTEND.read_text(encoding="utf-8")
    match = re.search(rf"(?:export )?const {name}\b[^=]*= \[\n(.*?)\n\]", source, re.DOTALL)
    assert match, name
    return match.group(1)


def _frontend_number(name: str) -> int:
    match = re.search(rf"(?:export )?const {name} = (\d+);",
                      _FRONTEND.read_text(encoding="utf-8"))
    assert match, name
    return int(match.group(1))


def _notices(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == TURN_NOTICE_EVENT]


# ── 事件與前端常數 ──────────────────────────────────────────────────


def test_a_notice_event_has_a_fixed_kind_capped_params_and_display_text() -> None:
    event = turn_notice_event(
        NOTICE_TOOL_FINISHED, "\n\n✅ 完成" + "字" * 400 + "\n",
        tool="t" * 400, rounds=3, flag=True, nested={"a": 1},
    )
    assert event["type"] == TURN_NOTICE_EVENT
    assert event["kind"] == NOTICE_TOOL_FINISHED
    assert event["text"].startswith("✅ 完成")  # 前後的換行不留
    assert len(event["text"]) == MAX_NOTICE_CHARS
    assert event["params"] == {"tool": "t" * MAX_NOTICE_CHARS, "rounds": 3}  # 只收字串與整數


def test_the_tool_exception_notice_is_scrubbed_before_anyone_sees_it() -> None:
    event = tool_exception_notice("read", "RuntimeError: failed Bearer abc123 key=abcdefgh")
    shown = json.dumps(event, ensure_ascii=False)
    assert event["kind"] == NOTICE_TOOL_EXCEPTION
    assert "RuntimeError: failed" in event["params"]["detail"]
    for secret in ("abc123", "abcdefgh"):
        assert secret not in shown


def test_event_name_kinds_and_caps_are_identical_to_the_frontend() -> None:
    source = _FRONTEND.read_text(encoding="utf-8")
    assert f"export const TURN_NOTICE_EVENT = '{TURN_NOTICE_EVENT}';" in source
    assert re.findall(r"'([^']*)'", _frontend_list("TURN_NOTICE_KINDS")) == list(NOTICE_KINDS)
    assert _frontend_number("MAX_NOTICES_PER_NODE") == MAX_NOTICES_PER_NODE
    assert _frontend_number("MAX_NOTICE_CHARS") == MAX_NOTICE_CHARS
    assert _frontend_number("LEGACY_LOG_MAX_MIDDLE") == LEGACY_LOG_MAX_MIDDLE


def test_legacy_log_line_patterns_are_identical_to_the_frontend() -> None:
    pairs = re.findall(r"\['((?:[^'\\]|\\.)*)', '((?:[^'\\]|\\.)*)'\]",
                       _frontend_list("LEGACY_LOG_LINES"))
    decoded = [tuple(part.replace("\\n", "\n") for part in pair) for pair in pairs]
    assert decoded == list(LEGACY_LOG_LINES)


# ── 舊對話裡的日誌行：送給模型時拿掉 ─────────────────────────────────


@pytest.mark.parametrize(
    "case", json.loads(_GOLDEN.read_text(encoding="utf-8")), ids=lambda case: case["name"]
)
def test_legacy_log_lines_are_stripped_like_the_frontend(case: dict[str, str]) -> None:
    assert strip_legacy_log_lines(case["input"]) == case["expected"]


def test_history_for_the_model_drops_old_log_lines_but_never_shows_notices() -> None:
    node = {
        "role": "assistant", "content": "先查\n✅ `時間記錄：2026-11-18 10:00:00 (週三)`\n好了",
        "notices": [{"kind": NOTICE_TOOL_FINISHED, "params": {"tool": "read"}}],
    }
    assert annotate_node(node)["content"] == "先查\n好了"
    user = {"role": "user", "content": "✅ `時間記錄：我自己打的`\n"}
    assert annotate_node(user) is user  # 只動 AI 的回覆
    assert "read" not in annotate_node({**node, "content": "好了"}).get("content", "")


def test_sanitize_history_leaves_no_residue_of_the_time_line_or_bold_warnings() -> None:
    messages = [{
        "role": "assistant",
        "content": "答案\n✅ `時間記錄：2026-11-18 10:00:00 (週三)`\n收尾\n\n"
                   "⚠️ **[工具幻覺警告]** 您模擬了工具 `read` 但未正式呼叫。",
    }]
    sanitize_history(messages)
    assert messages[0]["content"] == "答案\n收尾"


# ── 工具執行器：改送小提示，不再送日誌文字 ────────────────────────────


@pytest.fixture
def hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    return install_hooks(tmp_path, monkeypatch)


_EXPLOSION = "exploded Bearer abc123"


class _Raises:
    async def __call__(self, **_kwargs: Any) -> str:
        raise RuntimeError(_EXPLOSION)


async def test_a_finished_tool_sends_a_timing_notice_not_log_text(monkeypatch, hooks) -> None:
    hooks.configure({})
    _result, events = await run_tool(executor_with(monkeypatch, {"read": Recorder()}), "read",
                                     dict(_READ))
    assert all(e.get("type") != "log" for e in events)
    [notice] = _notices(events)
    assert (notice["kind"], notice["params"]["tool"]) == (NOTICE_TOOL_FINISHED, "read")
    assert notice["params"]["time"]


async def test_a_failed_tool_sends_a_failure_notice(monkeypatch, hooks) -> None:
    hooks.configure({})
    failing = Recorder(tool_errors.build_tool_error(tool_errors.UPSTREAM_ERROR, "boom"))
    _result, events = await run_tool(executor_with(monkeypatch, {"read": failing}), "read",
                                     dict(_READ))
    assert [n["kind"] for n in _notices(events)] == [NOTICE_TOOL_FAILED]


async def test_a_tool_exception_notice_carries_the_scrubbed_detail(monkeypatch, hooks) -> None:
    hooks.configure({})
    _result, events = await run_tool(executor_with(monkeypatch, {"read": _Raises()}), "read",
                                     dict(_READ))
    [notice] = _notices(events)
    assert notice["kind"] == NOTICE_TOOL_EXCEPTION
    assert "exploded" in notice["params"]["detail"]
    assert "abc123" not in json.dumps(notice, ensure_ascii=False)


async def test_a_policy_block_sends_a_blocked_notice(monkeypatch, hooks) -> None:
    hooks.configure({})
    executor = executor_with(monkeypatch, {"read": Recorder()})
    monkeypatch.setattr(permission_service, "evaluate_policy",
                        lambda _ctx: PolicyDecision(effect="DENY", reason="test"))
    _result, events = await run_tool(executor, "read", dict(_READ))
    assert [(n["kind"], n["params"]["tool"]) for n in _notices(events)] == [
        (NOTICE_TOOL_BLOCKED, "read")]


def _hook_says(hooks: Any, event: str, **fields: Any) -> None:
    hooks.configure({event: [group()]})
    body = {"hookSpecificOutput": {"hookEventName": event, **fields}}
    hooks.http.bodies[event] = json.dumps(body)


async def test_a_permission_hook_denial_sends_a_hook_notice(monkeypatch, hooks) -> None:
    _hook_says(hooks, "PermissionRequest", decision={"behavior": "deny"})
    executor = executor_with(monkeypatch, {"write": Recorder()})
    _result, events = await run_tool(executor, "write", dict(_WRITE), mode="Manual")
    assert [n["kind"] for n in _notices(events)] == [NOTICE_HOOK_DENIED]


async def test_an_approval_with_nobody_there_sends_a_blocked_notice(monkeypatch, hooks) -> None:
    _hook_says(hooks, "PreToolUse", permissionDecision="ask")
    executor = executor_with(monkeypatch, {"read": Recorder()})
    _result, events = await run_tool(executor, "read", dict(_READ), present=False)
    assert [n["kind"] for n in _notices(events)] == [NOTICE_APPROVAL_BLOCKED]


def test_every_notice_kind_has_a_screen_text_in_both_languages() -> None:
    locales = Path(__file__).resolve().parents[2] / "ToukaChat" / "src" / "locales"
    for language in ("en", "zh-TW"):
        catalog = json.loads((locales / language / "translation.json").read_text(encoding="utf-8"))
        texts = catalog["chat"]["turn_notice"]
        assert set(NOTICE_KINDS) <= set(texts), language
