"""上一輪沒做完的記號（ADR-0318 PR 3）。

網頁對話被使用者停止（``generationStopped``）或出錯（``generationFailed``）的那則 AI 回覆，
送給模型時 content 前加一行固定記號；前端組下一輪、伺服器從存檔組歷史（心跳、家原文、摘要）
用同一份文字（跨端以純文字比對）。角色仍是 assistant、回合數不變；空回覆加了記號就不再被
``drop_blank_assistant_turns`` 拿掉、前後兩則 user 不再合併。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from src.adapters.content_normalize import drop_blank_assistant_turns
from src.utils import async_db
from src.utils.history_manager import count_rounds_from_linear
from src.utils.interjection_notes import (
    DELIVERY_PLATFORM_NAMES,
    OTHER_PLATFORM_NAME,
    TURN_FAILED_NOTE,
    TURN_STOPPED_NOTE,
    TURN_UNDELIVERED_NOTE_TEMPLATE,
    annotate_node,
)

_FRONTEND = (
    Path(__file__).resolve().parents[2] / "ToukaChat" / "src" / "lib" / "chat" / "turnNotes.ts"
)
_NOTES = {
    "TURN_STOPPED_NOTE": TURN_STOPPED_NOTE,
    "TURN_FAILED_NOTE": TURN_FAILED_NOTE,
    "TURN_UNDELIVERED_NOTE_TEMPLATE": TURN_UNDELIVERED_NOTE_TEMPLATE,
    "OTHER_PLATFORM_NAME": OTHER_PLATFORM_NAME,
}


def _undelivered(platform: str) -> str:
    return TURN_UNDELIVERED_NOTE_TEMPLATE.replace("{platform}", platform)


def _assistant(content: Any, **flags: Any) -> dict[str, Any]:
    return {"id": "a1", "role": "assistant", "content": content, **flags}


def _tree(assistant: dict[str, Any]) -> dict[str, Any]:
    nodes = [
        {"id": "u1", "role": "user", "content": "幫我整理", "parentId": None,
         "childrenIds": ["a1"]},
        {**assistant, "parentId": "u1", "childrenIds": ["u2"]},
        {"id": "u2", "role": "user", "content": "繼續", "parentId": "a1", "childrenIds": []},
    ]
    return {"messages": {n["id"]: n for n in nodes}, "currentId": "u2"}


@pytest.mark.parametrize("name", sorted(_NOTES))
def test_notes_are_identical_to_the_frontend_constants(name: str) -> None:
    source = _FRONTEND.read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = '([^']*)';", source)
    assert match is not None
    assert match.group(1) == _NOTES[name]


def test_platform_names_are_identical_to_the_frontend() -> None:
    source = _FRONTEND.read_text(encoding="utf-8")
    block = re.search(r"export const DELIVERY_PLATFORM_NAMES[^=]*= \{([^}]*)\};", source)
    assert block is not None
    assert dict(re.findall(r"(\w+): '([^']*)'", block.group(1))) == DELIVERY_PLATFORM_NAMES


@pytest.mark.parametrize(
    ("platform", "name"),
    [
        ("discord", "Discord"),
        ("Telegram", "Telegram"),
        ("line", OTHER_PLATFORM_NAME),
        (None, OTHER_PLATFORM_NAME),
        # 物件內建的名字不是平台（前端查表要用 Object.hasOwn）
        ("constructor", OTHER_PLATFORM_NAME),
        ("__proto__", OTHER_PLATFORM_NAME),
    ],
)
def test_an_undelivered_reply_names_the_platform(platform: str | None, name: str) -> None:
    """SNS 送不出去的那則（ADR-0322 PR 2）：平台只用固定名稱，其他一律寫外部平台."""
    node = _assistant("回覆", deliveryFailed=True, deliveryPlatform=platform)
    assert annotate_node(node)["content"] == f"{_undelivered(name)}\n\n回覆"


def test_the_undelivered_note_comes_after_the_failed_note() -> None:
    node = _assistant(
        "寫到一半", generationFailed=True, deliveryFailed=True, deliveryPlatform="discord"
    )
    assert annotate_node(node)["content"] == (
        f"{TURN_FAILED_NOTE}\n{_undelivered('Discord')}\n\n寫到一半"
    )
    assert annotate_node(_assistant("", deliveryFailed=True))["content"] == _undelivered(
        OTHER_PLATFORM_NAME
    )


def test_only_a_true_delivery_flag_counts() -> None:
    node = _assistant("x", deliveryFailed="yes", deliveryPlatform="discord")
    assert annotate_node(node) is node


def test_stopped_and_failed_turns_carry_the_note() -> None:
    node = _assistant("寫到一半", generationStopped=True)
    assert annotate_node(node)["content"] == f"{TURN_STOPPED_NOTE}\n\n寫到一半"
    assert node["content"] == "寫到一半"  # 不改原節點（DB 讀出的樹）
    assert annotate_node(_assistant("", generationFailed=True))["content"] == TURN_FAILED_NOTE
    assert annotate_node(_assistant(None, generationStopped=True))["content"] == TURN_STOPPED_NOTE
    both = _assistant("x", generationStopped=True, generationFailed=True)
    assert annotate_node(both)["content"] == f"{TURN_STOPPED_NOTE}\n\nx"  # 使用者停的優先


def test_only_true_flags_on_text_assistant_nodes_count() -> None:
    for node in (
        _assistant("x", generationStopped="yes"),
        _assistant("x", generationFailed=1),
        {"id": "u", "role": "user", "content": "x", "generationStopped": True},
        _assistant([{"type": "text", "text": "x"}], generationStopped=True),
    ):
        assert annotate_node(node) is node


def test_the_note_comes_before_interjection_notes() -> None:
    node = _assistant(
        "答案",
        generationStopped=True,
        interjections=[{"id": "i", "text": "只看今年", "status": "delivered", "step": 1}],
    )
    assert annotate_node(node)["content"] == (
        f"{TURN_STOPPED_NOTE}\n\n〔本回合中使用者補充（第 1 步）：只看今年〕\n\n答案"
    )


def test_a_marked_blank_turn_is_kept_and_rounds_are_unchanged() -> None:
    """以前空回覆會被拿掉、前後兩則 user 合在一起；加了記號就保留（ADR-0231 決定 6 修訂）。"""
    linear = list(_tree(_assistant("", generationStopped=True))["messages"].values())
    sent = [annotate_node(m) for m in linear]
    kept = drop_blank_assistant_turns([{"role": m["role"], "content": m["content"]} for m in sent])
    assert [m["role"] for m in kept] == ["user", "assistant", "user"]
    assert kept[1]["content"] == TURN_STOPPED_NOTE
    assert count_rounds_from_linear(sent) == count_rounds_from_linear(linear) == 1


def test_home_messages_carry_the_note() -> None:
    messages = async_db.home_messages_of(
        {"id": "home", "messages": _tree(_assistant("查到一半", generationFailed=True))}
    )
    assert [m["content"] for m in messages] == [
        "幫我整理", f"{TURN_FAILED_NOTE}\n\n查到一半", "繼續"
    ]
