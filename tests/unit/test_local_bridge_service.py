"""本機小幫手通道（ADR-0311）：佇列、期限、兩段確認、換配對。

全部用假時鐘與假小幫手，不連任何東西。
"""

from __future__ import annotations

import asyncio

import pytest
from src.models.local_bridge import LocalJobResult
from src.services import local_bridge as module
from src.services.local_bridge import LocalBridge, host_mount_roots

GENERATION = 1
OK = LocalJobResult(status="ok", data={"done": True})
REFUSED = LocalJobResult(status="refused", code="rejected")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def bridge(clock: Clock) -> LocalBridge:
    return LocalBridge(clock=clock, check_interval=0.001)


async def _poll(bridge: LocalBridge, generation: int = GENERATION):
    return await bridge.poll(generation, "1.0", hold=0)


async def _start(bridge: LocalBridge, action: str = "read") -> asyncio.Task:
    task = asyncio.create_task(bridge.submit(action, {"path": "a"}, "Soul"))
    await asyncio.sleep(0)
    return task


async def _finish(task: asyncio.Task) -> LocalJobResult:
    return await asyncio.wait_for(task, 2)


async def _advance_online(bridge: LocalBridge, clock: Clock, seconds: float) -> None:
    """往前走時間，途中持續 poll，小幫手算在線。"""
    step = module.ONLINE_WINDOW_SECONDS / 2
    remaining = seconds
    while remaining > 0:
        clock.now += min(step, remaining)
        remaining -= step
        await _poll(bridge)
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_offline_submit_is_refused_and_queues_nothing(bridge: LocalBridge) -> None:
    result = await bridge.submit("read", {"path": "a"}, "Soul")
    assert (result.status, result.code) == ("error", module.CODE_OFFLINE)
    response = await _poll(bridge)
    assert response is not None and response.job is None


@pytest.mark.asyncio
async def test_job_round_trip(bridge: LocalBridge) -> None:
    await _poll(bridge)
    task = await _start(bridge)
    response = await _poll(bridge)
    assert response is not None and response.job is not None
    assert response.job.action == "read"
    assert response.job.args == {"path": "a"}
    assert response.job.requester == "Soul"
    assert bridge.deliver(response.job.id, GENERATION, OK) is True
    assert await _finish(task) == OK


@pytest.mark.asyncio
async def test_a_job_is_handed_out_only_once(bridge: LocalBridge) -> None:
    await _poll(bridge)
    task = await _start(bridge)
    first = await _poll(bridge)
    second = await _poll(bridge)
    assert first is not None and first.job is not None
    assert second is not None and second.job is None
    bridge.deliver(first.job.id, GENERATION, OK)
    await _finish(task)


@pytest.mark.asyncio
async def test_queue_limit(bridge: LocalBridge) -> None:
    await _poll(bridge)
    tasks = [await _start(bridge) for _ in range(module.MAX_PENDING_JOBS)]
    result = await bridge.submit("read", {"path": "a"}, "Soul")
    assert (result.status, result.code) == ("error", module.CODE_BUSY)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_read_times_out(bridge: LocalBridge, clock: Clock) -> None:
    await _poll(bridge)
    task = await _start(bridge)
    await _advance_online(bridge, clock, module.READ_WAIT_SECONDS + 1)
    result = await _finish(task)
    assert (result.status, result.code) == ("error", module.CODE_TIMED_OUT)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create_file", "create_folder"])
async def test_a_create_that_was_handed_over_is_unknown_on_timeout(
    bridge: LocalBridge, clock: Clock, action: str
) -> None:
    await _poll(bridge)
    task = await _start(bridge, action)
    await _poll(bridge)
    await _advance_online(bridge, clock, module.READ_WAIT_SECONDS + 1)
    result = await _finish(task)
    assert (result.status, result.code) == ("error", module.CODE_RESULT_UNKNOWN)


@pytest.mark.asyncio
async def test_a_create_that_was_handed_over_is_unknown_when_the_helper_vanishes(
    bridge: LocalBridge, clock: Clock
) -> None:
    await _poll(bridge)
    task = await _start(bridge, "create_file")
    await _poll(bridge)
    clock.now += module.ONLINE_WINDOW_SECONDS + 1
    result = await _finish(task)
    assert (result.status, result.code) == ("error", module.CODE_RESULT_UNKNOWN)


@pytest.mark.asyncio
async def test_a_create_never_handed_over_times_out_plainly(
    bridge: LocalBridge, clock: Clock
) -> None:
    await _poll(bridge)
    task = await _start(bridge, "create_file")
    await asyncio.sleep(0)
    bridge._queue.clear()
    await _advance_online(bridge, clock, module.READ_WAIT_SECONDS + 1)
    result = await _finish(task)
    assert (result.status, result.code) == ("error", module.CODE_TIMED_OUT)


@pytest.mark.asyncio
async def test_helper_decides_before_touka_stops_waiting(bridge: LocalBridge) -> None:
    await _poll(bridge)
    task = await _start(bridge, "propose")
    response = await _poll(bridge)
    assert response is not None and response.job is not None
    assert response.job.decide_within == pytest.approx(
        module.DECISION_WAIT_SECONDS - module.HELPER_DECISION_MARGIN_SECONDS
    )
    assert response.job.decide_within < module.DECISION_WAIT_SECONDS
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_propose_without_decision_changes_nothing(bridge: LocalBridge, clock: Clock) -> None:
    await _poll(bridge)
    task = await _start(bridge, "propose")
    await _poll(bridge)
    await _advance_online(bridge, clock, module.DECISION_WAIT_SECONDS + 1)
    result = await _finish(task)
    assert (result.status, result.code) == ("error", module.CODE_NO_DECISION)


@pytest.mark.asyncio
async def test_confirm_switches_to_execution_deadline(bridge: LocalBridge, clock: Clock) -> None:
    await _poll(bridge)
    task = await _start(bridge, "propose")
    response = await _poll(bridge)
    assert response is not None and response.job is not None
    await _advance_online(bridge, clock, module.DECISION_WAIT_SECONDS - 10)
    assert bridge.confirm(response.job.id, GENERATION) is True
    await _advance_online(bridge, clock, module.EXECUTION_WAIT_SECONDS - 20)
    assert not task.done()
    assert bridge.deliver(response.job.id, GENERATION, OK) is True
    assert await _finish(task) == OK


@pytest.mark.asyncio
async def test_touka_waits_longer_than_the_helper_executes() -> None:
    assert module.EXECUTION_WAIT_SECONDS > module.HELPER_EXECUTION_LIMIT_SECONDS


@pytest.mark.asyncio
async def test_timeout_after_confirm_is_unknown(bridge: LocalBridge, clock: Clock) -> None:
    await _poll(bridge)
    task = await _start(bridge, "propose")
    response = await _poll(bridge)
    assert response is not None and response.job is not None
    bridge.confirm(response.job.id, GENERATION)
    await _advance_online(bridge, clock, module.EXECUTION_WAIT_SECONDS + 1)
    result = await _finish(task)
    assert (result.status, result.code) == ("error", module.CODE_RESULT_UNKNOWN)


@pytest.mark.asyncio
async def test_confirm_refused_cases(bridge: LocalBridge, clock: Clock) -> None:
    await _poll(bridge)
    assert bridge.confirm("unknown", GENERATION) is False

    read = await _start(bridge, "read")
    read_job = (await _poll(bridge)).job
    assert bridge.confirm(read_job.id, GENERATION) is False
    bridge.deliver(read_job.id, GENERATION, OK)
    await _finish(read)

    propose = await _start(bridge, "propose")
    job = (await _poll(bridge)).job
    assert bridge.confirm(job.id, GENERATION + 1) is False
    assert bridge.confirm(job.id, GENERATION) is True
    assert bridge.confirm(job.id, GENERATION) is False
    bridge.deliver(job.id, GENERATION, OK)
    await _finish(propose)

    expired = await _start(bridge, "propose")
    expired_job = (await _poll(bridge)).job
    clock.now += module.DECISION_WAIT_SECONDS + 1
    assert bridge.confirm(expired_job.id, GENERATION) is False
    await _poll(bridge)
    await _finish(expired)


@pytest.mark.asyncio
async def test_confirm_refused_for_a_job_not_handed_out(
    bridge: LocalBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _size: "known-id")
    await _poll(bridge)
    task = await _start(bridge, "propose")
    assert bridge.confirm("known-id", GENERATION) is False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_deliver_refused_cases(bridge: LocalBridge) -> None:
    await _poll(bridge)
    assert bridge.deliver("unknown", GENERATION, OK) is False

    task = await _start(bridge)
    job = (await _poll(bridge)).job
    assert bridge.deliver(job.id, GENERATION + 1, OK) is False
    assert bridge.deliver(job.id, GENERATION, OK) is True
    assert bridge.deliver(job.id, GENERATION, OK) is False
    await _finish(task)


@pytest.mark.asyncio
async def test_propose_success_requires_confirm(bridge: LocalBridge) -> None:
    await _poll(bridge)
    task = await _start(bridge, "propose")
    job = (await _poll(bridge)).job
    assert bridge.deliver(job.id, GENERATION, OK) is False
    assert bridge.deliver(job.id, GENERATION, REFUSED) is True
    assert await _finish(task) == REFUSED


@pytest.mark.asyncio
async def test_cancelled_job_reaches_helper_and_cannot_be_confirmed(bridge: LocalBridge) -> None:
    await _poll(bridge)
    task = await _start(bridge, "propose")
    job = (await _poll(bridge)).job
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    response = await _poll(bridge)
    assert response is not None and response.cancelled == [job.id]
    assert bridge.confirm(job.id, GENERATION) is False
    assert (await _poll(bridge)).cancelled == []


@pytest.mark.asyncio
async def test_cancel_after_confirm_still_tells_helper(bridge: LocalBridge) -> None:
    await _poll(bridge)
    task = await _start(bridge, "propose")
    job = (await _poll(bridge)).job
    bridge.confirm(job.id, GENERATION)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert (await _poll(bridge)).cancelled == [job.id]


@pytest.mark.asyncio
async def test_new_generation_voids_waiting_poll_and_jobs(bridge: LocalBridge) -> None:
    await _poll(bridge)
    task = await _start(bridge, "propose")
    job = (await _poll(bridge)).job
    waiting = asyncio.create_task(bridge.poll(GENERATION, "1.0", hold=5))
    await asyncio.sleep(0)

    bridge.invalidate(GENERATION + 1)

    assert await asyncio.wait_for(waiting, 2) is None
    result = await _finish(task)
    assert (result.status, result.code) == ("error", module.CODE_PAIRING_CHANGED)
    assert bridge.online is False
    assert bridge.confirm(job.id, GENERATION + 1) is False


@pytest.mark.asyncio
async def test_new_generation_during_a_confirmed_list_is_unknown(bridge: LocalBridge) -> None:
    await _poll(bridge)
    task = await _start(bridge, "propose")
    job = (await _poll(bridge)).job
    assert bridge.confirm(job.id, GENERATION) is True

    bridge.invalidate(GENERATION + 1)

    result = await _finish(task)
    assert (result.status, result.code) == ("error", module.CODE_RESULT_UNKNOWN)


@pytest.mark.asyncio
async def test_an_older_generation_cannot_be_written_back(bridge: LocalBridge) -> None:
    await _poll(bridge)
    bridge.invalidate(GENERATION + 2)
    bridge.invalidate(GENERATION + 1)
    assert await _poll(bridge, GENERATION + 2) is not None
    assert await _poll(bridge, GENERATION + 1) is None


@pytest.mark.asyncio
async def test_a_very_long_requester_does_not_break_polling(bridge: LocalBridge) -> None:
    await _poll(bridge)
    task = asyncio.create_task(bridge.submit("read", {"path": "a"}, "x" * 500))
    await asyncio.sleep(0)
    response = await _poll(bridge)
    assert response is not None and response.job is not None
    assert len(response.job.requester) == module.MAX_REQUESTER_CHARS
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_new_generation_keeps_new_jobs_from_old_poll(bridge: LocalBridge) -> None:
    await _poll(bridge)
    waiting = asyncio.create_task(bridge.poll(GENERATION, "1.0", hold=5))
    await asyncio.sleep(0)
    bridge.invalidate(GENERATION + 1)
    await _poll(bridge, GENERATION + 1)
    task = await _start(bridge)
    assert await asyncio.wait_for(waiting, 2) is None
    response = await _poll(bridge, GENERATION + 1)
    assert response is not None and response.job is not None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_old_generation_poll_starts_void(bridge: LocalBridge) -> None:
    await _poll(bridge, GENERATION + 1)
    assert await bridge.poll(GENERATION, "1.0", hold=0) is None


@pytest.mark.asyncio
async def test_later_poll_replaces_earlier_one(bridge: LocalBridge) -> None:
    first = asyncio.create_task(bridge.poll(GENERATION, "1.0", hold=5))
    await asyncio.sleep(0)
    second = asyncio.create_task(bridge.poll(GENERATION, "1.0", hold=0.05))
    await asyncio.sleep(0)
    earlier = await asyncio.wait_for(first, 2)
    assert earlier is not None and earlier.job is None
    await asyncio.wait_for(second, 2)


@pytest.mark.asyncio
async def test_poll_returns_when_a_job_arrives(bridge: LocalBridge) -> None:
    await _poll(bridge)
    waiting = asyncio.create_task(bridge.poll(GENERATION, "1.0", hold=5))
    await asyncio.sleep(0)
    task = await _start(bridge)
    response = await asyncio.wait_for(waiting, 2)
    assert response is not None and response.job is not None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_online_window(bridge: LocalBridge, clock: Clock) -> None:
    assert bridge.online is False
    await _poll(bridge)
    assert bridge.online is True
    assert bridge.helper_version == "1.0"
    clock.now += module.ONLINE_WINDOW_SECONDS + 1
    assert bridge.online is False


@pytest.mark.asyncio
async def test_poll_hold_is_shorter_than_docker_stop() -> None:
    assert module.POLL_HOLD_SECONDS < 10
    assert module.ONLINE_WINDOW_SECONDS > module.POLL_HOLD_SECONDS


@pytest.mark.asyncio
async def test_helper_lost_before_confirm(bridge: LocalBridge, clock: Clock) -> None:
    await _poll(bridge)
    task = await _start(bridge, "propose")
    await _poll(bridge)
    clock.now += module.ONLINE_WINDOW_SECONDS + 1
    result = await _finish(task)
    assert (result.status, result.code) == ("error", module.CODE_OFFLINE)


@pytest.mark.asyncio
async def test_helper_lost_after_confirm_is_unknown(bridge: LocalBridge, clock: Clock) -> None:
    await _poll(bridge)
    task = await _start(bridge, "propose")
    job = (await _poll(bridge)).job
    bridge.confirm(job.id, GENERATION)
    clock.now += module.ONLINE_WINDOW_SECONDS + 1
    result = await _finish(task)
    assert (result.status, result.code) == ("error", module.CODE_RESULT_UNKNOWN)


@pytest.mark.parametrize(
    ("workspace", "data", "expected"),
    [
        ("/srv/workspace", "", []),
        ("../work", "", ["../work"]),
        ("", "./data", ["./data"]),
        ("../work", "../data", ["../work", "../data"]),
        ("/srv/workspace", "./data", ["./data"]),
        ("D:/Work", "", ["D:/Work"]),  # noqa: PATH_LITERAL_SSOT
        ("D:\\Work", "E:\\Data", ["D:\\Work", "E:\\Data"]),  # noqa: PATH_LITERAL_SSOT
        ("  ", "  ", []),
    ],
)
def test_host_mount_roots(workspace: str, data: str, expected: list[str]) -> None:
    assert host_mount_roots(workspace, data) == expected
