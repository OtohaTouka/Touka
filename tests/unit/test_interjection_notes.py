"""伺服器端重組的插話標註（ADR-0249 裁定 7、U4a-3）。

伺服器從 DB 組歷史給模型的三處——心跳脈衝、家對話注入（get_home_messages）、滑動視窗摘要——
對帶 interjections[] 的助理節點在 content 前加標註，格式與前端 INTERJECTION_NOTE_TEMPLATE
完全相同；角色仍是 assistant，回合數不變。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.souls import SoulConfig
from src.routers import heartbeat
from src.services import dialogue_summarizer
from src.services.prompt_blocks import CONTINUITY
from src.services.soul_service import SoulService
from src.utils import async_db
from src.utils.async_db import SessionSnapshot
from src.utils.history_manager import count_rounds_from_linear
from src.utils.interjection_notes import (
    INTERJECTION_NOTE_TEMPLATE,
    annotate_node,
    format_interjection_note,
    with_interjection_notes,
)

_FRONTEND = (
    Path(__file__).resolve().parents[2] / "ToukaChat" / "src" / "lib" / "chat" / "interjections.ts"
)


def _delivered(step: int, text: str, status: str = "delivered") -> dict[str, Any]:
    return {
        "id": f"i{step}",
        "text": text,
        "urgent": False,
        "status": status,
        "step": step,
        "created_at": 1.0,
    }


def _tree() -> dict[str, Any]:
    nodes = [
        {
            "id": "u1",
            "role": "user",
            "content": "先查資料",
            "parentId": None,
            "childrenIds": ["a1"],
        },
        {
            "id": "a1",
            "role": "assistant",
            "content": "查好了",
            "parentId": "u1",
            "childrenIds": ["u2"],
            "interjections": [_delivered(2, "只看 2026 年")],
        },
        {"id": "u2", "role": "user", "content": "整理一下", "parentId": "a1", "childrenIds": []},
    ]
    return {"messages": {n["id"]: n for n in nodes}, "currentId": "u2"}


def test_template_is_identical_to_the_frontend_constant():
    assert INTERJECTION_NOTE_TEMPLATE == "〔本回合中使用者補充（第 {step} 步）：{text}〕"
    source = _FRONTEND.read_text(encoding="utf-8")
    match = re.search(r"export const INTERJECTION_NOTE_TEMPLATE = '([^']*)';", source)
    assert match is not None
    assert match.group(1) == INTERJECTION_NOTE_TEMPLATE


def test_note_format_order_and_filters():
    assert (
        format_interjection_note(3, "改用 B {text}")
        == "〔本回合中使用者補充（第 3 步）：改用 B {text}〕"
    )
    items = [
        _delivered(4, "第二則"),
        _delivered(2, "還在排隊", status="queued"),
        _delivered(1, "第一則"),
        _delivered(3, ""),
        {**_delivered(5, "布林步數"), "step": True},
        "not-a-dict",
    ]
    assert with_interjection_notes("回答", items) == (
        "〔本回合中使用者補充（第 1 步）：第一則〕\n"
        "〔本回合中使用者補充（第 4 步）：第二則〕\n\n回答"
    )
    assert with_interjection_notes("回答", None) == "回答"
    assert with_interjection_notes("回答", []) == "回答"
    assert with_interjection_notes("回答", "bad") == "回答"


def test_annotate_node_only_touches_assistant_nodes_with_interjections():
    tree = _tree()
    assistant = tree["messages"]["a1"]
    annotated = annotate_node(assistant)
    assert annotated["role"] == "assistant"
    assert annotated["content"] == "〔本回合中使用者補充（第 2 步）：只看 2026 年〕\n\n查好了"
    assert assistant["content"] == "查好了"  # 不改原節點（DB 讀出的樹）
    user = tree["messages"]["u1"]
    assert annotate_node(user) is user
    plain = {"id": "a", "role": "assistant", "content": "x"}
    assert annotate_node(plain) is plain
    blocks = {
        "id": "b",
        "role": "assistant",
        "content": [{"type": "text", "text": "x"}],
        "interjections": [_delivered(1, "y")],
    }
    assert annotate_node(blocks) is blocks
    empty = {"id": "c", "role": "assistant", "content": None, "interjections": [_delivered(1, "y")]}
    assert annotate_node(empty)["content"] == "〔本回合中使用者補充（第 1 步）：y〕\n\n"


def test_round_count_is_unchanged_by_interjections():
    tree = _tree()
    linear = [tree["messages"][k] for k in ("u1", "a1", "u2")]
    plain = [{k: v for k, v in m.items() if k != "interjections"} for m in linear]
    assert count_rounds_from_linear(linear) == 1
    assert count_rounds_from_linear(plain) == 1
    assert count_rounds_from_linear([annotate_node(m) for m in linear]) == 1


async def _no_chunks():
    for _ in ():
        yield ""


async def test_heartbeat_pulse_sends_the_note_to_the_model(monkeypatch):
    captured: dict[str, Any] = {}

    async def _dispatch(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(body_iterator=_no_chunks())

    soul = object.__new__(SoulService)
    soul.get_soul_data = lambda _identity: ("靈魂", SoulConfig().model_dump())
    session = {"id": "s", "title": "t", "messages": _tree()}
    monkeypatch.setattr(
        heartbeat, "session_service",
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
    # 心跳也附 <continuity>（只在送出的最後一則 user 尾端）；插話標註不受影響
    role, content = messages[-1]
    tail = f"\n\n<{CONTINUITY}>"
    assert tail in content
    messages[-1] = (role, content.split(tail)[0])
    assert messages == [
        ("user", "先查資料"),
        ("assistant", "〔本回合中使用者補充（第 2 步）：只看 2026 年〕\n\n查好了"),
        ("user", "整理一下"),
    ]


async def test_home_messages_carry_the_note(monkeypatch):
    service = MagicMock(
        read_home_snapshot=AsyncMock(
            return_value=async_db.SessionSnapshot({"id": "home", "messages": _tree()}, "rev")
        ),
    )
    monkeypatch.setattr(async_db, "session_service", service)
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")

    messages = await async_db.get_home_messages(identity)

    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "先查資料"),
        ("assistant", "〔本回合中使用者補充（第 2 步）：只看 2026 年〕\n\n查好了"),
        ("user", "整理一下"),
    ]


def test_summarizer_input_carries_the_note():
    tree = _tree()
    linear = [tree["messages"][k] for k in ("u1", "a1", "u2")]
    assert dialogue_summarizer._format_dialogue(linear) == (
        "[unknown time] user: 先查資料\n"
        "[unknown time] assistant: 〔本回合中使用者補充（第 2 步）：只看 2026 年〕\n\n查好了\n"
        "[unknown time] user: 整理一下"
    )
