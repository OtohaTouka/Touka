"""ADR-0081 游標續傳可恢復串流 — StreamBufferService 契約測試（先 Red 後 Green）。

緩衝服務＝進行中互動串流的 per-session SSOT：
- create 建 entry（同 session 已 active → 取消舊串流再建，防競態）
- append 依序累積 chunk（seq = index）
- subscribe(starting_after) 重播既有 chunk → Condition 跟流新 chunk → 終態收尾
- cancel 取消 producer task 並喚醒訂閱者
- TTL 清掃（完成後保留 STREAM_BUFFER_TTL_SECONDS）
- MAX_CHUNKS 上限保險（超限終結為 error）
"""

import asyncio
import json

import pytest
from src.models import session_persistence as contract
from src.models.identity import AgentIdentity
from src.services import stream_buffer_service as buffer_module
from src.services.stream_buffer_service import (
    STREAM_ACTIVE_MAX_SECONDS,
    STREAM_BUFFER_MAX_CHUNKS,
    STREAM_BUFFER_TTL_SECONDS,
    StreamBufferService,
    StreamOwnershipError,
)

OWNER = AgentIdentity.for_role(agent_id="synthetic-owner", role="normal", is_human=True)
OTHER = AgentIdentity.for_role(agent_id="synthetic-other", role="normal", is_human=True)


def _ui_admission(*, incognito=False):
    model = getattr(
        contract, "IncognitoStreamAdmission" if incognito else "PersistedStreamAdmission", None
    )
    assert model is not None, "explicit UI persistence admission is missing"
    fields = {
        "mode": "incognito" if incognito else "persistent",
        "user_parent_id": "user",
        "assistant_node_id": "assistant",
    }
    if not incognito:
        fields["revision"] = "synthetic-revision"
    return model(**fields)


def _events(entry):
    return [
        json.loads(chunk.removeprefix("data: "))
        for chunk in entry.chunks
        if chunk.startswith("data: {")
    ]


@pytest.mark.parametrize("replacement", ["other", "agent_same_id", "legacy"])
@pytest.mark.asyncio
async def test_ui_replacement_cannot_cancel_another_principal(svc, replacement):
    original = await svc.create("private", identity=OWNER, admission=_ui_admission(incognito=True))
    producer = asyncio.create_task(asyncio.Event().wait())
    svc.attach_task("private", producer, stream_id=original.stream_id)
    before = list(original.chunks)
    options = (
        {}
        if replacement == "legacy"
        else {
            "identity": OTHER if replacement == "other" else AgentIdentity(agent_id=OWNER.agent_id),
            "admission": _ui_admission(incognito=True),
        }
    )
    try:
        with pytest.raises(StreamOwnershipError):
            await svc.create("private", **options)
        assert svc._streams["private"] is original
        assert original.chunks == before and original.terminal is None
        assert original.status == "active" and not producer.cancelling()
        assert svc.is_owned_by("private", OWNER)
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


@pytest.mark.asyncio
async def test_ui_terminal_and_status_are_atomic_and_first_terminal_wins(svc):
    entry = await svc.create("ui", identity=OWNER, admission=_ui_admission())
    saved = contract.SavedResult(status="saved", representation="text", revision="next")
    await svc.finish("ui", "done", stream_id=entry.stream_id, persistence=saved)
    await svc.finish("ui", "error", stream_id=entry.stream_id)
    state = svc.get_state("ui")
    events = _events(entry)
    assert state["status"] == "done" and events[0]["type"] == "touka_stream_started"
    assert len(events) == 2 and events[1] == state["terminal"]
    assert events[1]["generation"] == "completed" and events[1]["persistence"]["revision"] == "next"
    assert events[0]["assistant_node_id"] == events[1]["assistant_node_id"] == "assistant"


@pytest.mark.asyncio
async def test_ui_old_or_missing_fence_cannot_append_finish_or_attach_replacement(svc):
    first = await svc.create("ui", identity=OWNER, admission=_ui_admission())
    second = await svc.create("ui", identity=OWNER, admission=_ui_admission())
    task = asyncio.create_task(asyncio.Event().wait())
    svc.attach_task("ui", task, stream_id=first.stream_id)
    await asyncio.sleep(0)
    assert task.cancelled() and second.task is None
    for fence in (None, first.stream_id):
        await svc.append("ui", "data: bad\n\n", stream_id=fence)
        await svc.finish("ui", "done", stream_id=fence)
    assert second.status == "active" and len(second.chunks) == 1
    assert first.status == "cancelled" and _events(first)[-1]["generation"] == "cancelled"
    assert [chunk async for chunk in svc.subscribe("ui", stream_id=first.stream_id)] == []
    await svc.cancel("ui", stream_id=second.stream_id)


@pytest.mark.asyncio
async def test_ui_concurrent_create_leaves_one_active_generation(svc):
    entries = await asyncio.gather(
        *[svc.create("ui", identity=OWNER, admission=_ui_admission()) for _ in range(3)]
    )
    assert sum(entry.status == "active" for entry in entries) == 1
    assert len({entry.stream_id for entry in entries}) == 3
    for entry in entries[:-1]:
        assert _events(entry)[-1]["generation"] == "cancelled"
    assert svc.get_state("ui")["stream_id"] == entries[-1].stream_id
    await svc.cancel("ui", stream_id=entries[-1].stream_id)


@pytest.mark.asyncio
async def test_ui_orphan_sync_sweep_emits_terminal_and_wakes_two_waiters(svc, monkeypatch):
    entry = await svc.create("ui", identity=OWNER, admission=_ui_admission())
    readied = [asyncio.Event(), asyncio.Event()]

    async def reader(ready):
        output = []
        async for chunk in svc.subscribe("ui", stream_id=entry.stream_id):
            output.append(chunk)
            ready.set()
        return output

    readers = [asyncio.create_task(reader(ready)) for ready in readied]
    await asyncio.gather(*(ready.wait() for ready in readied))
    monkeypatch.setattr(
        buffer_module, "get_current_epoch", lambda: entry.started_at + STREAM_ACTIVE_MAX_SECONDS + 1
    )
    state = svc.get_state("ui")
    assert state["status"] == "error" and state["terminal"]["generation"] == "error"
    outputs = await asyncio.wait_for(asyncio.gather(*readers), timeout=1)
    assert outputs[0] == outputs[1] == entry.chunks
    assert len(_events(entry)) == 2


@pytest.mark.asyncio
async def test_ui_capacity_reserves_terminal_and_cancels_producer(svc, monkeypatch):
    monkeypatch.setattr(buffer_module, "STREAM_BUFFER_MAX_CHUNKS", 3)
    entry = await svc.create("ui", identity=OWNER, admission=_ui_admission())
    task = asyncio.create_task(asyncio.Event().wait())
    svc.attach_task("ui", task, stream_id=entry.stream_id)
    await svc.append("ui", "data: partial\n\n", stream_id=entry.stream_id)
    await svc.append("ui", "data: overflow\n\n", stream_id=entry.stream_id)
    await asyncio.sleep(0)
    assert task.cancelled() and len(entry.chunks) == 3
    assert _events(entry)[-1]["generation"] == "error"
    assert _events(entry)[-1]["persistence"]["status"] == "unknown"


@pytest.mark.parametrize("incognito", [False, True])
@pytest.mark.asyncio
async def test_ui_entry_is_principal_owned_and_cancel_is_not_proof_of_no_commit(svc, incognito):
    entry = await svc.create("ui", identity=OWNER, admission=_ui_admission(incognito=incognito))
    assert svc.is_owned_by("ui", OWNER) is True
    assert svc.is_owned_by("ui", OTHER) is False
    await svc.cancel("ui", stream_id=entry.stream_id)
    result = _events(entry)[-1]["persistence"]
    assert result == (
        {"status": "skipped", "reason": "incognito"}
        if incognito
        else {
            "status": "unknown",
            "error_code": result["error_code"],
        }
    )


@pytest.fixture
def svc() -> StreamBufferService:
    return StreamBufferService()


async def _collect(gen, n: int | None = None) -> list[str]:
    """收集訂閱者輸出（n=None 收到終止為止）。"""
    out: list[str] = []
    async for chunk in gen:
        out.append(chunk)
        if n is not None and len(out) >= n:
            break
    return out


@pytest.mark.asyncio
async def test_create_returns_active_entry_with_stream_id(svc):
    entry = await svc.create("sess-1")
    assert entry.session_id == "sess-1"
    assert entry.status == "active"
    assert isinstance(entry.stream_id, str) and len(entry.stream_id) > 8

    state = svc.get_state("sess-1")
    assert state is not None
    assert state["status"] == "active"
    assert state["last_seq"] == -1


@pytest.mark.asyncio
async def test_append_and_replay_from_start(svc):
    await svc.create("sess-1")
    await svc.append("sess-1", "data: a\n\n")
    await svc.append("sess-1", "data: b\n\n")
    await svc.finish("sess-1", "done")

    out = await _collect(svc.subscribe("sess-1", starting_after=-1))
    assert out == ["data: a\n\n", "data: b\n\n"]
    assert svc.get_state("sess-1")["last_seq"] == 1


@pytest.mark.asyncio
async def test_subscribe_starting_after_slices(svc):
    await svc.create("sess-1")
    for i in range(4):
        await svc.append("sess-1", f"data: {i}\n\n")
    await svc.finish("sess-1", "done")

    out = await _collect(svc.subscribe("sess-1", starting_after=1))
    assert out == ["data: 2\n\n", "data: 3\n\n"]


@pytest.mark.asyncio
async def test_subscribe_follows_live_appends(svc):
    """訂閱者先就位，之後的 append 仍會逐一收到（Condition 跟流）。"""
    await svc.create("sess-1")
    await svc.append("sess-1", "data: first\n\n")

    collected: list[str] = []

    async def reader():
        async for chunk in svc.subscribe("sess-1", starting_after=-1):
            collected.append(chunk)

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.05)  # 讓訂閱者重播並進入等待
    await svc.append("sess-1", "data: second\n\n")
    await asyncio.sleep(0.05)
    await svc.finish("sess-1", "done")
    await asyncio.wait_for(task, timeout=2)

    assert collected == ["data: first\n\n", "data: second\n\n"]


@pytest.mark.asyncio
async def test_two_subscribers_receive_identical_sequences(svc):
    await svc.create("sess-1")
    await svc.append("sess-1", "data: x\n\n")

    a: list[str] = []
    b: list[str] = []

    async def reader(sink):
        async for chunk in svc.subscribe("sess-1", starting_after=-1):
            sink.append(chunk)

    t1 = asyncio.create_task(reader(a))
    t2 = asyncio.create_task(reader(b))
    await asyncio.sleep(0.05)
    await svc.append("sess-1", "data: y\n\n")
    await svc.finish("sess-1", "done")
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2)

    assert a == b == ["data: x\n\n", "data: y\n\n"]


@pytest.mark.asyncio
async def test_cancel_marks_cancelled_cancels_task_and_wakes_subscribers(svc):
    await svc.create("sess-1")

    async def fake_producer():
        await asyncio.sleep(60)

    task = asyncio.create_task(fake_producer())
    svc.attach_task("sess-1", task)

    done = asyncio.Event()

    async def reader():
        async for _chunk in svc.subscribe("sess-1", starting_after=-1):
            pass
        done.set()

    rtask = asyncio.create_task(reader())
    await asyncio.sleep(0.05)

    await svc.cancel("sess-1")
    await asyncio.wait_for(done.wait(), timeout=2)
    await asyncio.sleep(0.05)

    assert task.cancelled()
    state = svc.get_state("sess-1")
    assert state["status"] == "cancelled"
    rtask.cancel()


@pytest.mark.asyncio
async def test_create_replaces_existing_active_stream(svc):
    """同 session 開新串流：舊串流取消（含 producer task）、新 entry 取代。"""
    old = await svc.create("sess-1")

    async def fake_producer():
        await asyncio.sleep(60)

    old_task = asyncio.create_task(fake_producer())
    svc.attach_task("sess-1", old_task)

    new = await svc.create("sess-1")
    await asyncio.sleep(0.05)

    assert new.stream_id != old.stream_id
    assert old.status == "cancelled"
    assert old_task.cancelled()
    assert svc.get_state("sess-1")["stream_id"] == new.stream_id


@pytest.mark.asyncio
async def test_ttl_sweep_removes_expired_finished_entries(svc, monkeypatch):
    """完成超過 TTL 的 entry 在下次操作時被清掃。"""
    now = 1_000_000.0
    monkeypatch.setattr("src.services.stream_buffer_service.get_current_epoch", lambda: now)
    await svc.create("sess-1")
    await svc.finish("sess-1", "done")
    assert svc.get_state("sess-1") is not None

    # 時間前進超過 TTL → 清掃
    monkeypatch.setattr(
        "src.services.stream_buffer_service.get_current_epoch",
        lambda: now + STREAM_BUFFER_TTL_SECONDS + 1,
    )
    assert svc.get_state("sess-1") is None
    assert svc.list_active() == []


@pytest.mark.asyncio
async def test_list_active_only_returns_active(svc):
    await svc.create("sess-a")
    await svc.create("sess-b")
    await svc.finish("sess-b", "done")

    active = svc.list_active()
    assert [s["session_id"] for s in active] == ["sess-a"]
    assert active[0]["status"] == "active"


@pytest.mark.asyncio
async def test_append_beyond_max_chunks_finishes_as_error(svc, monkeypatch):
    """超過 MAX_CHUNKS 上限：終結為 error（記憶體保險），不再累積。"""
    monkeypatch.setattr("src.services.stream_buffer_service.STREAM_BUFFER_MAX_CHUNKS", 3)
    await svc.create("sess-1")
    for i in range(5):
        await svc.append("sess-1", f"data: {i}\n\n")

    state = svc.get_state("sess-1")
    assert state["status"] == "error"
    assert state["last_seq"] <= 2
    assert STREAM_BUFFER_MAX_CHUNKS > 1000  # 真實上限必須遠大於測試替身值


@pytest.mark.asyncio
async def test_append_to_finished_stream_is_noop(svc):
    await svc.create("sess-1")
    await svc.finish("sess-1", "done")
    await svc.append("sess-1", "data: late\n\n")
    assert svc.get_state("sess-1")["last_seq"] == -1


# --------------------------------------------------------------------------- #
# 批⑤-E E43（ADR-0081）：孤兒 active 串流收割（_sweep elif 分支從不被觸及）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sweep_reaps_orphan_active_stream(svc, monkeypatch):
    """active 超過 STREAM_ACTIVE_MAX_SECONDS 未 finish → 收割：cancel producer＋標 error。

    變異 :201 `elif now - entry.started_at > ...` → `elif False:` → 孤兒永不收割 → 紅。
    """
    now = 1_000_000.0
    monkeypatch.setattr("src.services.stream_buffer_service.get_current_epoch", lambda: now)
    entry = await svc.create("sess-orphan")

    async def _hang() -> None:
        await asyncio.sleep(3600)

    producer = asyncio.create_task(_hang())
    entry.task = producer

    # 推時鐘超過 active 上限、刻意不 finish（模擬 handler 卡死）
    monkeypatch.setattr(
        "src.services.stream_buffer_service.get_current_epoch",
        lambda: now + STREAM_ACTIVE_MAX_SECONDS + 1,
    )
    await svc.create("sess-trigger")  # 公開路徑：create 進場先 _sweep

    assert entry.status == "error"
    assert entry.finished_at is not None
    await asyncio.sleep(0)  # 讓 cancellation 傳遞
    assert producer.cancelled()
