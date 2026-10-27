"""舊審查報告丙-5（ADR-0289）：最後一次重置的結果記在行程記憶體，「結果不明」時查得到.

重置只留一個替身 store、備份換成替身；勾選清除連備份的情境在
``test_memory_scrub_orchestrator.py``（o23～o26）。只用合成 ID。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.agent_reset import AgentResetResponse, LastResetStatus
from src.models.identity import AgentIdentity
from src.services import destructive_ops, system_backup
from src.services.agent_reset import last_result as L
from src.services.agent_reset import orchestrator as O
from src.services.agent_reset import primitives as P
from src.services.identity_router import get_verified_identity

if TYPE_CHECKING:
    from collections.abc import Callable

TARGET = "zz-last-a"
OTHER = "zz-last-b"
ADMIN = AgentIdentity.for_role(agent_id="zz-admin-l", role="admin", is_human=True)
ASSUMED_ADMIN = AgentIdentity.for_role(agent_id="zz-admin-x", role="admin")  # X-Agent-ID 代打的形狀
BACKUP = {"status": "success", "message": "ok", "filename": "Touka_Backup_last.zip"}
REPLY_TIMEOUT_S = 60


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(L, "_last", LastResetStatus(state="idle"))
    monkeypatch.setattr(O, "assert_agent_resettable", lambda c, _a: c)
    monkeypatch.setattr(system_backup, "backup_execution_lock", asyncio.Lock())
    monkeypatch.setattr(system_backup, "create_backup", lambda *, auto=False: dict(BACKUP))
    state = SimpleNamespace(purge=None)

    async def purge(candidate: str, _guard: Callable[[str], str]) -> int:
        if state.purge is not None:
            return await state.purge(candidate)
        return 2

    async def remaining(_candidate: str, _guard: Callable[[str], str]) -> int:
        return 0

    monkeypatch.setattr(O, "ALL_STORES", (P.StorePurger("fake", purge, remaining),))
    return state


async def _reset(target: str = TARGET, **kwargs: object) -> O.ResetReport:
    return await asyncio.wait_for(O.reset_agent_data(target, ADMIN, **kwargs), REPLY_TIMEOUT_S)


def _blocking(state: SimpleNamespace) -> tuple[asyncio.Event, asyncio.Event]:
    entered, release = asyncio.Event(), asyncio.Event()

    async def purge(_candidate: str) -> int:
        entered.set()
        await release.wait()
        return 2

    state.purge = purge
    return entered, release


async def test_l01_finished_reset_is_kept_with_its_report(env: SimpleNamespace) -> None:
    report = await _reset()
    last = L.last_reset()
    assert (last.state, last.agent_id, last.error_code) == ("done", TARGET, None)
    assert last.report == AgentResetResponse.model_validate(report.to_dict())
    assert last.report.backup.filename == BACKUP["filename"]
    assert last.started_at is not None
    assert last.finished_at is not None
    assert last.started_at <= last.finished_at


async def test_l02_running_reset_is_reported_as_running(env: SimpleNamespace) -> None:
    entered, release = _blocking(env)
    task = asyncio.create_task(_reset())
    await asyncio.wait_for(entered.wait(), REPLY_TIMEOUT_S)
    last = L.last_reset()
    assert (last.state, last.agent_id, last.finished_at, last.report) == (
        "running",
        TARGET,
        None,
        None,
    )
    assert last.started_at is not None
    release.set()
    await task
    assert L.last_reset().state == "done"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ToukaException(ErrorCode.SYSTEM_BACKUP_FAILED), "SYSTEM_BACKUP_FAILED"),
        (RuntimeError("secret detail"), "SYSTEM_ERROR"),
    ],
)
async def test_l03_failed_reset_keeps_only_the_error_code(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    def failing(*, auto: bool = False) -> dict:
        raise error

    monkeypatch.setattr(system_backup, "create_backup", failing)
    with pytest.raises(type(error)):
        await _reset()
    last = L.last_reset()
    assert (last.state, last.agent_id, last.error_code, last.report) == (
        "failed",
        TARGET,
        code,
        None,
    )
    assert last.finished_at is not None
    assert "secret" not in last.model_dump_json()  # 禁令 23：不放例外原文


async def test_l04_cancelled_reset_is_interrupted(env: SimpleNamespace) -> None:
    entered, _release = _blocking(env)
    task = asyncio.create_task(_reset())
    await asyncio.wait_for(entered.wait(), REPLY_TIMEOUT_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    last = L.last_reset()
    assert (last.state, last.agent_id, last.report) == ("interrupted", TARGET, None)
    assert last.finished_at is not None


async def test_l05_rejected_requests_do_not_replace_the_last_result(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _reset()
    kept = L.last_reset()

    def not_resettable(_c: str, _a: AgentIdentity) -> str:
        raise ToukaException(ErrorCode.VALIDATION_ERROR)

    monkeypatch.setattr(O, "assert_agent_resettable", not_resettable)
    with pytest.raises(ToukaException):
        await _reset(OTHER)
    assert L.last_reset() is kept
    monkeypatch.setattr(O, "assert_agent_resettable", lambda c, _a: c)
    entered, release = _blocking(env)
    task = asyncio.create_task(_reset())
    await asyncio.wait_for(entered.wait(), REPLY_TIMEOUT_S)
    running = L.last_reset()
    with pytest.raises(ToukaException) as exc:  # 已經有一次在跑：409，不算一次重置
        await _reset(OTHER)
    assert exc.value.code is ErrorCode.AGENT_RESET_IN_PROGRESS
    assert L.last_reset() is running
    release.set()
    await task


async def test_l06_a_report_that_cannot_be_kept_does_not_break_the_reset(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system_backup, "create_backup", lambda *, auto=False: {"status": "ok"})
    report = await _reset()  # 重置本身照常結束
    assert report.clean is True
    last = L.last_reset()
    assert (last.state, last.error_code, last.report) == ("failed", "SYSTEM_ERROR", None)


async def test_l09_result_is_kept_before_the_lock_is_released(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = destructive_ops.release

    def release(target: AgentIdentity) -> None:
        seen.append(L.last_reset().state)  # 放鎖時已經記好：下一次重置蓋不掉
        real(target)

    monkeypatch.setattr(destructive_ops, "release", release)
    await _reset()
    assert seen == ["done"]


def _get(identity: AgentIdentity) -> object:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    try:
        client = TestClient(app, raise_server_exceptions=False)
        with ThreadPoolExecutor(1) as pool:  # 同步的 TestClient 也要有上限
            return pool.submit(client.get, "/v1/admin/agents/reset/last").result(
                timeout=REPLY_TIMEOUT_S
            )
    finally:
        app.dependency_overrides.clear()


def test_l07_endpoint_returns_the_last_result_to_a_human_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(L, "_last", LastResetStatus(state="idle"))
    response = _get(ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "idle"
    status = LastResetStatus(
        state="failed",
        agent_id=TARGET,
        started_at=1.0,
        finished_at=2.0,
        error_code="SYSTEM_BACKUP_FAILED",
    )
    monkeypatch.setattr(L, "_last", status)
    response = _get(ADMIN)
    assert response.status_code == 200, response.text
    assert LastResetStatus.model_validate(response.json()) == status


@pytest.mark.parametrize(
    "identity",
    [
        ASSUMED_ADMIN,
        AgentIdentity.for_role(agent_id="zz-user", role="normal", is_human=True),
    ],
)
def test_l08_endpoint_refuses_everyone_but_a_human_admin(
    monkeypatch: pytest.MonkeyPatch, identity: AgentIdentity
) -> None:
    monkeypatch.setattr(
        L, "_last", LastResetStatus(state="failed", agent_id=TARGET, error_code="SYSTEM_ERROR")
    )
    response = _get(identity)
    assert response.status_code == 403, response.text
    assert TARGET not in response.text
