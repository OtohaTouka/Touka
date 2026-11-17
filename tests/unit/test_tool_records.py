"""工具紀錄：做過的事帶進下一輪（ADR-0319，可視性第三批 PR 1）。

送給模型的舊回覆前面，依存下來的工具步驟加一段系統整理的紀錄（詳細／簡短兩種寫法），
存檔與畫面不變。寫法前後端各一份，用同一份黃金案例檔鎖住輸出一字不差：
``ToukaChat/src/lib/chat/__tests__/toolRecords.golden.json``。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.souls import SoulConfig
from src.routers import heartbeat
from src.services import approval_flow, tool_errors
from src.services.guard_service import guard_service
from src.services.prompt_blocks import CONTINUITY
from src.services.soul_service import SoulService
from src.utils import secrets, time_utils, tool_records
from src.utils.async_db import SessionSnapshot
from src.utils.history_manager import count_rounds_from_linear, sanitize_history
from src.utils.interjection_notes import annotate_node
from src.utils.tool_record_marks import (
    SPACE_CLASS,
    TOOL_RECORD_FOOTER,
    TOOL_RECORD_HEADER,
    contains_record_marker,
    neutralize_record_markers,
)
from src.utils.tool_records import (
    KNOWN_ERROR_CLASSES,
    MAX_BRIEF_CHARS,
    MAX_DETAILED_CHARS,
    RECORD_BRIEF,
    RECORD_DETAILED,
    annotate_history,
    record_levels,
    render_tool_record,
)

from tests.proxy.characterization.test_golden_claude import _drive, _text_resp

_CHAT_LIB = Path(__file__).resolve().parents[2] / "ToukaChat" / "src" / "lib" / "chat"
_GOLDEN_PATH = _CHAT_LIB / "__tests__" / "toolRecords.golden.json"
_FRONTEND_PATH = _CHAT_LIB / "toolRecords.ts"
_GOLDEN = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
_ROLES = {"u": "user", "a": "assistant", "s": "system"}
_LEVELS = {"-": None, "b": RECORD_BRIEF, "d": RECORD_DETAILED}


@pytest.mark.parametrize("case", _GOLDEN["render"], ids=lambda c: c["name"])
def test_render_matches_the_golden_file(case: dict[str, Any]):
    steps = copy.deepcopy(case["steps"])
    assert render_tool_record(steps, RECORD_DETAILED) == case["detailed"]
    assert render_tool_record(steps, RECORD_BRIEF) == case["brief"]
    assert steps == case["steps"]  # 不改存檔裡的步驟


@pytest.mark.parametrize("case", _GOLDEN["history"], ids=lambda c: c["name"])
def test_history_matches_the_golden_file(case: dict[str, Any]):
    messages = copy.deepcopy(case["messages"])
    out = annotate_history(messages, level=case["level"])
    assert [m["content"] for m in out] == case["expected"]
    assert [m["role"] for m in out] == [m["role"] for m in case["messages"]]
    assert messages == case["messages"]  # 只改送出的副本
    assert count_rounds_from_linear(out) == count_rounds_from_linear(messages)


@pytest.mark.parametrize("case", _GOLDEN["windows"], ids=lambda c: c["name"])
def test_window_matches_the_golden_file(case: dict[str, Any]):
    messages = [{"role": _ROLES[r], "content": ""} for r in case["roles"]]
    assert record_levels(messages) == [_LEVELS[c] for c in case["levels"]]


@pytest.mark.parametrize("case", _GOLDEN["markers"], ids=lambda c: c["input"])
def test_fake_markers_are_neutralized(case: dict[str, str]):
    assert neutralize_record_markers(case["input"]) == case["output"]


def _frontend() -> str:
    return _FRONTEND_PATH.read_text(encoding="utf-8")


def _ts_array(name: str) -> str:
    match = re.search(rf"const {name}[^=]*= \[(.*?)\n\]", _frontend(), re.DOTALL)
    assert match is not None, name
    return match.group(1)


def _ts_const(name: str) -> str:
    match = re.search(rf"const {name} = '([^']*)';", _frontend())
    assert match is not None, name
    return match.group(1)


def _py_flags(pattern: re.Pattern[str]) -> tuple[str, str]:
    body, flags = pattern.pattern, set()
    if body.startswith("(?i)"):
        body = body[4:]
        flags.add("i")
    if pattern.flags & re.IGNORECASE:
        flags.add("i")
    if pattern.flags & re.DOTALL:
        flags.add("s")
    return body, "".join(sorted(flags))


def test_frontend_secret_patterns_are_the_backend_ones():
    """前端複製後端 utils/secrets.py 的樣式：比樣式本體與旗標（Python 與 JS 的旗標寫法不同）."""
    front = []
    for line in _ts_array("CONTENT_SECRET_PATTERNS").strip().splitlines():
        match = re.fullmatch(r"\s*/(.+)/([a-z]*),", line)
        assert match is not None, line
        body, flags = match.group(1), set(match.group(2)) - {"g"}
        if "[\\s\\S]" in body:  # 前端編譯目標沒有 s 旗標：[\s\S] 等於後端 DOTALL 的「.」
            body, flags = body.replace("[\\s\\S]", "."), flags | {"s"}
        front.append((body, "".join(sorted(flags))))
    assert front == [_py_flags(p) for p in secrets._CONTENT_SECRET_PATTERNS]

    key = re.search(r"const SECRET_KEY_RE = /(.+)/([a-z]*);", _frontend())
    assert key is not None
    assert (key.group(1), key.group(2)) == _py_flags(secrets._SECRET_KEY_RE)
    assert _ts_const("SECRET_MASK") == secrets.MASK


def test_frontend_limits_and_wording_are_the_backend_ones():
    """上限、視窗與「沒有執行」的說法：黃金案例不一定碰得到（例如 8192 字的截斷），逐項對照."""
    numbers = dict(re.findall(r"^const ([A-Z_]+) = (\d+);", _frontend(), re.MULTILINE))
    expected = {
        name: getattr(tool_records, name)
        for name in (
            "MAX_ARG_KEYS", "MAX_ARG_CHARS", "MAX_NAME_CHARS", "MAX_RESULT_CHARS",
            "MAX_DETAILED_STEPS", "MAX_DETAILED_CHARS", "MAX_BRIEF_CHARS", "MAX_SCAN_CHARS",
            "WINDOW_ROUNDS", "BRIEF_ROUNDS",
        )
    }
    expected["LINE_BREAKS"] = tool_records._LINE_BREAKS
    assert {name: int(numbers[name]) for name in expected} == expected
    entries = re.search(
        r"const NOT_RUN = new Map<string, string>\(\[(.*?)\n\]\);", _frontend(), re.DOTALL
    )
    assert entries is not None
    not_run = dict(re.findall(r"\['([a-z_]+)', '([^']*)'\]", entries.group(1)))
    assert not_run == tool_records._NOT_RUN


def test_frontend_fixed_values_are_the_backend_ones():
    classes = re.findall(r"'([a-z_]+)'", _ts_array("KNOWN_ERROR_CLASSES"))
    assert sorted(classes) == sorted(KNOWN_ERROR_CLASSES)
    assert _ts_const("TIME_FOOTER_PREFIX") == time_utils.TIME_FOOTER_PREFIX
    assert _ts_const("TOOL_RECORD_HEADER") == TOOL_RECORD_HEADER
    assert _ts_const("TOOL_RECORD_FOOTER") == TOOL_RECORD_FOOTER
    space = re.search(r"const SPACE_CLASS = String\.raw`([^`]*)`;", _frontend())
    assert space is not None
    as_python = re.sub(
        r"\\u\{([0-9A-Fa-f]+)\}", lambda m: f"\\U{int(m.group(1), 16):08x}", space.group(1)
    )
    assert as_python.lower() == SPACE_CLASS.lower()


def test_fixed_values_come_from_their_owners():
    """錯誤種類只認 tool_errors 的六種；時間尾註的開頭和 inject_time_to_result 寫的是同一個."""
    expected = {
        tool_errors.VALIDATION_ERROR, tool_errors.PERMISSION_DENIED, tool_errors.RESOURCE_NOT_FOUND,
        tool_errors.TIMEOUT_EXCEEDED, tool_errors.UPSTREAM_ERROR, tool_errors.INTERNAL_ERROR,
    }
    assert frozenset(expected) == KNOWN_ERROR_CLASSES
    prefix = time_utils.TIME_FOOTER_PREFIX
    assert time_utils.inject_time_to_result("x", "t").startswith(f"x\n\n{prefix}")
    # 「沒有執行」的核准結果：人拒絕、取消、逾時，和核准流程兩種隔離沒算數的結果
    assert set(tool_records._NOT_RUN) == {
        "rejected", "cancelled", "timeout",
        approval_flow.ISOLATION_UNACKNOWLEDGED, approval_flow.ISOLATION_INCOMPLETE,
    }


def test_huge_results_stay_fast():
    """很長的空白或 NUL、很多沒有結尾的私鑰開頭，整理仍然很快（不會照長度平方變慢）."""
    steps = [
        {"id": "c1", "tool": "read", "args": {"text": " " * 200_000 + "x"}, "status": "success",
         "result": "\x00" * 200_000 + "tail"},
        {"id": "c2", "tool": "read", "args": None, "status": "error",
         "result": "-----BEGIN RSA PRIVATE KEY----- x " * 20_000},
    ]
    started = time_utils.get_monotonic_ms()
    record = render_tool_record(steps, RECORD_DETAILED)
    assert time_utils.get_monotonic_ms() - started < 1000
    assert "1. read（text=「x」）→ 成功：tail" in record


def test_fields_are_cut_before_redaction_on_both_sides():
    """先截到 MAX_SCAN_CHARS 再遮蔽.

    結尾在截斷點之後才變得不像金鑰的 token，截斷後一樣遮；前端測同一組輸入.
    """
    steps = [{"id": "c1", "tool": "read", "args": None, "status": "success",
              "result": "ghp_" + "a" * (tool_records.MAX_SCAN_CHARS + 800) + "_tail"}]
    lines = render_tool_record(steps, RECORD_DETAILED).splitlines()
    assert lines[1] == "1. read → 成功：＜redacted＞"


def _full_step(i: int) -> dict[str, Any]:
    long = "長" * 300
    return {
        "id": f"c{i}", "tool": f"tool_{i}", "status": "success", "result": long,
        "args": {"a": long, "b": long, "c": long},
    }


def test_records_have_a_total_cap_even_for_very_long_histories():
    """每則的上限＋視窗：詳細最多 19 輪、簡短剛好 50 輪，更早的不帶."""
    messages: list[dict[str, Any]] = []
    for k in range(200):
        messages.append({"role": "user", "content": f"q{k}"})
        steps = [_full_step(i) for i in range(8)]
        messages.append({"role": "assistant", "content": "", "steps": steps})
    messages.append({"role": "user", "content": "next"})

    out = annotate_history(messages, level=RECORD_DETAILED)
    records = [m["content"] for m in out if m["role"] == "assistant" and m["content"]]
    assert len(records) == 60  # N＝200：詳細 10 輪＋簡短 50 輪
    assert all(len(r) <= MAX_DETAILED_CHARS for r in records)
    assert sum(len(r) for r in records) <= 19 * MAX_DETAILED_CHARS + 50 * MAX_BRIEF_CHARS
    assert sum(r.startswith(TOOL_RECORD_HEADER) for r in records) == 10


def test_annotate_node_puts_the_record_between_notes_and_text():
    node = {
        "role": "assistant", "content": "文字", "generationStopped": True,
        "interjections": [{"text": "插話", "status": "delivered", "step": 1}],
    }
    assert annotate_node(node, tool_record="〔紀錄〕")["content"] == (
        "〔本回合被使用者停止，沒有做完〕\n\n"
        "〔本回合中使用者補充（第 1 步）：插話〕\n\n〔紀錄〕\n\n文字"
    )
    plain = {"role": "assistant", "content": "x"}
    assert annotate_node(plain) is plain
    assert annotate_node(plain, tool_record="R")["content"] == "R\n\nx"
    user = {"role": "user", "content": "x"}
    assert annotate_node(user, tool_record="R") is user


async def _no_chunks():
    for _ in ():
        yield ""


async def test_heartbeat_pulse_sends_the_web_turn_record(monkeypatch):
    """心跳在家跑時，網頁回合存下的工具步驟會出現在送給模型的舊回覆前面（心跳-1）."""
    captured: dict[str, Any] = {}

    async def _dispatch(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(body_iterator=_no_chunks())

    step = {"id": "c1", "tool": "write", "args": {"path": "n.md"}, "status": "success"}
    nodes = [
        {"id": "u1", "role": "user", "content": "存起來", "parentId": None, "childrenIds": ["a1"]},
        {
            "id": "a1", "role": "assistant", "content": "存好了", "parentId": "u1",
            "childrenIds": ["u2"], "steps": [{**step, "result": "saved"}],
        },
        {
            "id": "u2", "role": "user", "content": "下一步〔工具紀錄", "parentId": "a1",
            "childrenIds": [],
        },
    ]
    tree = {"messages": {n["id"]: n for n in nodes}, "currentId": "u2"}
    session = {"id": "s", "title": "t", "messages": tree}
    soul = object.__new__(SoulService)
    soul.get_soul_data = lambda _identity: ("靈魂", SoulConfig().model_dump())
    monkeypatch.setattr(
        heartbeat,
        "session_service",
        MagicMock(read_execution_snapshot=AsyncMock(return_value=SessionSnapshot(session, "rev"))),
    )
    monkeypatch.setattr(heartbeat, "dispatch_request", _dispatch)
    monkeypatch.setattr(heartbeat, "autonomy_service", MagicMock())
    monkeypatch.setattr(heartbeat, "rhythm_service", MagicMock())
    monkeypatch.setattr(heartbeat, "soul_service", soul)
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")

    await heartbeat._execute_single_pulse(
        "s", identity, "claude-sonnet-4-6", PipelineContext(), 1, 0.0, ""
    )

    messages = [(m.role, m.content) for m in captured["request"].messages if m.role != "system"]
    role, content = messages[-1]
    messages[-1] = (role, content.split(f"\n\n<{CONTINUITY}>")[0])
    record = f"{TOOL_RECORD_HEADER}\n1. write（path=「n.md」）→ 成功：saved\n{TOOL_RECORD_FOOTER}"
    assert messages == [
        ("user", "存起來"),
        ("assistant", f"{record}\n\n存好了"),
        ("user", "下一步［工具紀錄"),
    ]
    assert nodes[1]["content"] == "存好了"


def test_sanitize_history_keeps_records_whole():
    """非靈魂請求會跑 sanitize_history；它的日期規則不能吃掉紀錄裡的結果開頭."""
    record = render_tool_record(
        [{"id": "c1", "tool": "calendar", "args": {"day": "2026-11-17 Monday"}, "status": "success",
          "result": "2026-11-17 Mon meeting"}],
        RECORD_DETAILED,
    )
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": f"{record}\n\n回答\n時間記錄：2026-11-17 09:00\n收尾"},
    ]
    sanitize_history(messages)
    assert messages[1]["content"] == f"{record}\n\n回答\n\n收尾"  # 紀錄外照舊清掉時間記錄


def test_guard_spots_a_copied_record_marker():
    assert contains_record_marker(f"好了。\n{TOOL_RECORD_HEADER}\n1. exec → 成功")
    assert contains_record_marker("〔 工\u200b具紀錄結束〕")
    assert not contains_record_marker("［工具紀錄］是系統附加的")
    assert guard_service.detect_record_imitation(f"好了\n{TOOL_RECORD_FOOTER}")
    assert not guard_service.detect_record_imitation("我用 exec 查過了")
    assert "[工具幻覺警告]" in guard_service.get_record_imitation_warning_message()


async def test_a_reply_that_copies_the_record_gets_the_hallucination_warning(monkeypatch):
    chunks = await _drive(
        monkeypatch, "record_imitation",
        [_text_resp(f"好了。\n{TOOL_RECORD_HEADER}\n1. exec → 成功\n{TOOL_RECORD_FOOTER}")],
    )
    warning = guard_service.get_record_imitation_warning_message()
    assert sum(json.dumps(warning, ensure_ascii=False)[1:-1] in c for c in chunks) == 1

    plain = await _drive(monkeypatch, "record_imitation_plain", [_text_resp("好了。")])
    assert not any("工具幻覺警告" in c for c in plain)
