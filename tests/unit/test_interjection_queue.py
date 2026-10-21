"""插話佇列（ADR-0249 第 4a 段）：狀態機、上限、TTL、鍵隔離與串流生命週期清空。

佇列以 (session_id, stream_id) 為鍵、只放記憶體。狀態：queued → pending(step=N) →
delivered(step=N)，或 → undelivered／retracted。串流結束或同 session 開新串流時清空。
期望值一律寫字面值；錯誤以 errors.json 的代碼字串與 HTTP 狀態比對。
"""

from __future__ import annotations

import pytest
from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity
from src.models.session_persistence import IncognitoStreamAdmission, PersistedStreamAdmission
from src.services import interjection_queue as queue_mod
from src.services import stream_buffer_service as buffer_mod
from src.services.interjection_queue import InMemoryInterjectionQueue, InterjectionQueue
from src.services.stream_buffer_service import STREAM_ACTIVE_MAX_SECONDS, StreamBufferService

_OWNER = AgentIdentity.for_role(agent_id="human-owner", role="normal", is_human=True)
_OPERATIONS = (
    "enqueue",
    "drain",
    "list_items",
    "retract",
    "mark_pending",
    "mark_delivered",
    "mark_undelivered",
    "clear",
)


@pytest.fixture
def queue() -> InMemoryInterjectionQueue:
    return InMemoryInterjectionQueue()


def _error(exc: pytest.ExceptionInfo[ToukaException]) -> tuple[int, str]:
    return exc.value.status_code, exc.value.detail["code"]


def _rows(items: list) -> list[tuple]:
    return [(i.text, i.status, i.step, i.urgent) for i in items]


def test_in_memory_queue_implements_protocol(queue):
    for name in _OPERATIONS:
        assert callable(getattr(queue, name)), name
        assert hasattr(InterjectionQueue, name), name
    assert isinstance(queue, InterjectionQueue)


def test_limits_are_module_constants():
    assert queue_mod.INTERJECTION_MAX_PER_STREAM == 10
    assert queue_mod.INTERJECTION_MAX_CHARS == 4000
    assert queue_mod.INTERJECTION_TTL_SECONDS == 600


def test_drain_claims_queued_items_once_with_step(queue):
    first = queue.enqueue("s1", "st1", "first")
    second = queue.enqueue("s1", "st1", "second", urgent=True)
    assert (first.status, first.step, first.urgent) == ("queued", None, False)
    assert first.id != second.id

    drained = queue.drain("s1", "st1", step=3)
    assert [i.id for i in drained] == [first.id, second.id]
    assert _rows(drained) == [("first", "pending", 3, False), ("second", "pending", 3, True)]
    # 已取出的不會被第二次取出（不重複送達）
    assert queue.drain("s1", "st1", step=4) == []

    assert _rows(queue.mark_delivered("s1", "st1", [first.id])) == [
        ("first", "delivered", 3, False)
    ]
    assert _rows(queue.mark_undelivered("s1", "st1", [second.id])) == [
        ("second", "undelivered", 3, True)
    ]
    assert _rows(queue.list_items("s1", "st1")) == [
        ("first", "delivered", 3, False),
        ("second", "undelivered", 3, True),
    ]


def test_transitions_only_from_allowed_states(queue):
    item = queue.enqueue("s1", "st1", "text")
    assert queue.mark_delivered("s1", "st1", [item.id]) == []  # queued 不能直接 delivered
    assert _rows(queue.mark_pending("s1", "st1", [item.id], step=1)) == [
        ("text", "pending", 1, False)
    ]
    assert queue.mark_pending("s1", "st1", [item.id], step=2) == []  # 已是 pending
    assert _rows(queue.mark_delivered("s1", "st1", [item.id])) == [("text", "delivered", 1, False)]
    assert queue.mark_undelivered("s1", "st1", [item.id]) == []  # delivered 是終態
    assert queue.mark_pending("s1", "st1", ["no-such-id"], step=1) == []
    queued = queue.enqueue("s1", "st1", "never drained")
    assert _rows(queue.mark_undelivered("s1", "st1", [queued.id])) == [
        ("never drained", "undelivered", None, False)
    ]


def test_retract_only_while_queued(queue):
    first = queue.enqueue("s1", "st1", "x")
    second = queue.enqueue("s1", "st1", "y")
    retracted = queue.retract("s1", "st1", first.id)
    assert (retracted.id, retracted.status, retracted.text) == (first.id, "retracted", "x")
    assert [i.id for i in queue.list_items("s1", "st1")] == [second.id]
    # 收回的不會再被取出
    assert [i.id for i in queue.drain("s1", "st1", step=1)] == [second.id]

    with pytest.raises(ToukaException) as exc:
        queue.retract("s1", "st1", second.id)  # 已交給模型
    assert _error(exc) == (409, "INTERJECTION_NOT_RETRACTABLE")
    with pytest.raises(ToukaException) as exc:
        queue.retract("s1", "st1", first.id)  # 已收回
    assert _error(exc) == (404, "INTERJECTION_NOT_FOUND")


def test_per_stream_cap_and_retract_frees_a_slot(queue):
    items = [queue.enqueue("s1", "st1", f"m{n}") for n in range(10)]
    with pytest.raises(ToukaException) as exc:
        queue.enqueue("s1", "st1", "overflow")
    assert _error(exc) == (429, "INTERJECTION_QUEUE_FULL")
    assert len(queue.list_items("s1", "st1")) == 10
    # 上限各串流分開算
    assert queue.enqueue("s1", "st2", "other stream").status == "queued"
    assert queue.enqueue("s2", "st1", "other session").status == "queued"
    queue.retract("s1", "st1", items[0].id)
    assert queue.enqueue("s1", "st1", "after retract").status == "queued"


def test_delivered_items_still_count_toward_cap(queue):
    items = [queue.enqueue("s1", "st1", f"m{n}") for n in range(10)]
    queue.drain("s1", "st1", step=1)
    queue.mark_delivered("s1", "st1", [i.id for i in items])
    with pytest.raises(ToukaException) as exc:
        queue.enqueue("s1", "st1", "overflow")
    assert _error(exc) == (429, "INTERJECTION_QUEUE_FULL")


def test_text_length_and_blank_text(queue):
    assert queue.enqueue("s1", "st1", "字" * 4000).text == "字" * 4000
    with pytest.raises(ToukaException) as exc:
        queue.enqueue("s1", "st1", "字" * 4001)
    assert _error(exc) == (422, "INTERJECTION_TOO_LONG")
    with pytest.raises(ToukaException) as exc:
        queue.enqueue("s1", "st1", " \n\t ")
    assert _error(exc) == (422, "VALIDATION_ERROR")
    assert len(queue.list_items("s1", "st1")) == 1


def test_keys_isolate_sessions_and_streams(queue):
    item = queue.enqueue("s1", "old", "from the old stream")
    assert queue.drain("s1", "new", step=1) == []
    assert queue.list_items("s1", "new") == []
    assert queue.list_items("s2", "old") == []
    for session_id, stream_id in (("s1", "new"), ("s2", "old")):
        with pytest.raises(ToukaException) as exc:
            queue.retract(session_id, stream_id, item.id)
        assert _error(exc) == (404, "INTERJECTION_NOT_FOUND")
        assert queue.mark_pending(session_id, stream_id, [item.id], step=1) == []
        assert queue.mark_undelivered(session_id, stream_id, [item.id]) == []
    assert _rows(queue.list_items("s1", "old")) == [("from the old stream", "queued", None, False)]


def test_clear_one_stream_or_whole_session(queue):
    a = queue.enqueue("s1", "st1", "a")
    b = queue.enqueue("s1", "st2", "b")
    c = queue.enqueue("s2", "st1", "c")
    assert [i.id for i in queue.clear("s1", "st1")] == [a.id]
    assert queue.list_items("s1", "st1") == []
    assert [i.id for i in queue.list_items("s1", "st2")] == [b.id]
    assert [i.id for i in queue.clear("s1")] == [b.id]
    assert queue.list_items("s1", "st2") == []
    assert [i.id for i in queue.list_items("s2", "st1")] == [c.id]
    assert queue.clear("s1") == []


def test_ttl_expires_queued_items_and_purges_idle_queues(queue, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(queue_mod, "get_current_epoch", lambda: now[0])
    stale = queue.enqueue("s1", "st1", "stale")
    now[0] = 1300.0
    fresh = queue.enqueue("s1", "st1", "fresh")
    now[0] = 1600.0  # 剛好 600 秒：還沒過期
    assert [i.status for i in queue.list_items("s1", "st1")] == ["queued", "queued"]
    now[0] = 1601.0
    assert [(i.id, i.status) for i in queue.list_items("s1", "st1")] == [
        (stale.id, "undelivered"),
        (fresh.id, "queued"),
    ]
    # 過期的不會被交給模型
    assert [i.id for i in queue.drain("s1", "st1", step=2)] == [fresh.id]
    now[0] = 2300.0  # 閒置超過 TTL，但還有 pending：保留
    assert [i.status for i in queue.list_items("s1", "st1")] == ["undelivered", "pending"]
    queue.mark_delivered("s1", "st1", [fresh.id])
    now[0] = 2901.0  # 閒置 601 秒、沒有 pending，但有已送達的：留到串流結束（4-R1b）
    assert [i.status for i in queue.list_items("s1", "st1")] == ["undelivered", "delivered"]
    assert len([queue.enqueue("s1", "st1", f"m{n}") for n in range(8)]) == 8  # 上限不歸零
    with pytest.raises(ToukaException) as exc:
        queue.enqueue("s1", "st1", "m8")
    assert _error(exc) == (429, "INTERJECTION_QUEUE_FULL")


def test_idle_purge_only_clears_queues_without_delivered_or_pending(queue, monkeypatch):
    """4-R1b：已送達的留到串流結束 clear；只剩過期或未送達的照舊整串清掉。"""
    now = [1000.0]
    monkeypatch.setattr(queue_mod, "get_current_epoch", lambda: now[0])
    kept = queue.enqueue("s1", "st1", "送達")
    queue.mark_pending("s1", "st1", [kept.id], step=1)
    queue.mark_delivered("s1", "st1", [kept.id])
    queue.enqueue("s2", "st1", "沒人取")  # 只有 queued，過期後變 undelivered
    lost = queue.enqueue("s3", "st1", "送不到")
    queue.mark_undelivered("s3", "st1", [lost.id])
    now[0] = 1601.0
    assert _rows(queue.list_items("s1", "st1")) == [("送達", "delivered", 1, False)]
    assert queue.list_items("s2", "st1") == []
    assert queue.list_items("s3", "st1") == []


async def _ui_stream(buffer: StreamBufferService, session_id: str, *, persistent: bool = False):
    if persistent:
        admission = PersistedStreamAdmission(
            mode="persistent", revision="r1", user_parent_id="u", assistant_node_id="a"
        )
    else:
        admission = IncognitoStreamAdmission(
            mode="incognito", user_parent_id="u", assistant_node_id="a"
        )
    return await buffer.create(session_id, identity=_OWNER, admission=admission)


@pytest.mark.parametrize("ending", ["done", "error", "cancel", "replace", "retire", "orphan"])
async def test_stream_end_clears_its_interjections(queue, monkeypatch, ending):
    buffer = StreamBufferService(interjections=queue)
    entry = await _ui_stream(buffer, "sess", persistent=ending == "retire")
    queue.enqueue("other-session", "x", "kept")
    queue.enqueue(entry.session_id, entry.stream_id, "queued text")
    queue.enqueue(entry.session_id, entry.stream_id, "pending text")
    queue.drain(entry.session_id, entry.stream_id, step=1)

    if ending in ("done", "error"):
        await buffer.finish(entry.session_id, ending, stream_id=entry.stream_id)
    elif ending == "cancel":
        await buffer.cancel(entry.session_id, stream_id=entry.stream_id)
    elif ending == "replace":
        replacement = await _ui_stream(buffer, "sess")
        assert queue.list_items("sess", replacement.stream_id) == []
    elif ending == "retire":
        assert buffer.retire_persistent(entry.session_id, entry.stream_id) is True
    else:
        monkeypatch.setattr(
            buffer_mod,
            "get_current_epoch",
            lambda: entry.started_at + STREAM_ACTIVE_MAX_SECONDS + 1,
        )
        buffer.list_active()
        assert entry.status == "error"

    assert queue.list_items(entry.session_id, entry.stream_id) == []
    assert [i.text for i in queue.list_items("other-session", "x")] == ["kept"]


async def test_new_stream_clears_leftover_keys_of_the_session(queue):
    buffer = StreamBufferService(interjections=queue)
    queue.enqueue("sess", "ghost-stream", "left behind")
    queue.enqueue("other-session", "ghost-stream", "kept")
    entry = await _ui_stream(buffer, "sess")
    assert queue.list_items("sess", "ghost-stream") == []
    assert queue.list_items("sess", entry.stream_id) == []
    assert [i.text for i in queue.list_items("other-session", "ghost-stream")] == ["kept"]


async def test_rejected_stream_replacement_keeps_the_owners_queue(queue):
    buffer = StreamBufferService(interjections=queue)
    entry = await _ui_stream(buffer, "sess")
    queue.enqueue("sess", entry.stream_id, "owner text")
    stranger = AgentIdentity.for_role(agent_id="stranger", role="normal", is_human=True)
    with pytest.raises(ValueError, match="owner required"):
        await buffer.create(
            "sess",
            identity=stranger,
            admission=IncognitoStreamAdmission(
                mode="incognito", user_parent_id="u", assistant_node_id="a"
            ),
        )
    assert [i.text for i in queue.list_items("sess", entry.stream_id)] == ["owner text"]


async def test_default_buffer_clears_the_shared_queue():
    shared = queue_mod.interjection_queue
    buffer = StreamBufferService()
    entry = await _ui_stream(buffer, "sess-default-wiring-u4a1")
    shared.enqueue(entry.session_id, entry.stream_id, "text")
    await buffer.finish(entry.session_id, stream_id=entry.stream_id)
    assert shared.list_items(entry.session_id, entry.stream_id) == []
