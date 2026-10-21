"""自寫上游 SSE：解析器（3b 共用）與 OpenAI 相容 Chat 彙整器（ADR-0248 附註 14）。

解析照 WHATWG 規則：行尾 CRLF／LF／CR 都算；``:`` 開頭是註解；``data:`` 後的一個空白可有可無；
多行 data 以換行接起；空行送出事件；串流開頭的 BOM 去掉。檔尾沒有空行時，最後一個事件照送
（比規格寬鬆；完整與否另由 ``[DONE]`` 判斷）。位元組先過增量 UTF-8 解碼，切在字元中間也不壞。

彙整器把 chunk 拼回非串流回應的形狀，鍵只來自 chunk（不自己補）：choice 的 ``index`` 與
``finish_reason`` 有收到才有；message 的 ``content`` 一定在（只有工具呼叫時是 None）；中途的
null 不蓋掉已收到的內容；工具呼叫依 index 拼接（缺號、交錯、沒有 index、同一 index 換了 id
都照收），輸出不帶 index、沒給 type 補 ``function``。chunk 裡出現 ``message`` 鍵直接拒收。

Gemini 彙整器的規則（text part 何時合併、thoughtSignature 放哪裡、完整訊號）寫在 ADR-0248。
"""

from __future__ import annotations

import codecs
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_EOL = re.compile(r"\r\n|\r|\n")


class StreamShapeError(Exception):
    """上游 chunk 形狀不對（例如帶著非串流的 ``message``）。"""


async def sse_data(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """逐一吐出每個 SSE 事件的 data（多行以換行接起）。"""
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending, data, started = "", [], False
    async for chunk in chunks:
        pending += decoder.decode(chunk)
        if not started and pending:
            pending, started = pending.removeprefix("\ufeff"), True
        lines, pending = _split_lines(pending)
        for line in lines:
            if (event := _feed(line, data)) is not None:
                yield event
    for line in _split_lines(pending + decoder.decode(b"", final=True) + "\n")[0]:
        if (event := _feed(line, data)) is not None:
            yield event
    if data:
        yield "\n".join(data)


def _split_lines(text: str) -> tuple[list[str], str]:
    """完整的行與剩下的半行；結尾單獨的 CR 先留著（下一段可能是 LF）。"""
    lines, start = [], 0
    for match in _EOL.finditer(text):
        if match.group() == "\r" and match.end() == len(text):
            break
        lines.append(text[start:match.start()])
        start = match.end()
    return lines, text[start:]


def _feed(line: str, data: list[str]) -> str | None:
    if not line:
        event = "\n".join(data) if data else None
        data.clear()
        return event
    field, _, value = line.partition(":")
    if field == "data":
        data.append(value.removeprefix(" "))
    return None  # 註解（field 為空）、event／id／retry 與未知欄位都略過


class ChatStreamAggregator:
    """OpenAI 相容 ``chat.completion.chunk`` → 非串流回應 dict（形狀見模組說明）。"""

    def __init__(self) -> None:
        self._top: dict[str, Any] = {}
        self._choices: dict[int, dict[str, Any]] = {}

    def add(self, chunk: dict[str, Any]) -> None:
        """併入一個 chunk；``message`` 鍵（非串流形狀）與型別不對的欄位一律 StreamShapeError。"""
        try:
            self._add(chunk)
        except (AttributeError, TypeError, KeyError) as e:  # JSON 合法但形狀不對（R-3b ② L4）
            raise StreamShapeError(chunk) from e

    def _add(self, chunk: dict[str, Any]) -> None:
        for key, value in chunk.items():
            if key == "choices":
                self._top.setdefault("choices", None)
                for choice in value or []:
                    self._add_choice(choice)
            elif key == "usage":
                if value is not None or key not in self._top:
                    self._top[key] = value
            else:
                self._top.setdefault(key, value)

    def _add_choice(self, choice: dict[str, Any]) -> None:
        if "message" in choice:
            raise StreamShapeError(choice)
        slot = self._choices.setdefault(choice.get("index", 0), {
            "head": {"index": choice["index"]} if "index" in choice else {},
            "extra": {}, "fields": {}, "calls": [], "by_index": {}, "finish": None,
        })
        if choice.get("finish_reason") is not None:
            slot["finish"] = choice["finish_reason"]
        for key, value in choice.items():  # 其他 choice 欄位（如 logprobs）照收第一次的值
            if key not in ("index", "delta", "finish_reason"):
                slot["extra"].setdefault(key, value)
        for key, value in (choice.get("delta") or {}).items():
            if key == "tool_calls":
                for call in value or []:
                    _merge_call(slot, call)
            elif value is None:
                slot["fields"].setdefault(key, None)
            elif isinstance(value, str) and key != "role":
                slot["fields"][key] = (slot["fields"].get(key) or "") + value
            else:
                slot["fields"][key] = value

    def result(self) -> dict[str, Any]:
        """拼好的回應；choices 依 index 排序。"""
        out = dict(self._top)
        if "choices" in out:
            out["choices"] = [_choice(self._choices[i]) for i in sorted(self._choices)]
        return out


def _merge_call(slot: dict[str, Any], call: dict[str, Any]) -> None:
    index, function = call.get("index"), call.get("function") or {}
    current = slot["by_index"].get(index) if index is not None else (
        slot["calls"][-1] if slot["calls"] and not call.get("id") else None)
    if current is None or (call.get("id") and current["id"] not in (None, call["id"])):
        current = {"id": None, "type": None, "name": None, "arguments": ""}
        slot["calls"].append(current)
        slot["by_index"][index] = current
    for key in ("id", "type"):
        current[key] = call.get(key) or current[key]
    current["name"] = function.get("name") or current["name"]
    current["arguments"] += function.get("arguments") or ""


def _choice(slot: dict[str, Any]) -> dict[str, Any]:
    message = {"content": None, **slot["fields"]}
    if slot["calls"]:
        message["tool_calls"] = [
            {"id": c["id"], "type": c["type"] or "function",
             "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in slot["calls"]
        ]
    choice = {**slot["head"], **slot["extra"], "message": message}
    if slot["finish"] is not None:
        choice["finish_reason"] = slot["finish"]
    return choice


_JOINABLE_PREV = frozenset({"text", "thought"})
_JOINABLE_NEXT = frozenset({"text", "thought", "thoughtSignature"})


class GeminiStreamAggregator:
    """Gemini SSE 的 GenerateContentResponse 序列 → 非串流回應 dict（規則見 ADR-0248）。"""

    def __init__(self) -> None:
        self._top: dict[str, Any] = {}
        self._candidates: dict[int, dict[str, Any]] = {}
        self.complete = False  # 任一 candidate 有 finishReason，或 promptFeedback 有 blockReason

    def add(self, chunk: Any) -> None:
        """併入一個 chunk（chunk、candidate、part 不是物件或型別不對＝StreamShapeError）。"""
        try:
            self._add(chunk)
        except (AttributeError, TypeError, KeyError) as e:  # JSON 合法但形狀不對（R-3b ② L4）
            raise StreamShapeError(chunk) from e

    def _add(self, chunk: Any) -> None:
        if not isinstance(chunk, dict):
            raise StreamShapeError(chunk)
        feedback = chunk.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            self.complete = True
        for key, value in chunk.items():
            if key == "candidates":
                self._top.setdefault("candidates", None)
                for candidate in value or []:
                    self._add_candidate(candidate)
            elif key == "usageMetadata":
                if value is not None or key not in self._top:
                    self._top[key] = value  # 每個 chunk 都帶，取最後一個
            else:
                self._top.setdefault(key, value)

    def _add_candidate(self, candidate: Any) -> None:
        if not isinstance(candidate, dict):
            raise StreamShapeError(candidate)
        if candidate.get("finishReason"):
            self.complete = True
        slot = self._candidates.setdefault(candidate.get("index", 0), {})
        for key, value in candidate.items():
            if key == "content" and isinstance(value, dict):
                content = slot.setdefault("content", {k: v for k, v in value.items()
                                                      if k != "parts"})
                parts = content.setdefault("parts", [])
                for n, part in enumerate(value.get("parts") or []):
                    if not isinstance(part, dict):
                        raise StreamShapeError(part)
                    if n == 0 and parts and _joinable(parts[-1], part):
                        parts[-1]["text"] += part["text"]
                        if "thoughtSignature" in part:
                            parts[-1]["thoughtSignature"] = part["thoughtSignature"]
                    else:
                        parts.append(dict(part))
            elif value is not None or key not in slot:
                slot[key] = value

    def result(self) -> dict[str, Any]:
        """拼好的回應；candidates 依 index 排序。"""
        out = dict(self._top)
        if "candidates" in out:
            out["candidates"] = [self._candidates[i] for i in sorted(self._candidates)]
        return out


def _joinable(prev: dict[str, Any], part: dict[str, Any]) -> bool:
    return ("text" in prev and "text" in part and prev.keys() <= _JOINABLE_PREV
            and part.keys() <= _JOINABLE_NEXT
            and bool(prev.get("thought")) == bool(part.get("thought")))
