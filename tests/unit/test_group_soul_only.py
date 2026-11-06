"""ADR-0245（2a 段 U2a-4）：群聊成員只限靈魂.

- 白名單只認 ToukaSoul 區；舊 Native／Custom 項目讀到就略過並記警告，不改檔。
- 別名必須解析到 agents_registry 裡的靈魂；原始模型 ID 與 regex 不再展開。
- 群聊派發只走靈魂路徑；SNS 的 @別名經 ToukaGroup，同樣只到靈魂。
全部用 tmp 合成設定，不讀 live 設定檔。
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.sns import SNSIncomingEvent
from src.routers.group_settings import GroupWhitelist, get_whitelist
from src.services.dispatchers.group import dispatch_group_stream
from src.services.group_router import parse_group_roster
from src.services.hooks.events import wrap_hook_output
from src.services.sns_gateway import SNSGatewayService

# Tester 是 registry 成員但不是靈魂（如出貨範本的 HeartbeatTester）。
REGISTRY = {"Alice": ["Ember"], "Bob": [], "Tester": ["tester"]}
SOULS = {"Alice", "Bob"}
SOUL_SECTION = {
    "小愛": "alice",
    "阿鮑": "Bob",
    "Alice": "Alice",
    "壞": "gpt-4o",
    "通配": "Ali.*",
    "測試員": "Tester",
    "Ember": "gpt-4o",
}
LEGACY = {
    "ToukaSoul": SOUL_SECTION,
    "Native": {"gem": "gemini-2.5-flash"},
    "Custom": {"自訂": "my-custom"},
}
ADMIN = AgentIdentity.for_role(agent_id="DebugAgent", role="admin", capabilities=[])


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """tmp 合成的 group_whitelist（含舊 Native／Custom）與 agents_registry."""
    (tmp_path / "group_whitelist.json").write_text(
        json.dumps(LEGACY, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "agents_registry.json").write_text(json.dumps(REGISTRY), encoding="utf-8")
    for mod in (
        "src.services.group_router",
        "src.services.dispatchers.group",
        "src.routers.group_settings",
    ):
        monkeypatch.setattr(f"{mod}.get_config_dir", lambda: tmp_path)
    souls = SimpleNamespace(is_toukasoul=SOULS.__contains__)
    monkeypatch.setattr("src.services.group_router.soul_service", souls)
    return tmp_path


@pytest.fixture
def streams(monkeypatch):
    """攔截每位成員的派發；靈魂注入與 brain 讀寫改成假的."""
    calls: list[dict[str, Any]] = []

    async def _fake_gsg(model, req, identity, request_id, start_time, ctx):
        calls.append({"model": model, "identity": identity, "is_toukasoul": ctx["is_toukasoul"]})

        async def _gen():
            yield 'data: {"choices": [{"delta": {"content": "嗨"}, "index": 0}]}\n\n'
            yield "data: [DONE]\n\n"

        return _gen()

    soul = MagicMock()
    soul.inject_home_messages = AsyncMock()
    monkeypatch.setattr("src.services.dispatchers.group.get_stream_generator", _fake_gsg)
    monkeypatch.setattr("src.services.dispatchers.group.get_active_brain", lambda i: "Brain-X")
    monkeypatch.setattr("src.services.dispatchers.group.record_working_brain", lambda *a: None)
    monkeypatch.setattr("src.services.dispatchers.group.soul_service", soul)
    return calls, soul


def _roster(text: str) -> Any:
    return parse_group_roster([OpenAIMessage(role="user", content=text)], REGISTRY)


async def _group_text(text: str) -> str:
    request = OpenAIChatRequest(
        model="ToukaGroup", messages=[OpenAIMessage(role="user", content=text)]
    )
    out = []
    async for chunk in dispatch_group_stream(request, "req-g", 0.0, PipelineContext()):
        payload = chunk.removeprefix("data: ").strip()
        if payload.startswith("{"):
            delta = json.loads(payload).get("choices", [{}])[0].get("delta", {})
            out.append(delta.get("content", ""))
    return "".join(out)


def test_mentions_inside_hook_output_are_not_roll_calls(cfg):
    """Hook 的補充說明接在 user 訊息尾端，是資料：裡面的 @ 不加人，也不讓整輪出錯（ADR-0292）."""
    note = wrap_hook_output("UserPromptSubmit", "記得找 @阿鮑，還有 @不存在的人")
    assert _roster(f"@小愛 早\n\n{note}") == [{"name": "小愛", "id": "Alice"}]
    blocks = [{"type": "text", "text": "@小愛 早"}, {"type": "text", "text": f"\n\n{note}"}]
    roster = parse_group_roster([OpenAIMessage(role="user", content=blocks)], REGISTRY)
    assert roster == [{"name": "小愛", "id": "Alice"}]


def test_alias_resolves_to_registry_soul(cfg):
    assert _roster("@小愛 @阿鮑 早") == [
        {"name": "小愛", "id": "Alice"},
        {"name": "阿鮑", "id": "Bob"},
    ]


def test_soul_section_entry_pointing_to_raw_model_is_rejected(cfg):
    assert _roster("@壞 你好") == {"error": "『@壞』指向的『gpt-4o』不是靈魂，群聊成員只限靈魂。"}


def test_registry_member_that_is_not_a_soul_is_rejected(cfg):
    assert _roster("@測試員 你好") == {
        "error": "『@測試員』指向的『Tester』不是靈魂，群聊成員只限靈魂。"
    }


def test_non_soul_value_does_not_fall_back_to_alias_itself(cfg):
    """白名單值不是靈魂時，不拿別名本身（Ember＝Alice 的註冊別名）去比對 registry."""
    assert _roster("@Ember 你好") == {
        "error": "『@Ember』指向的『gpt-4o』不是靈魂，群聊成員只限靈魂。"
    }


def _history(*user_texts: str) -> list[OpenAIMessage]:
    messages: list[OpenAIMessage] = []
    for text in user_texts:
        messages += [OpenAIMessage(role="user", content=text)]
        messages += [OpenAIMessage(role="assistant", content="收到")]
    return messages[:-1]


def test_unresolvable_mentions_in_older_history_are_skipped(cfg):
    roster = parse_group_roster(_history("@gpt 你好", "@壞 在嗎", "@Alice 你好"), REGISTRY)
    assert roster == [{"name": "Alice", "id": "Alice"}]


def test_latest_message_is_still_validated_strictly(cfg):
    assert parse_group_roster(_history("@小愛 你好", "@gpt 你好"), REGISTRY) == {
        "error": "找不到靈魂『@gpt』，請確認是否打錯或尚未加入群聊白名單；群聊成員只限靈魂。"
    }
    assert parse_group_roster(_history("@小愛 你好", "@測試員 你好"), REGISTRY) == {
        "error": "『@測試員』指向的『Tester』不是靈魂，群聊成員只限靈魂。"
    }


def test_regex_value_is_not_expanded(cfg):
    assert _roster("@通配 你好") == {
        "error": "『@通配』指向的『Ali.*』不是靈魂，群聊成員只限靈魂。"
    }


def test_legacy_native_custom_entries_ignored_with_warning_file_untouched(cfg, caplog):
    before = (cfg / "group_whitelist.json").read_bytes()
    with caplog.at_level(logging.WARNING, logger="src.services.group_router"):
        native = _roster("@gem 你好")
        custom = _roster("@自訂 你好")
    assert native == {
        "error": "找不到靈魂『@gem』，請確認是否打錯或尚未加入群聊白名單；群聊成員只限靈魂。"
    }
    assert custom == {
        "error": "找不到靈魂『@自訂』，請確認是否打錯或尚未加入群聊白名單；群聊成員只限靈魂。"
    }
    assert any("Custom, Native" in r.getMessage() for r in caplog.records)
    assert (cfg / "group_whitelist.json").read_bytes() == before


async def test_settings_get_returns_only_soul_section(cfg):
    before = (cfg / "group_whitelist.json").read_bytes()
    result = await get_whitelist(identity=ADMIN)
    assert result.model_dump() == {"ToukaSoul": SOUL_SECTION}
    assert (cfg / "group_whitelist.json").read_bytes() == before


@pytest.mark.parametrize("section", ["Native", "Custom"])
def test_settings_model_rejects_legacy_sections(section):
    with pytest.raises(ValidationError):
        GroupWhitelist.model_validate({"ToukaSoul": {}, section: {"x": "gemini-2.5-flash"}})


async def test_group_dispatch_never_streams_to_non_soul(cfg, streams):
    calls, _ = streams
    text = await _group_text("@gem 哈囉")
    assert "找不到靈魂『@gem』" in text
    assert calls == []


async def test_group_dispatch_soul_member_uses_soul_identity(cfg, streams):
    calls, soul = streams
    text = await _group_text("@小愛 哈囉")
    assert "【小愛】" in text
    assert calls == [
        {"model": "brain-x", "identity": AgentIdentity(agent_id="Alice"), "is_toukasoul": True}
    ]
    assert soul.apply_soul_to_request.call_args[0][1] == AgentIdentity(agent_id="Alice")


async def _sns_reply(content: str) -> str:
    async def _proxy(request, ctx, is_anthropic=False):
        async def _empty():
            return
            yield  # pragma: no cover

        resp = MagicMock()
        resp.body_iterator = (
            dispatch_group_stream(request, "req-sns", 0.0, PipelineContext())
            if request.model == "ToukaGroup"
            else _empty()
        )
        return resp

    adapter = MagicMock(agent_id="Alice", show_typing=AsyncMock(), send_message=AsyncMock())
    event = SNSIncomingEvent(platform="telegram", sender_id="u1", channel_id="c1", content=content)
    session = MagicMock(
        get_session_by_id=AsyncMock(return_value={"id": "x"}),
        append_message_to_session=AsyncMock(),
    )
    with (
        patch(
            "src.services.sns_gateway.agent_service.identify_agent",
            side_effect=lambda value: {"agent_id": value, "is_toukasoul": value in SOULS},
        ),
        patch(
            "src.services.sns_gateway.identity_service.get_identity",
            side_effect=lambda value: AgentIdentity.for_role(value, "Owner"),
        ),
        patch("src.services.sns_gateway.session_service", session),
        patch("src.services.sns_gateway.process_proxy_request", side_effect=_proxy),
    ):
        return await SNSGatewayService().on_event(adapter, event)


async def test_sns_alias_reaches_only_souls(cfg, streams):
    calls, _ = streams
    assert "群聊成員只限靈魂" in await _sns_reply("@gem 你好")
    assert calls == []
    assert "嗨" in await _sns_reply("@小愛 你好")
    assert [c["identity"].agent_id for c in calls] == ["Alice"]
