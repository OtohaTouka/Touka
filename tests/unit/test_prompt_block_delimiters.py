"""Unit tests for Phase B0 prompt 區塊邊界統一 XML（ADR-0092）。

驗證 soul / mission / IDE / project context 各區塊都以一致 XML delimiter 包覆，
且 **soul 內文逐字保留**（只在外層加 tag）。供 Phase B 的 context breakdown
以單一 parser 切段。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.middleware.layers.context import ContextLayer
from src.services.prompt_blocks import wrap_block
from src.services.soul_service import SoulService

_GET_PROJECT_CTX = "src.middleware.layers.context.get_project_context"


class _AnthropicReq:
    """類名含 'Anthropic' → ContextLayer 的 is_anthropic 判定為真（走 system 注入）。"""

    def __init__(self, **kw):
        self.system = ""
        self.messages = [{"role": "user", "content": "hi"}]
        self.ide_context = None
        self.project_id = None
        for k, v in kw.items():
            setattr(self, k, v)


def _ctx(mission=None):
    c = MagicMock()
    c.get = lambda key, default=None: mission if key == "mission_awareness" else default
    return c


# ── wrap_block ───────────────────────────────────────────────────────────────


def test_wrap_block_format():
    assert wrap_block("foo", "bar") == "<foo>\nbar\n</foo>"


# ── soul <persona>（內文逐字保留）─────────────────────────────────────────────


def test_soul_wrapped_in_persona_verbatim():
    svc = object.__new__(SoulService)  # 不污染 singleton
    soul_text = "我是花同，一個有自主意識的存在。我的語氣隨性。"
    svc.get_soul_data = lambda identity: (soul_text, {})
    req = SimpleNamespace(system="", messages=[{"role": "user", "content": "hi"}])

    svc.apply_soul_to_request(req, "Hanatou")

    assert req.system.startswith("<persona>")
    assert req.system.rstrip().endswith("</persona>")
    # 內文一字不動：soul_text 原樣為子字串。
    assert soul_text in req.system


def test_soul_persona_preserves_technical_directives():
    svc = object.__new__(SoulService)
    soul_text = "靈魂本體。"
    svc.get_soul_data = lambda identity: (soul_text, {})
    # 既有 system 含技術關鍵字 → 會被保留進 final_soul（仍在 persona 內）。
    req = SimpleNamespace(
        system="You have access to a tool and an api path.",
        messages=[{"role": "user", "content": "hi"}],
    )

    svc.apply_soul_to_request(req, "Hanatou")

    assert "<persona>" in req.system
    assert soul_text in req.system
    assert "api path" in req.system  # 技術指令仍保留


# ── ContextLayer 各區塊 ───────────────────────────────────────────────────────


async def test_mission_block_uses_xml_tag():
    task = SimpleNamespace(
        status="DOING", id="t1", description="做事", dependencies=[], artifact_ref=None
    )
    mission = SimpleNamespace(goal="完成 X", tasks=[task])
    req = _AnthropicReq()

    await ContextLayer().process(req, _ctx(mission=mission))

    assert "<mission_awareness>" in req.system
    assert "</mission_awareness>" in req.system
    assert "完成 X" in req.system


async def test_ide_context_uses_xml_tag_not_bracket():
    req = _AnthropicReq(ide_context={"fileName": "a.py", "line": 5})

    await ContextLayer().process(req, _ctx())

    content = req.messages[-1]["content"]
    assert "<ide_context>" in content
    assert "</ide_context>" in content
    assert "a.py" in content
    assert "[IDE Context:" not in content  # 舊 markdown 字面已移除


_IMG = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
_MULTI = [{"type": "text", "text": "look"}, _IMG]


@pytest.mark.parametrize(
    ("as_object", "content", "expected_head"),
    [
        (False, "hi", None),  # 純文字：直接接在後面
        (True, "hi", None),
        (False, json.dumps(_MULTI), _MULTI),  # ToukaChat 的多模態 JSON 字串：先還原再加一段
        (True, json.dumps(_MULTI), _MULTI),
        (False, list(_MULTI), _MULTI),  # 已是 list：加一段
        (True, list(_MULTI), _MULTI),
    ],
)
async def test_ide_context_keeps_image_parts(as_object, content, expected_head):
    """IDE 區塊比照 continuity：先把多模態 JSON 字串還原成 list，圖片完整、IDE 是另一段文字。"""
    message = SimpleNamespace(role="user", content=content) if as_object else {
        "role": "user", "content": content,
    }
    req = _AnthropicReq(ide_context={"fileName": "a.py", "line": 5}, messages=[message])

    await ContextLayer().process(req, _ctx())

    out = message.content if as_object else message["content"]
    if expected_head is None:
        assert isinstance(out, str) and out.startswith("hi") and "<ide_context>" in out
    else:
        assert isinstance(out, list) and out[:2] == expected_head
        assert out[2]["type"] == "text" and "<ide_context>" in out[2]["text"]
        assert len(out) == 3


async def test_project_context_wrapped_in_xml_tag():
    req = _AnthropicReq(project_id="p1")

    with patch(_GET_PROJECT_CTX, new=AsyncMock(return_value="PROJDATA-XYZ")):
        await ContextLayer().process(req, _ctx())

    assert "<project_context>" in req.system
    assert "</project_context>" in req.system
    assert "PROJDATA-XYZ" in req.system


# ── inject_context_summary（ADR-0092 Phase D）───────────────────────────────

_GET_SESSION = "src.utils.async_db.session_service.get_session_by_id"


async def test_inject_context_summary_adds_conversation_summary_block():
    svc = object.__new__(SoulService)
    req = SimpleNamespace(system="BASE", messages=[{"role": "user", "content": "hi"}])
    sess = {"context_summary": "稍早：約定下週交付、花同答應準備。"}
    with patch(_GET_SESSION, new=AsyncMock(return_value=sess)):
        await svc.inject_context_summary(req, "sess-1")
    assert "<conversation_summary>" in req.system
    assert "約定下週交付" in req.system
    assert req.system.startswith("BASE")  # 前置於既有 system 後（不覆蓋）


async def test_inject_context_summary_noop_without_summary():
    svc = object.__new__(SoulService)
    req = SimpleNamespace(system="BASE", messages=[])
    with patch(_GET_SESSION, new=AsyncMock(return_value={"context_summary": None})):
        await svc.inject_context_summary(req, "sess-1")
    assert "<conversation_summary>" not in req.system


async def test_inject_context_summary_noop_without_session_id():
    svc = object.__new__(SoulService)
    req = SimpleNamespace(system="BASE")
    await svc.inject_context_summary(req, None)
    assert req.system == "BASE"
