"""Claude 對話歷史快取斷點（斷點 2）要落在「請求時才附在尾端」的區塊之前。

<continuity>、<ide_context> 只附在這一次請求的最後一則 user 訊息尾端，下一輪送回的歷史
不含它們。斷點若打在含它們的區塊上，快取會寫在「含這次尾端區塊」的前綴上，下一輪對不上，
每輪都得重寫整段歷史。這裡釘住：斷點在使用者原文，尾端區塊是最後一塊、沒有斷點；第 N 輪
斷點以前的區塊，和第 N+1 輪對應位置逐一相同。

全部離線：無痕的 continuity（只放現在時間）不讀 session 與任務看板；時鐘凍結。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from src.adapters.openai_to_claude import convert_openai_to_anthropic
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import continuity_service as cs
from src.services.prompt_blocks import CONTINUITY, IDE_CONTEXT, wrap_block

if TYPE_CHECKING:
    from src.adapters.schemas import AnthropicRequest

OPEN = f"<{CONTINUITY}>"
NOW = 1_760_500_000.0
SOUL = AgentIdentity.for_role(agent_id="U6CacheSoul", role="Owner", is_human=False)
IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    now = {"t": NOW}
    monkeypatch.setattr(cs, "get_current_epoch", lambda: now["t"])
    return now


async def _request(*history: tuple[str, Any], last: Any) -> OpenAIChatRequest:
    """system＋歷史＋最後一則 user（只有最後一則被附上 continuity，和 SoulLayer 一樣）。"""
    messages = [OpenAIMessage(role="system", content="<persona>\nP\n</persona>")]
    messages += [OpenAIMessage(role=role, content=content) for role, content in history]
    messages.append(OpenAIMessage(role="user", content=last))
    request = OpenAIChatRequest(model="claude-sonnet-4-5", messages=messages)
    await cs.inject_continuity(request, PipelineContext(incognito=True), SOUL)
    return request


def _blocks(converted: AnthropicRequest) -> list[tuple[str, dict[str, Any], bool]]:
    """攤平成 (role, 去掉 cache_control 的區塊, 是否有斷點)。"""
    flat: list[tuple[str, dict[str, Any], bool]] = []
    for message in converted.messages:
        for block in message.content:
            bare = {k: v for k, v in block.items() if k != "cache_control"}
            flat.append((message.role, bare, "cache_control" in block))
    return flat


def _last_user_blocks(converted: AnthropicRequest) -> list[dict[str, Any]]:
    return converted.messages[-1].content


async def test_breakpoint_on_user_text_not_on_continuity(clock: dict[str, float]) -> None:
    """字串串接：拆成「使用者原文」與「continuity」兩塊，斷點在原文，continuity 是最後一塊。"""
    blocks = _last_user_blocks(convert_openai_to_anthropic(await _request(last="hi")))
    assert [b["text"][:2] for b in blocks] == ["hi", "\n\n"]
    assert blocks[0] == {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    assert blocks[-1]["text"].lstrip().startswith(OPEN)
    assert "cache_control" not in blocks[-1]


async def test_breakpoint_skips_continuity_part_after_image(clock: dict[str, float]) -> None:
    """多模態（continuity 是獨立的最後一塊）：斷點打在前一塊（圖片）。"""
    last = json.dumps([{"type": "text", "text": "look"}, IMAGE])
    blocks = _last_user_blocks(convert_openai_to_anthropic(await _request(last=last)))
    assert [b["type"] for b in blocks] == ["text", "image", "text"]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert OPEN in blocks[2]["text"] and "cache_control" not in blocks[2]
    assert "cache_control" not in blocks[0]


async def test_ide_context_and_continuity_both_stay_after_breakpoint(
    clock: dict[str, float],
) -> None:
    """<ide_context> 也是請求時附在尾端：兩段一起切到斷點之後。"""
    last = "hi\n\n" + wrap_block(IDE_CONTEXT, "📄 a.py | 📍 Line 1")
    blocks = _last_user_blocks(convert_openai_to_anthropic(await _request(last=last)))
    assert blocks[0] == {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    assert len(blocks) == 2 and "cache_control" not in blocks[1]
    assert blocks[1]["text"].startswith(f"\n\n<{IDE_CONTEXT}>") and OPEN in blocks[1]["text"]


async def test_no_breakpoint_when_nothing_precedes_continuity(clock: dict[str, float]) -> None:
    """前面沒有別的區塊：不打斷點（不把快取寫在只含這次 continuity 的前綴上）。"""
    converted = convert_openai_to_anthropic(await _request(last=""))
    assert OPEN in _last_user_blocks(converted)[-1]["text"]
    assert not any(marked for _, _, marked in _blocks(converted))


async def test_history_prefix_matches_next_turn(clock: dict[str, float]) -> None:
    """第 N 輪斷點以前（含斷點那塊）的區塊，和第 N+1 輪對應位置逐一相同。"""
    turn_n = _blocks(convert_openai_to_anthropic(await _request(last="hi")))
    clock["t"] = NOW + 600  # 下一輪的 continuity 內容不同
    turn_next = _blocks(convert_openai_to_anthropic(
        await _request(("user", "hi"), ("assistant", "reply"), last="next")
    ))
    marked = [i for i, (_, _, has) in enumerate(turn_n) if has]
    assert len(marked) == 1
    k = marked[0]
    assert [(r, b) for r, b, _ in turn_n[: k + 1]] == [(r, b) for r, b, _ in turn_next[: k + 1]]
    assert turn_next[k + 1 :] and OPEN not in json.dumps(turn_next[: k + 1], ensure_ascii=False)
