"""網頁版在家對話本身聊天時，家對話不再重複送給模型（2026-10-29）。

網頁版一般（非無痕）聊天送的是這個對話自己的完整紀錄（前端存檔時持有整棵樹、後端核對過
revision）；目前對話就是家對話時，SoulLayer 不再插一份家對話，否則模型會收到兩份一樣的
紀錄，剛打的那句還會被包進「舊資料」界線裡多出現一次。其他情況照舊插入：子對話要看到家對話
的即時內容；SNS 家對話模式與 IDE 只送最新一句、靠後端補前文；只有 ctx 旗標、沒有存檔核准
欄位的請求（例如繼承父 ctx 的路徑）也照舊。無痕照舊不插。

全部走真 SoulLayer 與沙盒 session DB（家對話、子對話、前情摘要都是真的資料列）。
"""

from __future__ import annotations

import json
import uuid
from collections import OrderedDict
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from src.adapters.schemas import (
    AnthropicMessage,
    AnthropicRequest,
    OpenAIChatRequest,
    OpenAIMessage,
)
from src.middleware.layers import soul as soul_mod
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import continuity_service
from src.utils.async_db import init_db_async, session_service

if TYPE_CHECKING:
    from pathlib import Path

SOUL = "HomeDedupeSoul"
SOUL_TEXT = "你是 Aoi，一位安靜的圖書館員。"
MODEL = "claude-sonnet-4-5"
NOW = 1_760_500_000.0
HOME_BOUNDARY = 'source="home_conversation"'
HOME_EARLIER = "MARK-HOME-EARLIER 今天人多嗎？"
HOME_REPLY = "MARK-HOME-REPLY 還好。"
HOME_LATEST = "MARK-HOME-LATEST 書展還開嗎？"
SUB_EARLIER = "MARK-SUB-EARLIER 幫我整理書單。"
SUB_REPLY = "MARK-SUB-REPLY 好的。"
SUB_LATEST = "MARK-SUB-LATEST 再加一本。"
SUMMARY = "MARK-SUMMARY 上次聊到書展。"


def _soul() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=SOUL, role="Owner", is_human=False)


def _tree(*texts: str) -> dict[str, Any]:
    """user／assistant 交替的線性樹；最後一則是前端先存好的這一句 user。"""
    messages: dict[str, dict[str, Any]] = {}
    parent = None
    for index, text in enumerate(texts):
        node_id = f"n-{index}"
        messages[node_id] = {"id": node_id, "role": "user" if index % 2 == 0 else "assistant",
                             "content": text, "parentId": parent, "childrenIds": [],
                             "timestamp": int((NOW - 3600) * 1000)}
        if parent:
            messages[parent]["childrenIds"].append(node_id)
        parent = node_id
    return {"messages": messages, "currentId": parent}


@pytest.fixture(autouse=True)
def _soul_layer_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """只換掉讀磁碟的人格來源與主要模型；家對話、摘要、連續性、組裝都走真程式。"""
    monkeypatch.setattr(soul_mod.soul_service, "get_soul_data",
                        lambda _a: (SOUL_TEXT, {"is_toukasoul": True}))
    monkeypatch.setattr(soul_mod.agent_service, "identify_agent",
                        lambda aid: {"agent_id": aid, "role": "Owner", "is_toukasoul": True})
    monkeypatch.setattr(soul_mod, "get_active_brain", lambda _identity: MODEL)
    monkeypatch.setattr(continuity_service, "_last_request_at", OrderedDict())
    monkeypatch.setattr(continuity_service, "get_current_epoch", lambda: NOW)


@pytest.fixture
async def sessions(tmp_path: Path) -> Any:
    """沙盒 DB：這個靈魂的家對話（有前情摘要）與一個子對話，都綁在這個靈魂。"""
    with patch("src.utils.async_db.get_db_path", return_value=tmp_path / "sessions.db"):
        await init_db_async()
        home, sub = f"home-{uuid.uuid4().hex[:8]}", f"sub-{uuid.uuid4().hex[:8]}"
        assert await session_service.upsert_session(
            home, "home", _tree(HOME_EARLIER, HOME_REPLY, HOME_LATEST),
            identity=_soul(), is_home=True, home_for_agent=SOUL, context_summary=SUMMARY,
        )
        assert await session_service.upsert_session(
            sub, "sub", _tree(SUB_EARLIER, SUB_REPLY, SUB_LATEST), identity=_soul(),
        )
        yield {"home": home, "sub": sub}


def _persistent() -> dict[str, str]:
    return {"mode": "persistent", "revision": "rev-1",
            "user_parent_id": "n-1", "assistant_node_id": "a-new"}


def _openai(session_id: str, *texts: str, ui: dict | None = None,
            incognito: bool = False) -> OpenAIChatRequest:
    return OpenAIChatRequest(
        model=SOUL, stream=True, session_id=session_id, incognito=incognito, ui_persistence=ui,
        messages=[OpenAIMessage(role="user" if i % 2 == 0 else "assistant", content=t)
                  for i, t in enumerate(texts)],
    )


def _ctx(session_id: str, *, ui_authorized: bool, incognito: bool = False) -> PipelineContext:
    extra = {"ui_authorized": True} if ui_authorized else {}
    return PipelineContext(is_toukasoul=True, agent_id=SOUL, session_id=session_id,
                           identity=_soul(), incognito=incognito, **extra)


def _text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def _messages(request: Any) -> list[tuple[str, str]]:
    """(角色, 文字)，照送出順序；dict 與 Pydantic 訊息都收。"""
    out = []
    for m in request.messages:
        role = m.get("role") if isinstance(m, dict) else m.role
        out.append((role, _text(m.get("content") if isinstance(m, dict) else m.content)))
    return out


def _joined(request: Any) -> str:
    return "\n".join(text for _role, text in _messages(request))


async def _run(request: Any, ctx: PipelineContext) -> Any:
    await soul_mod.SoulLayer().process(request, ctx)
    return request


# --- 網頁版在家對話本身：不再插 ------------------------------------------------


async def test_web_home_conversation_is_not_sent_twice(sessions: dict) -> None:
    """紅：現在家對話會被插在最前面再送一次，最新那句也包進舊資料界線裡多出現一次。"""
    request = await _run(
        _openai(sessions["home"], HOME_EARLIER, HOME_REPLY, HOME_LATEST, ui=_persistent()),
        _ctx(sessions["home"], ui_authorized=True),
    )
    joined = _joined(request)
    assert HOME_BOUNDARY not in joined
    for text in (HOME_EARLIER, HOME_REPLY, HOME_LATEST):
        assert joined.count(text) == 1, text
    # 人格仍在最前面、前情摘要照舊注入
    role, system = _messages(request)[0]
    assert role == "system"
    assert SOUL_TEXT in system
    assert "<conversation_summary>" in system
    assert SUMMARY in system


async def test_web_home_conversation_anthropic_is_not_sent_twice(sessions: dict) -> None:
    """紅：Anthropic 格式同上；人格與前情摘要在 system。"""
    request = AnthropicRequest(
        model=SOUL, stream=True, session_id=sessions["home"], ui_persistence=_persistent(),
        messages=[AnthropicMessage(role="user", content=HOME_EARLIER),
                  AnthropicMessage(role="assistant", content=HOME_REPLY),
                  AnthropicMessage(role="user", content=HOME_LATEST)],
    )
    await _run(request, _ctx(sessions["home"], ui_authorized=True))
    joined = _joined(request)
    assert HOME_BOUNDARY not in joined
    for text in (HOME_EARLIER, HOME_REPLY, HOME_LATEST):
        assert joined.count(text) == 1, text
    system = _text(request.system)
    assert SOUL_TEXT in system
    assert SUMMARY in system


# --- 其他情況照舊插入 ----------------------------------------------------------


async def test_web_sub_conversation_still_sees_home(sessions: dict) -> None:
    """子對話：照舊插入家對話的即時內容（設計用意），排在人格之後、子對話之前。"""
    request = await _run(
        _openai(sessions["sub"], SUB_EARLIER, SUB_REPLY, SUB_LATEST, ui=_persistent()),
        _ctx(sessions["sub"], ui_authorized=True),
    )
    joined = _joined(request)
    assert joined.count(HOME_BOUNDARY) == 1
    assert joined.index(HOME_EARLIER) < joined.index(SUB_EARLIER)
    for text in (HOME_EARLIER, HOME_LATEST, SUB_EARLIER, SUB_LATEST):
        assert joined.count(text) == 1, text
    assert _messages(request)[0][0] == "system"


async def test_latest_only_client_in_home_still_gets_context(sessions: dict) -> None:
    """SNS 家對話模式與 IDE：只送最新一句、沒有存檔核准欄位，靠後端補前文。

    最新那句在 SNS 會多出現一次是已知的另一個問題（HANDOVER 待辦），這裡不驗次數。
    """
    request = await _run(_openai(sessions["home"], HOME_LATEST), _ctx(sessions["home"],
                                                                      ui_authorized=False))
    joined = _joined(request)
    assert joined.count(HOME_BOUNDARY) == 1
    assert joined.count(HOME_EARLIER) == 1
    assert joined.count(HOME_REPLY) == 1


async def test_admission_without_ui_authorized_still_injects(sessions: dict) -> None:
    """有存檔核准欄位、但路由沒核准（ctx 沒有 ui_authorized）：不能當成完整紀錄。"""
    request = await _run(
        _openai(sessions["home"], HOME_EARLIER, HOME_REPLY, HOME_LATEST, ui=_persistent()),
        _ctx(sessions["home"], ui_authorized=False),
    )
    assert _joined(request).count(HOME_BOUNDARY) == 1


async def test_ui_authorized_flag_alone_still_injects(sessions: dict) -> None:
    """只有 ctx 旗標、請求沒帶存檔核准欄位（例如繼承父 ctx 的路徑）：照舊插入。"""
    request = await _run(_openai(sessions["home"], HOME_LATEST),
                         _ctx(sessions["home"], ui_authorized=True))
    joined = _joined(request)
    assert joined.count(HOME_BOUNDARY) == 1
    assert joined.count(HOME_EARLIER) == 1


@pytest.mark.parametrize(("mode", "expected"), [("persistent", True), ("incognito", False)])
def test_only_persistent_admission_counts_as_full_history(mode: str, expected: bool) -> None:
    """無痕在更早的閘就跳過注入，經 SoulLayer 測不到；直接釘住判斷只認 persistent。"""
    request = SimpleNamespace(ui_persistence=SimpleNamespace(mode=mode))
    ctx = PipelineContext(ui_authorized=True)
    assert soul_mod.SoulLayer._sends_own_history(request, ctx) is expected


async def test_incognito_still_skips_home(sessions: dict) -> None:
    """無痕：照舊不插家對話。"""
    session_id = f"incognito-{uuid.uuid4().hex[:8]}"
    ui = {"mode": "incognito", "user_parent_id": "n-1", "assistant_node_id": "a-new"}
    request = await _run(_openai(session_id, HOME_LATEST, ui=ui, incognito=True),
                         _ctx(session_id, ui_authorized=True, incognito=True))
    joined = _joined(request)
    assert HOME_BOUNDARY not in joined
    assert HOME_EARLIER not in joined
