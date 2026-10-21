"""U4b-1：插話佇列的 Ctrl+Enter 訊號（ADR-0249 第 4b 段；裁定 2）。

每個 (session, stream) 一個 ``asyncio.Event``：有 urgent 排隊中才 set；取出、收回、過期、
清掉之後 clear，不會漏到下一個串流（串流結束時的 clear 由 test_interjection_queue 守）。
另守 ``round_interrupted_by_user`` 的後端常數與形狀（前端常數由 U4b-2 加）。
"""

from __future__ import annotations

import pytest
from src.core.exceptions import ToukaException
from src.services import interjection_queue as queue_mod
from src.services.interjection_queue import InMemoryInterjectionQueue, InterjectionQueue


def test_signal_follows_queued_urgent_items():
    queue = InMemoryInterjectionQueue()
    queue.enqueue("s0", "st0", "early", urgent=True)
    assert queue.urgent_signal("s0", "st0").is_set()  # 先加入、後來才拿訊號也是 set
    assert hasattr(InterjectionQueue, "urgent_signal") and isinstance(queue, InterjectionQueue)
    signal = queue.urgent_signal("s1", "st1")
    assert signal is queue.urgent_signal("s1", "st1")
    assert signal is not queue.urgent_signal("s1", "st2")
    queue.enqueue("s1", "st1", "plain")
    assert not signal.is_set()  # 一般 Enter 不打斷
    first = queue.enqueue("s1", "st1", "u1", urgent=True)
    second = queue.enqueue("s1", "st1", "u2", urgent=True)
    assert signal.is_set() and not queue.urgent_signal("s2", "st1").is_set()
    queue.retract("s1", "st1", first.id)
    assert signal.is_set()  # 還有一則 urgent
    queue.retract("s1", "st1", second.id)
    assert not signal.is_set()
    third = queue.enqueue("s1", "st1", "u3", urgent=True)
    queue.drain("s1", "st1", step=0)
    assert not signal.is_set()  # 取出就 clear
    with pytest.raises(ToukaException) as exc:  # 取出之後才收回：409
        queue.retract("s1", "st1", third.id)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "INTERJECTION_NOT_RETRACTABLE"
    assert not signal.is_set()


def test_expired_urgent_item_clears_the_signal(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(queue_mod, "get_current_epoch", lambda: now[0])
    queue = InMemoryInterjectionQueue()
    signal = queue.urgent_signal("s1", "st1")
    queue.enqueue("s1", "st1", "now", urgent=True)
    now[0] = 1300.0
    queue.enqueue("s1", "st1", "plain")  # 整串還在用，不會因閒置被清掉
    now[0] = 1601.0  # urgent 超過 TTL：改成 undelivered，不再交給模型
    assert [i.status for i in queue.list_items("s1", "st1")] == ["undelivered", "queued"]
    assert not signal.is_set()


def test_clearing_a_stream_clears_its_signal_and_the_next_stream_starts_clear():
    queue = InMemoryInterjectionQueue()
    old, other = queue.urgent_signal("s1", "st1"), queue.urgent_signal("s2", "st9")
    queue.enqueue("s1", "st1", "now", urgent=True)
    queue.enqueue("s2", "st9", "keep", urgent=True)
    queue.clear("s1", "st1")
    assert not old.is_set()  # 拿著舊訊號的人看不到殘留
    assert not queue.urgent_signal("s1", "st2").is_set()  # 下一個串流
    assert not queue.urgent_signal("s1", "st1").is_set()
    assert other.is_set()  # 別的 session 不受影響
    queue.clear("s2")
    assert not other.is_set()


def test_round_interrupted_event_constant_and_shape():
    assert queue_mod.ROUND_INTERRUPTED_BY_USER_EVENT == "round_interrupted_by_user"
    assert queue_mod.round_interrupted_by_user_event(step=2) == {
        "type": "round_interrupted_by_user", "step": 2}
