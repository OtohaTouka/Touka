"""Committed branch projection, content filters and timestamp semantics."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

import pytest

from tests.support.conversation_search import (
    DAY,
    DAY_END,
    denied,
    search,
    success,
    tree,
    unknown_count,
)
from tests.support.conversation_search import conversation_db as conversation_db
from tests.support.session_scope import scope_db as scope_db

if TYPE_CHECKING:
    from tests.support.conversation_search import ConversationDB


@pytest.mark.parametrize("tip,expected", [("m1", 2), ("m0", 1), (None, 0)])
async def test_only_current_ancestor_chain_is_searched(
    conversation_db: ConversationDB, tip: str | None, expected: int
) -> None:
    messages = tree("root-visible", "assistant-visible", "descendant-hidden")
    messages["messages"]["sibling"] = {
        "id": "sibling",
        "parentId": "m0",
        "role": "assistant",
        "content": "sibling-hidden",
        "timestamp": DAY,
    }
    messages["messages"]["m0"]["childrenIds"] = ["m1", "sibling"]
    messages["currentId"] = tip
    await conversation_db.ui("branch", "branch-title", messages=messages)
    result = await search(source="ui")
    success(result, expected)
    assert "sibling-hidden" not in result and "descendant-hidden" not in result
    assert ("root-visible" in result) == (expected > 0)
    assert ("assistant-visible" in result) == (expected > 1)


@pytest.mark.parametrize(
    "damage", ["missing-parent", "cycle", "id-mismatch", "missing-tip", "list", "json"]
)
async def test_corrupt_current_history_is_an_error_not_empty(
    conversation_db: ConversationDB, damage: str
) -> None:
    valid = tree("allowed", "reply")
    await conversation_db.ui("chat", "chat", messages=valid)
    before = await search(source="ui")
    broken = copy.deepcopy(valid)
    if damage == "missing-parent":
        broken["messages"]["m1"]["parentId"] = "missing"
    elif damage == "cycle":
        broken["messages"]["m0"]["parentId"] = "m1"
    elif damage == "id-mismatch":
        broken["messages"]["m1"]["id"] = "different"
    elif damage == "missing-tip":
        broken["currentId"] = "missing"
    elif damage == "list":
        broken = list(valid["messages"].values())
    await conversation_db.execute("DELETE FROM user_sessions WHERE id = ?", ("chat",))
    await conversation_db.ui("chat", "chat", messages=broken)
    if damage == "json":
        await conversation_db.execute(
            "UPDATE user_sessions SET messages = ? WHERE id = ?", ("{", "chat")
        )
    denied(await search(source="ui"), "internal_error")
    success(before, 2)


async def test_only_text_is_projected_and_assistant_is_not_claimed_as_author(
    conversation_db: ConversationDB,
) -> None:
    messages = tree("visible-user", "visible-assistant", "system-secret", "tool-secret")
    messages["messages"]["m2"]["role"] = "system"
    messages["messages"]["m3"]["role"] = "tool"
    messages["messages"]["m1"].update(
        {
            "model": "ordinary-model",
            "name": "invented-author",
            "thinking": "thinking-secret",
            "tool_calls": [{"function": {"arguments": "tool-arguments-secret"}}],
            "images": ["image-secret"],
            "attachments": [{"name": "attachment-secret"}],
        }
    )
    await conversation_db.ui("chat", "title", messages=messages, owner="owner-secret")
    await conversation_db.execute(
        "UPDATE user_sessions SET context_summary = ? WHERE id = ?", ("summary-secret", "chat")
    )
    result = await search(source="ui")
    success(result, 2)
    assert "visible-user" in result and "visible-assistant" in result and "[助理]" in result
    assert "不代表逐則作者已驗證" in result
    assert "secret" not in result and "invented-author" not in result and "[你]" not in result


@pytest.mark.parametrize(
    "query,expected",
    [
        ("%", ["percent"]),
        ("_", ["underscore"]),
        ("\\", ["backslash"]),
        ("rain", ["ascii"]),
        ("æ", []),
        ("title-only", []),
        ("suffix-match", ["long"]),
    ],
)
async def test_keyword_uses_full_content_and_legacy_literal_like_rules(
    conversation_db: ConversationDB, query: str, expected: list[str]
) -> None:
    texts = {
        "percent": "percent 100%",
        "underscore": "underscore a_b",
        "backslash": "backslash a\\b",
        "ascii": "ascii RAIN",
        "unicode": "unicode Æ",
        "long": "long " + "x" * 310 + "suffix-match",
    }
    for sid, text in texts.items():
        await conversation_db.ui(sid, text, title="title-only")
    result = await search(source="ui", query=query)
    success(result, len(expected))
    for sid in texts:
        assert (f"「{sid} " in result) == (sid in expected)
    assert "suffix-match" not in result


@pytest.mark.parametrize(
    "role,expected", [("user", "user-match"), ("assistant", "assistant-match")]
)
async def test_role_filter_changes_total_and_page(
    conversation_db: ConversationDB, role: str, expected: str
) -> None:
    await conversation_db.ui("chat", "title", messages=tree("user-match", "assistant-match"))
    result = await search(source="ui", role=role, page_size=1)
    success(result, 1)
    assert expected in result and "下一頁" not in result
    assert ("assistant-match" if role == "user" else "user-match") not in result


async def test_taipei_day_is_inclusive_and_uses_message_time_not_updated_at(
    conversation_db: ConversationDB,
) -> None:
    for index, timestamp in enumerate((DAY - 1, DAY, DAY // 1000 + 1, DAY_END, DAY_END + 1)):
        await conversation_db.ui(str(index), f"point-{index}", timestamp=timestamp)
    await conversation_db.execute("UPDATE user_sessions SET updatedAt = ?", (DAY - 100_000_000,))
    result = await search(source="ui", date_start="2026-05-13", date_end="2026-05-13")
    success(result, 3)
    assert result.index("point-1") < result.index("point-2") < result.index("point-3")
    assert "point-0" not in result and "point-4" not in result
    assert "2026-05-13 00:00 Asia/Taipei" in result
    assert "對話紀錄" in result and "裝置時鐘" in result


@pytest.mark.parametrize("timestamp", [None, 0, -1])
async def test_unknown_time_is_last_and_date_exclusion_is_counted(
    conversation_db: ConversationDB, timestamp: int | None
) -> None:
    await conversation_db.ui("known", "known-match")
    await conversation_db.ui("unknown", "unknown-match", timestamp=timestamp)
    await conversation_db.ui("nonmatch", "not-selected", timestamp=None)
    undated = await search(source="ui", query="match")
    dated = await search(source="ui", query="match", date_start="2026-05-13")
    success(undated, 2)
    success(dated, 1)
    assert undated.index("known-match") < undated.index("unknown-match")
    assert "未知時間" in undated and "unknown-match" not in dated
    unknown_count(dated, 1)


@pytest.mark.parametrize("schema_damage", ["table", "marker"])
async def test_unsupported_store_fails_without_initialization(
    conversation_db: ConversationDB, schema_damage: str
) -> None:
    await conversation_db.ui("chat", "allowed")
    good = await search(source="ui")
    if schema_damage == "table":
        await conversation_db.execute("DROP TABLE user_sessions")
    else:
        await conversation_db.execute("ALTER TABLE user_sessions DROP COLUMN session_scope_version")
    conversation_db.statements.clear()
    denied(await search(source="ui"), "internal_error")
    assert not any(
        sql.lstrip().upper().startswith(("CREATE", "ALTER", "INSERT"))
        for sql in conversation_db.statements
    )
    success(good, 1)
