"""主迴圈自寫串流用的共用假傳輸（3b-U2；ADR-0248 附註 17）。

- 換掉 ``httpx.AsyncClient.send``：``client.stream(...)`` 與 ``client.post(...)`` 都走這裡，用的是
  httpx 真的 ``Request``／``Response``，回應本體是非同步位元組串流。
- 送出內容從 ``request.content`` 解析（``sent``），不看呼叫參數。
- 記請求次數；每個請求呼叫一次 ``on_request(n)``（例如推進假時鐘）；``assert_all_closed()``
  檢查每個回應都關了。
- ``stream_reply(fixture, mode)`` 把非串流回應 dict 改成 SSE：``one``＝1 個 chunk；``semantic``＝
  文字、思考、工具參數拆段、用量放最後的 ``choices: []`` chunk；``bytes``＝把 semantic 的位元組
  用固定種子亂切（至少一刀切在 UTF-8 字元中間）。都以 ``[DONE]`` 結尾。
- ``gemini_stream_reply(fixture, mode)``：Gemini 版（3b-U3）。每個事件是一份完整回應；``semantic``
  把 text part 拆兩段（前一個 part 的後半與下一個 part 的前半放同一個 chunk，part 邊界不落在
  chunk 交界）、``usageMetadata`` 每個 chunk 都帶（中途是部分值）、candidate 其他欄位放最後一個
  chunk；沒有 ``[DONE]``。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

SSE_HEADERS = {"content-type": "text/event-stream"}
MODES = ("one", "semantic", "bytes")


class _Pieces(httpx.AsyncByteStream):
    """腳本化的回應位元組：bytes＝送出、float＝等幾秒、Event＝停住等它、例外＝中途斷線。"""

    def __init__(self, pieces: list) -> None:
        self._pieces = pieces

    async def __aiter__(self) -> Any:
        for piece in self._pieces:
            if isinstance(piece, bytes):
                yield piece
            elif isinstance(piece, asyncio.Event):
                await piece.wait()
            elif isinstance(piece, BaseException):
                raise piece
            else:
                await asyncio.sleep(piece)


@dataclass
class Reply:
    """一個預排的上游回應。"""

    status: int = 200
    pieces: list = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=lambda: dict(SSE_HEADERS))
    error: Exception | None = None  # 有值＝送出時直接丟這個例外（連線錯誤）


class FakeUpstream:
    """``httpx.AsyncClient.send`` 的替身（用 ``install(monkeypatch)`` 裝上）。"""

    def __init__(self, replies: list[Reply], *, on_request: Any = None) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []
        self._on_request = on_request

    def send_fn(self) -> Any:
        """可直接放到 ``httpx.AsyncClient.send`` 的函式（smoke 用 ``patch.object``）。"""

        async def send(_client: httpx.AsyncClient, request: httpx.Request, *,
                       stream: bool = False, **_kwargs: Any) -> httpx.Response:
            return await self._send(request, stream=stream)

        return send

    def install(self, monkeypatch: Any) -> FakeUpstream:
        """以 pytest monkeypatch 換掉 ``httpx.AsyncClient.send``。"""
        monkeypatch.setattr(httpx.AsyncClient, "send", self.send_fn())
        return self

    async def _send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
        self.requests.append(request)
        if self._on_request is not None:
            self._on_request(len(self.requests))
        assert self.replies, f"第 {len(self.requests)} 個請求沒有預排回應"
        reply = self.replies.pop(0)
        if reply.error is not None:
            raise reply.error
        response = httpx.Response(reply.status, headers=reply.headers,
                                  stream=_Pieces(reply.pieces), request=request)
        self.responses.append(response)
        if not stream:
            await response.aread()
        return response

    @property
    def sent(self) -> list[dict]:
        """每個請求送出的 JSON（從 request.content 解析）。"""
        return [json.loads(request.content) for request in self.requests]

    def assert_all_closed(self) -> None:
        """每個回應都已關閉（含錯誤回應）。"""
        assert [r.is_closed for r in self.responses] == [True] * len(self.responses)


def json_reply(body: Any, status: int = 200) -> Reply:
    """非串流回應（自訂連線或錯誤狀態）。"""
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return Reply(status, [text.encode()], {"content-type": "application/json"})


def sse(*events: Any, done: bool = True) -> bytes:
    """把事件（dict 轉 JSON、str 原樣）組成 SSE 位元組；預設以 ``[DONE]`` 結尾。"""
    lines = [e if isinstance(e, str) else json.dumps(e, ensure_ascii=False) for e in events]
    return "".join(f"data: {line}\n\n" for line in [*lines, *(["[DONE]"] if done else [])]).encode()


def _delta(message: dict, *, split: bool) -> list[dict]:
    """非串流 message → delta 序列（split＝文字、思考、工具參數各拆兩段）。"""
    deltas: list[dict] = []
    for key, value in message.items():
        if key == "tool_calls":
            for i, call in enumerate(value):
                head = {"index": i, "id": call["id"], "type": call["type"],
                        "function": {"name": call["function"]["name"], "arguments": ""}}
                args = call["function"]["arguments"]
                cut = len(args) // 2 if split else len(args)
                head["function"]["arguments"] = args[:cut]
                deltas.append({"tool_calls": [head]})
                if split:
                    rest = {"index": i, "function": {"arguments": args[cut:]}}
                    deltas.append({"tool_calls": [rest]})
        elif isinstance(value, str) and split and key != "role" and len(value) > 1:
            cut = len(value) // 2
            deltas += [{key: value[:cut]}, {key: None}, {key: value[cut:]}]  # 中途的 null 不蓋掉
        else:
            deltas.append({key: value})
    return deltas


def stream_chunks(fixture: dict, mode: str) -> list[dict]:
    """非串流回應 dict → chunk 序列（``one``／``semantic``）。"""
    choice = fixture["choices"][0]
    head = {"index": choice["index"]} if "index" in choice else {}
    split = mode != "one"
    deltas = _delta(choice["message"], split=split) or [{}]
    if not split:  # 1 個 chunk：同一個 delta 帶全部欄位與全部工具呼叫
        merged: dict = {}
        for delta in deltas:
            for key, value in delta.items():
                merged[key] = merged.get(key, []) + value if key == "tool_calls" else value
        deltas = [merged]
    chunks = []
    for n, delta in enumerate(deltas):
        last = n == len(deltas) - 1
        finish = choice.get("finish_reason") if last else None
        item = {**head, "delta": delta, "finish_reason": finish}
        chunks.append({"choices": [item]})
    if "usage" in fixture:
        if split:
            chunks.append({"choices": [], "usage": fixture["usage"]})
        else:
            chunks[-1]["usage"] = fixture["usage"]
    return chunks


def byte_cuts(body: bytes, seed: int = 20261012, count: int = 9) -> list[bytes]:
    """固定種子亂切位元組，並保證至少一刀切在多位元組 UTF-8 字元中間。"""
    cuts, x = set(), seed
    for _ in range(count):  # 固定種子的線性同餘序列（決定性，非密碼學用途）
        x = (x * 1103515245 + 12345) % 2**31
        cuts.add(1 + x % (len(body) - 1))
    wide = next((i for i, b in enumerate(body) if b >= 0xC0), None)
    if wide is not None:
        cuts.add(wide + 1)
    edges = [0, *sorted(cuts), len(body)]
    return [body[a:b] for a, b in zip(edges, edges[1:], strict=False) if b > a]


def stream_reply(fixture: dict, mode: str) -> Reply:
    """把 golden 的非串流回應包成串流回應（三種模式見模組說明）。"""
    body = sse(*stream_chunks(fixture, "one" if mode == "one" else "semantic"))
    return Reply(200, byte_cuts(body) if mode == "bytes" else [body])


_SPLITTABLE = frozenset({"text", "thought", "thoughtSignature"})


def _gemini_halves(part: dict) -> tuple[dict, dict | None]:
    """可拆的 text part → (前半, 後半；簽章放後半)；其他 part 不拆。"""
    text = part.get("text")
    if not isinstance(text, str) or len(text) < 2 or not part.keys() <= _SPLITTABLE:
        return part, None
    cut = len(text) // 2
    head = {k: v for k, v in part.items() if k != "thoughtSignature"} | {"text": text[:cut]}
    tail = {"text": text[cut:], **({"thought": True} if part.get("thought") else {})}
    if "thoughtSignature" in part:
        tail["thoughtSignature"] = part["thoughtSignature"]
    return head, tail


def gemini_stream_chunks(fixture: dict, mode: str) -> list[dict]:
    """非串流 GenerateContentResponse → chunk 序列（``one``／``semantic``）。"""
    candidates = fixture.get("candidates") or []
    content = candidates[0].get("content") if candidates else None
    if mode == "one" or not content or not content.get("parts"):
        return [fixture]
    halves = [_gemini_halves(part) for part in content["parts"]]
    groups = [[halves[0][0]]]
    for (_head, prev_tail), (head, _tail) in zip(halves, halves[1:], strict=False):
        groups.append([*([prev_tail] if prev_tail else []), head])
    if halves[-1][1]:
        groups.append([halves[-1][1]])
    extra = {k: v for k, v in candidates[0].items() if k != "content"}
    chunks = []
    for n, parts in enumerate(groups):
        last = n == len(groups) - 1
        candidate = {"content": {**{k: v for k, v in content.items() if k != "parts"},
                                 "parts": parts}, **(extra if last else {})}
        chunk: dict = {}
        for key, value in fixture.items():
            if key == "candidates":
                chunk[key] = [candidate]
            elif key == "usageMetadata":
                chunk[key] = value if last else {**value, "candidatesTokenCount": n}
            else:
                chunk[key] = value
        chunks.append(chunk)
    return chunks


def gemini_stream_reply(fixture: dict, mode: str) -> Reply:
    """把 Gemini 非串流回應包成串流回應（模式同 ``stream_reply``；Gemini 沒有 ``[DONE]``）。"""
    body = sse(*gemini_stream_chunks(fixture, "one" if mode == "one" else "semantic"), done=False)
    return Reply(200, byte_cuts(body) if mode == "bytes" else [body])


def status_reply(status: int, text: str = "server busy") -> Reply:
    """非 200 的純文字回應（例如 503）。"""
    return Reply(status, [text.encode()], {"content-type": "text/plain"})
