"""G14（守衛涵蓋缺口待辦）— 伺服端 SSE 消費者必須只讀 delta.content（排除 reasoning）。

缺口背景（見 docs/architecture/proposals/guard-coverage-gap-backlog.md · G14）：
    ADR-0152：既有伺服端 SSE 消費者（dispatcher 子代理累積、heartbeat 轉錄、SNS
    gateway、subagent 收流）必須維持「只讀 delta.content」語意，使 reasoning-only
    chunk（有 delta.reasoning_content、無 delta.content）被自然略過＝心跳轉錄/派工正文
    正確排除思考。

    此不變式**先前零測試**：四消費者對應測試全只餵 content chunk，無一餵 reasoning-only
    chunk 斷言萃取為空；若有人為某消費者加上 reasoning_content 處理，思考會靜默漏進
    心跳轉錄與派工結果（原 bug 在自動化平面重演）。

本檔兩鎖：① 行為——dispatcher/subagent 的萃取對 reasoning-only chunk 回空；
② 源碼掃描——四消費檔皆不得讀取 reasoning_content（現況 0，回歸加入即紅）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.services.dispatcher import _extract_delta_text
from src.tools.subagent_tools import _drain_text

_REPO = Path(__file__).resolve().parents[2]

# 純 reasoning chunk：有 reasoning_content、無 content（grok-4.5 獨立思考頻道形態）
_REASONING_LINE = 'data: ' + json.dumps(
    {"choices": [{"delta": {"reasoning_content": "let me think..."}}]}
)
_CONTENT_LINE = 'data: ' + json.dumps({"choices": [{"delta": {"content": "hello"}}]})


def test_dispatcher_extract_skips_reasoning_only_chunk():
    """dispatcher._extract_delta_text：reasoning-only → 空；content → 文字。"""
    assert _extract_delta_text(_REASONING_LINE) == "", (
        "dispatcher 累積了 reasoning_content＝思考漏進派工正文（ADR-0152／G14）"
    )
    assert _extract_delta_text(_CONTENT_LINE) == "hello"  # 非空砲：content 照常萃取


@pytest.mark.asyncio
async def test_subagent_drain_skips_reasoning_only_chunk():
    """subagent._drain_text：reasoning-only chunk 被略過，只累積 content。"""

    async def _stream():
        yield _REASONING_LINE + "\n\n"
        yield _CONTENT_LINE + "\n\n"
        yield "data: [DONE]\n\n"

    assert await _drain_text(_stream()) == "hello"


def test_no_sse_consumer_reads_reasoning_content():
    """源碼掃描：四伺服端 SSE 消費者皆不得讀取 reasoning_content（純消費者、只讀 content）。"""
    consumers = [
        "ToukaProxy/src/services/dispatcher.py",
        "ToukaProxy/src/tools/subagent_tools.py",
        "ToukaProxy/src/services/sns_gateway.py",
        "ToukaProxy/src/routers/heartbeat.py",
    ]
    for rel in consumers:
        content = (_REPO / rel).read_text(encoding="utf-8")
        assert "reasoning_content" not in content, (
            f"{rel} 讀取 reasoning_content＝思考可能漏進心跳轉錄/派工正文（ADR-0152／G14）；"
            "SSE 消費者必須只讀 delta.content"
        )
