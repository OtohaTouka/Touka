"""只有 ``messages.create`` 的假 Claude client 用的串流替身（3b-U1；ADR-0248 附註 15）。

主迴圈改走 ``messages.stream()`` 之後，假 client 呼叫 ``delegate_stream_to_create(client)``：
``stream(**kw)`` 進入時呼叫當下的 ``messages.create(**kw)``（例外照拋），事件只有一個
``message_stop``，``get_final_message()`` 回 create 的結果；``stream`` 是 MagicMock，可數呼叫次數。

不產生位元組、也不經過 httpx2，所以**不能**拿來證明閒置逾時或關上游；那些用真的
AsyncAnthropic＋httpx2.MockTransport（test_claude_stream_sdk.py、test_claude_stream_timeouts.py）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock


class _CreateBackedStream:
    """``AsyncMessageStreamManager``／``AsyncMessageStream`` 的最小替身。"""

    def __init__(self, messages: Any, kwargs: dict[str, Any]) -> None:
        self._messages = messages
        self._kwargs = kwargs
        self._message: Any = None

    async def __aenter__(self) -> _CreateBackedStream:
        self._message = await self._messages.create(**self._kwargs)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def __aiter__(self) -> Any:
        yield SimpleNamespace(type="message_stop")

    async def get_final_message(self) -> Any:
        return self._message


def delegate_stream_to_create(client: Any) -> Any:
    """讓 ``client.messages.stream`` 委派給 ``client.messages.create``；回傳同一個 client。"""
    messages = client.messages
    messages.stream = MagicMock(side_effect=lambda **kw: _CreateBackedStream(messages, kw))
    return client
