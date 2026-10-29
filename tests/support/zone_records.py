"""測試期間的區例外紀錄：記錄，測試結束時判定（ADR-0293 R13）。

conftest 把 ``observe`` 登記成 ``zone_paths`` 的觀察者：區的三種例外與 ``ZonePath`` 被當成
路徑用時都會記一筆，帶「建立紀錄當時」的測試 id（contextvar，asyncio 工作與 ``to_thread``
會帶過去；拿不到時用全域的目前測試 id）。測試結束時有紀錄就失敗，所以產品程式接住了例外，
測試照樣紅。預期會有拒絕的測試用 ``expect_zone_refusal`` 宣告。

``zone_paths`` 匯入不到時（在改之前的樹上跑連結測試，R15）什麼都不做。
"""

from __future__ import annotations

import contextlib
import contextvars
import importlib
import itertools
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

try:
    zone_paths = importlib.import_module("memory_server.utils.zone_paths")
except ImportError:  # 改之前的樹沒有這個模組
    zone_paths = None


@dataclass(frozen=True)
class Record:
    """一筆紀錄：``kind`` 是 ``refusal``（觀察者）或 ``tripwire``（Z9）."""

    seq: int
    test_id: str | None
    kind: str
    error: str
    display: str | None
    detail: str


_current_test: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "zone_records_test", default=None
)
_global_test: str | None = None
_records: list[Record] = []
_lock = threading.Lock()
_seq = itertools.count(1)


def _test_id() -> str | None:
    return _current_test.get() or _global_test


def add(kind: str, error: str, display: str | None, detail: str = "") -> None:
    """記一筆（觀察者與 Z9 共用）."""
    with _lock:
        _records.append(Record(next(_seq), _test_id(), kind, error, display, detail))


def observe(event: object) -> None:
    """``zone_paths`` 的觀察者：類別名稱、顯示路徑與誤用的方法."""
    error = event.error  # type: ignore[attr-defined]
    add("refusal", error.__name__, event.display, event.detail)  # type: ignore[attr-defined]


def begin(test_id: str) -> contextvars.Token[str | None]:
    """測試開始：之後建立的紀錄都帶這個 id."""
    global _global_test
    _global_test = test_id
    return _current_test.set(test_id)


def end(test_id: str, token: contextvars.Token[str | None]) -> list[Record]:
    """測試結束：取出這個測試的紀錄（之後才到的照它自己的 id 留到 session 結束）."""
    global _global_test
    _current_test.reset(token)
    _global_test = None
    with _lock:
        mine = [r for r in _records if r.test_id == test_id]
        _records[:] = [r for r in _records if r.test_id != test_id]
    return mine


def remaining() -> list[Record]:
    """session 結束時還留著的紀錄（測試結束後才到的、或不屬於任何測試的）."""
    with _lock:
        left = list(_records)
        _records.clear()
    return left


def describe(records: list[Record]) -> str:
    """失敗訊息：每筆一行（種類、類別、顯示路徑、細節）."""
    return "\n".join(f"  {r.kind} {r.error} {r.display} {r.detail}".rstrip() for r in records)


def _matches(record: Record, names: frozenset[str], display_prefix: str | None) -> bool:
    return (
        record.kind == "refusal"
        and record.error in names
        and (display_prefix is None or (record.display or "").startswith(display_prefix))
    )


def _names(cls: type[BaseException] | str) -> frozenset[str]:
    """``cls`` 與它的子類別的名稱（傳名稱時只認那一個；改之前的樹上類別匯入不到）."""
    if isinstance(cls, str):
        return frozenset({cls})
    found = {cls.__name__}
    pending = [cls]
    while pending:
        for sub in pending.pop().__subclasses__():
            found.add(sub.__name__)
            pending.append(sub)
    return frozenset(found)


@contextlib.contextmanager
def expect_zone_refusal(
    cls: type[BaseException] | str,
    *,
    at_least: int = 1,
    at_most: int | None = None,
    display_prefix: str | None = None,
) -> Iterator[None]:
    """宣告這段會有 ``cls`` 的拒絕：次數要在範圍內（宣告了沒發生也算失敗）；不吸收 Z9 的紀錄.

    權限自癒修完會重試一次，同一個呼叫可能建立兩次例外，所以是範圍。
    """
    if zone_paths is None:
        yield
        return
    test_id = _test_id()
    with _lock:
        start = next(_seq)
    yield
    names = _names(cls)
    with _lock:
        hit = [
            r
            for r in _records
            if r.seq > start and r.test_id == test_id and _matches(r, names, display_prefix)
        ]
        _records[:] = [r for r in _records if r not in hit]
    if len(hit) < at_least or (at_most is not None and len(hit) > at_most):
        bound = f"{at_least}..{'' if at_most is None else at_most}"
        pytest.fail(f"expected {bound} zone refusal(s) of {sorted(names)}, got {len(hit)}")


@contextlib.contextmanager
def raises_zone(
    cls: type[BaseException],
    *,
    match: str | None = None,
    at_least: int = 1,
    at_most: int | None = None,
) -> Iterator[pytest.ExceptionInfo[BaseException]]:
    """``pytest.raises(cls)`` 加上同一個類別的 ``expect_zone_refusal``：這段預期丟出區的例外."""
    with (
        expect_zone_refusal(cls, at_least=at_least, at_most=at_most),
        pytest.raises(cls, match=match) as info,
    ):
        yield info
