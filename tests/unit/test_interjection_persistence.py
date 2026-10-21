"""插話存檔（ADR-0249 第 4a 段 U4a-3）：伺服器把這個串流已送達的插話存進助理節點 interjections[]。

- 來源是插話佇列 ``list_items(session_id, stream_id)`` 裡 status=delivered 的項目，形狀同 API
  ``Interjection``（id、text、urgent、status、step、created_at），和前端寫進節點的一致。
- 沒有已送達的插話時節點不多出欄位；停止、錯誤、無痕都不存。
- 存檔在 StreamBufferService 清空佇列（串流結束）之前完成。
"""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock

import pytest
from src.models.identity import AgentIdentity
from src.models.session_persistence import (
    IncognitoStreamAdmission,
    PersistedStreamAdmission,
    SavedResult,
    UiGenerationStopped,
)
from src.services import dispatcher as dispatcher_mod
from src.services import interjection_queue as queue_mod
from src.services.interjection_queue import InMemoryInterjectionQueue
from src.services.stream_buffer_service import StreamBufferService
from src.utils.async_db import init_db_async, session_service

UI_OWNER = AgentIdentity.for_role(agent_id="synthetic-owner", role="normal", is_human=True)
UI_TREE = {
    "messages": {"user": {"id": "user", "role": "user", "content": "hi", "parentId": None}},
    "currentId": "user",
}
TEXT_CHUNK = 'data: {"choices":[{"delta":{"content":"回答"}}]}\n\n'


@pytest.fixture
async def sandbox_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("src.utils.async_db.get_db_path", lambda: db_path)
    await init_db_async()
    return db_path


@pytest.fixture
def queue(monkeypatch) -> InMemoryInterjectionQueue:
    monkeypatch.setattr(queue_mod, "get_current_epoch", lambda: 1000.0)
    svc = InMemoryInterjectionQueue()
    monkeypatch.setattr(dispatcher_mod, "interjection_queue", svc)
    return svc


@pytest.fixture
def buffer(monkeypatch, queue) -> StreamBufferService:
    svc = StreamBufferService(interjections=queue)
    monkeypatch.setattr(dispatcher_mod, "stream_buffer_service", svc)
    monkeypatch.setattr(dispatcher_mod, "_maybe_trim_after_chat", AsyncMock())
    return svc


async def _seed(sid: str) -> PersistedStreamAdmission:
    assert await session_service.upsert_session(
        sid, "UI", copy.deepcopy(UI_TREE), owner_user_id=UI_OWNER.agent_id
    )
    snapshot = await session_service.read_persistence_snapshot(sid, UI_OWNER)
    return PersistedStreamAdmission(
        mode="persistent",
        revision=snapshot.revision,
        user_parent_id="user",
        assistant_node_id="assistant",
    )


async def _saved_node(sid: str) -> dict:
    snapshot = await session_service.read_persistence_snapshot(sid, UI_OWNER)
    return snapshot.session["messages"]["messages"]["assistant"]


def _deliver(queue, sid: str, stream_id: str, text: str, step: int, *, urgent: bool = False):
    item = queue.enqueue(sid, stream_id, text, urgent=urgent)
    queue.mark_pending(sid, stream_id, [item.id], step=step)
    queue.mark_delivered(sid, stream_id, [item.id])
    return item


async def _persist(sid: str, admission, queue, stream_id: str = "st"):
    snapshot = await session_service.read_persistence_snapshot(sid, UI_OWNER)
    return await dispatcher_mod._persist_ui_assistant_turn(
        sid, "回答", "m", UI_OWNER, snapshot, admission, stream_id=stream_id, interjections=queue
    )


async def test_delivered_interjections_are_saved_on_the_assistant_node(sandbox_db, queue):
    admission = await _seed("s1")
    second = _deliver(queue, "s1", "st", "第二則", 4, urgent=True)
    first = _deliver(queue, "s1", "st", "只看 2026 年", 2)
    queue.enqueue("s1", "st", "還在排隊")
    pending = queue.enqueue("s1", "st", "交出但還沒確認")
    queue.mark_pending("s1", "st", [pending.id], step=5)
    lost = queue.enqueue("s1", "st", "沒送到")
    queue.mark_undelivered("s1", "st", [lost.id])
    retracted = queue.enqueue("s1", "st", "收回了")
    queue.retract("s1", "st", retracted.id)
    _deliver(queue, "s1", "other-stream", "別的串流", 1)

    result = await _persist("s1", admission, queue)

    assert isinstance(result, SavedResult)
    node = await _saved_node("s1")
    assert node["content"] == "回答"
    assert node["interjections"] == [
        {
            "id": second.id,
            "text": "第二則",
            "urgent": True,
            "status": "delivered",
            "step": 4,
            "created_at": 1000.0,
        },
        {
            "id": first.id,
            "text": "只看 2026 年",
            "urgent": False,
            "status": "delivered",
            "step": 2,
            "created_at": 1000.0,
        },
    ]


@pytest.mark.parametrize("state", ["none", "queued", "pending", "undelivered", "retracted"])
async def test_no_delivered_interjection_leaves_the_node_unchanged(sandbox_db, queue, state):
    admission = await _seed("s1")
    if state != "none":
        item = queue.enqueue("s1", "st", "text")
        if state == "pending":
            queue.mark_pending("s1", "st", [item.id], step=1)
        elif state == "undelivered":
            queue.mark_undelivered("s1", "st", [item.id])
        elif state == "retracted":
            queue.retract("s1", "st", item.id)

    assert isinstance(await _persist("s1", admission, queue), SavedResult)

    node = await _saved_node("s1")
    assert "interjections" not in node
    assert set(node) == {"id", "role", "content", "parentId", "timestamp", "name"}


async def test_pump_saves_before_the_stream_end_clears_the_queue(sandbox_db, buffer, queue):
    admission = await _seed("s1")
    entry = await buffer.create("s1", identity=UI_OWNER, admission=admission)
    item = _deliver(queue, "s1", entry.stream_id, "補充", 3)

    async def generation():
        yield TEXT_CHUNK

    await dispatcher_mod._pump_ui_stream(
        generation(), "s1", entry.stream_id, "m", UI_OWNER, admission
    )

    assert entry.status == "done"
    assert entry.terminal.persistence.status == "saved"
    assert [i["id"] for i in (await _saved_node("s1"))["interjections"]] == [item.id]
    assert queue.list_items("s1", entry.stream_id) == []  # 存完才由串流結束清空


@pytest.mark.parametrize("ending", ["cancelled", "error"])
async def test_stopped_or_failed_generation_saves_nothing(
    sandbox_db, buffer, queue, monkeypatch, ending
):
    admission = await _seed("s1")
    entry = await buffer.create("s1", identity=UI_OWNER, admission=admission)
    _deliver(queue, "s1", entry.stream_id, "補充", 1)
    save = AsyncMock()
    monkeypatch.setattr(session_service, "conditional_save_session", save)

    async def generation():
        yield TEXT_CHUNK
        raise UiGenerationStopped(ending)

    await dispatcher_mod._pump_ui_stream(
        generation(), "s1", entry.stream_id, "m", UI_OWNER, admission
    )

    save.assert_not_awaited()
    assert entry.status == ending
    assert queue.list_items("s1", entry.stream_id) == []


async def test_incognito_saves_nothing(buffer, queue, monkeypatch):
    admission = IncognitoStreamAdmission(
        mode="incognito", user_parent_id="user", assistant_node_id="assistant"
    )
    entry = await buffer.create("inc", identity=UI_OWNER, admission=admission)
    _deliver(queue, "inc", entry.stream_id, "補充", 1)
    database = AsyncMock(side_effect=AssertionError("incognito must not touch the database"))
    for name in ("conditional_save_session", "read_persistence_snapshot", "upsert_session"):
        monkeypatch.setattr(session_service, name, database)

    async def generation():
        yield TEXT_CHUNK

    await dispatcher_mod._pump_ui_stream(
        generation(), "inc", entry.stream_id, "m", UI_OWNER, admission
    )

    database.assert_not_awaited()
    assert entry.terminal.persistence.status == "skipped"
    assert queue.list_items("inc", entry.stream_id) == []


async def test_delivered_interjections_survive_idle_time_before_the_save(sandbox_db, monkeypatch):
    """4-R1b：最後一則送達後 AI 又跑超過 TTL 才結束，存檔時已送達的還在（閒置清除不清）。"""
    now = [1000.0]
    monkeypatch.setattr(queue_mod, "get_current_epoch", lambda: now[0])
    queue = InMemoryInterjectionQueue()
    admission = await _seed("s-idle")
    item = _deliver(queue, "s-idle", "st", "只看 2026 年", 2)
    now[0] = 1601.0  # 閒置 601 秒，串流還在
    assert isinstance(await _persist("s-idle", admission, queue), SavedResult)
    node = await _saved_node("s-idle")
    assert [(i["id"], i["status"], i["step"]) for i in node["interjections"]] == [
        (item.id, "delivered", 2)]
