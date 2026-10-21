"""上游錄製台（model-p1a U1）。

``RecordingAdapter`` 包住真的 ProviderAdapter，只換掉 ``send_round``：在送出當下把
請求**深拷貝**成快照（``state.payload``／``state.kwargs`` 會跨輪原地修改，存參照會讓
每輪快照都變成最後狀態），再吐出預排的回應。其餘成員全部照轉，所以
``run_react_loop`` 察覺不到差異（L 測試在 test_harness.py）。斷言放在交給傳輸層之前，
不 patch httpx。

- 快照正規化：以 adapter 類別為鍵的小表（新增廠商＝加一個函式）。
- 回應假物件：只在這裡定義一次；Claude 用 ``FakeBlock``（有 ``model_dump``），不用
  MagicMock——MagicMock 無法轉成純資料，快照遇到會直接報錯。
- ``drive()`` 沿用 characterization harness 的時鐘／ctx／假執行器（只 import，不修改）。
  Gemini 顯式快取已全停（ADR-0243），不需要任何快取 patch。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from characterization.harness import (
    UPSTREAM_S,
    drain,
    install_clock,
    make_clock,
    make_ctx,
    make_mock_executor,
)
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.handlers.react_core import run_react_loop
from src.services.rhythm_service import rhythm_service

if TYPE_CHECKING:
    from collections.abc import Callable

CORE = "src.handlers.react_core"


# ── 快照：送出當下轉成純資料（等同深拷貝） ────────────────────────────────────


def to_plain(obj: Any) -> Any:
    """dict／list 逐層重建；SDK 物件走 ``model_dump()``；其他物件（含 MagicMock）拒收。"""
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    # 類別層級才算（MagicMock 的屬性是動態生成的，不算）
    assert getattr(type(obj), "model_dump", None) is not None, (
        f"快照遇到非純資料物件 {type(obj).__name__}；Claude block 請用 FakeBlock"
    )
    return to_plain(obj.model_dump())


def _snap_claude(state: Any) -> dict:
    return {"wire": "anthropic", "kwargs": to_plain(state.kwargs)}


def _snap_openai(state: Any) -> dict:
    return {
        "wire": "openai",
        "url": state.url,
        "headers": to_plain(state.headers),
        "payload": to_plain(state.payload),
    }


def _snap_gemini(state: Any) -> dict:
    return {"wire": "gemini", "url": state.url, "payload": to_plain(state.payload)}


def _snap_responses(state: Any) -> dict:
    """Responses（3b-U4a）：base URL＋create 參數；SDK client 物件不進快照。"""
    return {"wire": "responses", "base_url": state.base_url, "kwargs": to_plain(state.kwargs)}


SNAPSHOTTERS: dict[type, Callable[[Any], dict]] = {
    ClaudeAdapter: _snap_claude,
    OpenAIAdapter: _snap_openai,
    GeminiAdapter: _snap_gemini,
    ResponsesAdapter: _snap_responses,
}


# ── RecordingAdapter ────────────────────────────────────────────────────────


class RecordingAdapter:
    """ProviderAdapter 替身：只換 send_round（快照＋預排回應）。

    其餘成員（含 ``emits_context_breakdown`` 與 Protocol 未宣告、核心會叫的
    ``resolve_provider_label``）一律經 ``__getattr__`` 照轉給真的 adapter。
    """

    def __init__(self, inner: Any, responses: list, *,
                 on_send: Callable[[int], None] | None = None) -> None:
        self.inner = inner
        self.snapshots: list[dict] = []
        self._snapshot = SNAPSHOTTERS[type(inner)]
        self._responses = list(responses)
        self._on_send = on_send

    def __getattr__(self, name: str) -> Any:
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    async def send_round(self, state: Any) -> Any:
        self.snapshots.append(self._snapshot(state))  # 送出當下深拷貝
        if self._on_send is not None:
            self._on_send(len(self.snapshots))
        assert self._responses, f"第 {len(self.snapshots)} 輪沒有預排回應（多送了一輪）"
        yield self._responses.pop(0)


# ── 回應假物件（三家） ──────────────────────────────────────────────────────


@dataclass
class FakeBlock:
    """Anthropic SDK content block 替身（屬性存取＋model_dump）。"""

    type: str
    text: str | None = None
    thinking: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None

    def model_dump(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class FakeUsage:
    input_tokens: int = 10
    output_tokens: int = 5

    def model_dump(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


@dataclass
class FakeMessage:
    """Anthropic SDK Message 替身（content blocks＋usage.model_dump()＋model_dump()）。"""

    content: list[FakeBlock]
    usage: FakeUsage = field(default_factory=FakeUsage)

    def model_dump(self) -> dict:
        return {
            "id": "msg_upstream", "model": "claude-fake", "role": "assistant",
            "content": [b.model_dump() for b in self.content],
            "usage": self.usage.model_dump(),
        }


def claude_text(text: str, thinking: str | None = None) -> FakeMessage:
    blocks = [FakeBlock("thinking", thinking=thinking)] if thinking else []
    return FakeMessage([*blocks, FakeBlock("text", text=text)])


def claude_tools(*calls: tuple[str, dict]) -> FakeMessage:
    return FakeMessage([
        FakeBlock("tool_use", id=f"toolu_{i}", name=name, input=args)
        for i, (name, args) in enumerate(calls, 1)
    ])


def openai_text(text: str) -> dict:
    return {
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def openai_tools(*calls: tuple[str, dict]) -> dict:
    tool_calls = [
        {"id": f"call_{i}", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
        for i, (name, args) in enumerate(calls, 1)
    ]
    return {
        "choices": [{"index": 0, "finish_reason": "tool_calls",
                     "message": {"role": "assistant", "content": None,
                                 "tool_calls": tool_calls}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def gemini_text(text: str) -> dict:
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }


def gemini_tools(*calls: tuple[str, dict]) -> dict:
    parts = [{"functionCall": {"name": name, "args": args}} for name, args in calls]
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }


# ── drive：跑 run_react_loop，回傳每輪快照與下游 SSE ─────────────────────────


@dataclass
class Run:
    snapshots: list[dict]
    chunks: list[str]


def reset_rhythm(*ids: str) -> None:
    for i in ids:
        rhythm_service.clear_session(i)
        rhythm_service.clear_interrupt(i)


async def drive(monkeypatch, adapter: Any, request: Any, responses: list, *,
                tag: str, ctx_extra: dict | None = None) -> Run:
    """以 RecordingAdapter 包住 adapter 跑完整迴圈（假時鐘、假執行器）。"""
    clock = make_clock()
    install_clock(monkeypatch, CORE, clock)
    rid, sid = f"req-up-{tag}", f"sess-up-{tag}"
    reset_rhythm(rid, sid)

    def advance(_round: int) -> None:
        clock["now"] += UPSTREAM_S

    recorder = RecordingAdapter(adapter, responses, on_send=advance)
    ctx = make_ctx(rid, sid, **(ctx_extra or {}))
    try:
        with patch(f"{CORE}.ToolExecutor") as executor:
            executor.return_value.execute_to_queue = AsyncMock(
                side_effect=make_mock_executor(clock)
            )
            chunks = await drain(run_react_loop(recorder, request, ctx))
    finally:
        reset_rhythm(rid, sid)
    return Run(recorder.snapshots, chunks)


# ── shape：一輪快照壓成精簡形狀 ─────────────────────────────────────────────


def _mark(block: Any) -> str:
    """內容區塊型別；帶 Anthropic 快取斷點的加 *。"""
    if not isinstance(block, dict):
        return type(block).__name__
    return block.get("type", "?") + ("*" if "cache_control" in block else "")


def _kinds(content: Any) -> str:
    if content is None:
        return "none"
    if isinstance(content, str):
        return "str"
    return ",".join(_mark(b) for b in content)


def _shape_anthropic(snap: dict) -> dict:
    kw = snap["kwargs"]
    return {
        "keys": sorted(kw),
        "model": kw["model"],
        "max_tokens": kw["max_tokens"],
        "headers": kw.get("extra_headers"),
        "tool_choice": kw.get("tool_choice"),
        "thinking": kw.get("thinking"),
        "system": _kinds(kw["system"]) if "system" in kw else None,
        "tools": [t["name"] + ("*" if "cache_control" in t else "") for t in kw.get("tools", [])],
        "messages": [f"{m['role']}:{_kinds(m['content'])}" for m in kw["messages"]],
    }


def _shape_openai(snap: dict) -> dict:
    p = snap["payload"]
    return {
        "url": snap["url"],
        "headers": sorted(snap["headers"]),
        "keys": sorted(p),
        "tool_choice": p.get("tool_choice"),
        "tools": [t.get("function", {}).get("name") for t in p.get("tools", [])],
        "messages": [
            f"{m['role']}:{_kinds(m.get('content'))}"
            + ("+tool_calls" if m.get("tool_calls") else "")
            for m in p["messages"]
        ],
    }


def _shape_gemini(snap: dict) -> dict:
    p = snap["payload"]
    return {
        "url": snap["url"],
        "keys": sorted(p),
        "tool_config": p.get("tool_config", {}).get("function_calling_config", {}).get("mode"),
        "tools": [d["name"] for t in p.get("tools", []) for d in t.get("functionDeclarations", [])],
        "contents": [
            f"{c['role']}:" + ",".join(next(iter(part), "?") for part in c["parts"])
            for c in p["contents"]
        ],
    }


_SHAPERS: dict[str, Callable[[dict], dict]] = {
    "anthropic": _shape_anthropic,
    "openai": _shape_openai,
    "gemini": _shape_gemini,
}


def shape(snap: dict) -> dict:
    """頂層鍵、tool_choice、system／斷點位置、角色與內容型別序列、工具名、標頭。"""
    return _SHAPERS[snap["wire"]](snap)
