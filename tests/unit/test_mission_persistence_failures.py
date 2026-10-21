"""可靠性 R2 B1：受管保存失敗不可偽裝成功；所有資料均由 pytest sandbox 提供。"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from memory_server.config_utils import get_resource_proxy
from memory_server.models.mission import TaskStatus
from memory_server.services import mission_manager as mission_module
from memory_server.services.mission_manager import MissionManager
from memory_server.tools.mission_tools import mission_tools as memory_mission_tools
from src.core.exceptions import ErrorCode
from src.models.identity import AgentIdentity
from src.tools.mission_tools import mission_tools

from ToukaDispatch import server as dispatch_server

if TYPE_CHECKING:
    from memory_server.models.mission import MissionState

SENSITIVE_CAUSE = "synthetic-private-board-content-and-path"


async def _board(*, done: bool = False) -> tuple[MissionManager, AgentIdentity, MissionState]:
    manager = MissionManager()
    identity = AgentIdentity.for_role("ReliabilityBoard", "Owner")
    state = await manager.initialize_mission(
        identity, "original goal", [{"id": "t1", "description": "one task"}]
    )
    if done:
        await manager.update_task(identity, "t1", TaskStatus.DOING)
        state = await manager.update_task(identity, "t1", TaskStatus.DONE)
    return manager, identity, state


def _fail_write(monkeypatch: pytest.MonkeyPatch) -> Mock:
    failed = Mock(side_effect=OSError(SENSITIVE_CAUSE))
    monkeypatch.setattr(get_resource_proxy(), "write_file_text", failed)
    return failed


async def _mutation(
    operation: str, manager: MissionManager, identity: AgentIdentity, state: MissionState
) -> None:
    if operation == "initialize":
        await manager.initialize_mission(identity, "replacement goal")
    elif operation == "save":
        candidate = state.model_copy(deep=True)
        candidate.goal = "replacement goal"
        await manager.save_mission(identity, candidate)
    elif operation == "update":
        await manager.update_task(identity, "t1", TaskStatus.DOING)
    elif operation == "review":
        await manager.record_review(
            identity, "t1", review_status="APPROVED", reviewer_id="SeparateReviewer"
        )
    elif operation == "finalize":
        await manager.finalize_mission(identity, "approved outcome")
    else:
        pytest.fail(f"unhandled operation {operation}")


@pytest.mark.parametrize("operation", ["initialize", "save", "update", "review", "finalize"])
async def test_precommit_failure_raises_and_preserves_board_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    manager, identity, state = await _board(done=operation == "finalize")
    old = state.model_dump()
    with monkeypatch.context() as failure:
        write = _fail_write(failure)
        with pytest.raises(RuntimeError, match="Mission persistence failed") as caught:
            await _mutation(operation, manager, identity, state)
        assert type(caught.value).__name__ == "MissionPersistenceError"
        assert SENSITIVE_CAUSE not in str(caught.value)
        assert isinstance(caught.value.__cause__, OSError)
        write.assert_called_once()
        reloaded = await asyncio.wait_for(manager.get_current_mission(identity), timeout=2)
        assert reloaded is not None
        assert reloaded.model_dump() == old
    await asyncio.wait_for(_mutation(operation, manager, identity, state), timeout=2)


async def test_failed_save_does_not_mutate_callers_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, identity, state = await _board()
    before = state.model_dump()
    monkeypatch.setattr(mission_module, "get_current_epoch", lambda: state.created_at + 10)
    _fail_write(monkeypatch)
    with suppress(RuntimeError):
        await manager.save_mission(identity, state)
    assert state.model_dump() == before


async def test_successful_save_updates_timestamp_only_after_managed_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, identity, state = await _board()
    before = state.updated_at
    expected = before + 10
    original_write = get_resource_proxy().write_file_text

    def observed_write(path: str, content: str) -> None:
        assert state.updated_at == before
        assert json.loads(content)["updated_at"] == expected
        original_write(path, content)

    monkeypatch.setattr(mission_module, "get_current_epoch", lambda: expected)
    monkeypatch.setattr(get_resource_proxy(), "write_file_text", observed_write)
    await manager.save_mission(identity, state)
    assert state.updated_at == expected
    reloaded = await manager.get_current_mission(identity)
    assert reloaded is not None and reloaded.updated_at == expected


@pytest.mark.parametrize("action", ["initialize", "update_status", "finalize"])
async def test_native_boundary_returns_safe_structured_persistence_error(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    _, identity, _ = await _board(done=action == "finalize")
    _fail_write(monkeypatch)
    output = await mission_tools(
        action,
        identity=identity,
        goal="replacement",
        task_id="t1",
        status="DOING",
        verdict="approved outcome",
    )
    result = json.loads(output)
    assert result["error_class"] == "internal_error"
    assert result["error_code"] == ErrorCode.PERSISTENCE_ERROR.value
    assert result["outcome"] == "unknown"
    assert SENSITIVE_CAUSE not in output
    assert "✅" not in output


@pytest.mark.parametrize("action", ["init", "update"])
async def test_memory_bridge_propagates_safe_domain_failure(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    _, identity, _ = await _board()
    _fail_write(monkeypatch)
    with pytest.raises(RuntimeError, match="Mission persistence failed"):
        await memory_mission_tools(
            action, identity=identity, goal="replacement", task_id="t1", status="DOING"
        )


async def test_dispatch_does_not_rotate_token_or_invoke_after_save_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_manager = type(dispatch_server._session_mgr)()
    monkeypatch.setattr(dispatch_server, "_session_mgr", session_manager)
    session_token = session_manager.create_session({"name": "synthetic-internal"})
    rotate = Mock(return_value="synthetic-issued-credential")
    invoke = Mock(return_value="synthetic invocation completed")
    monkeypatch.setattr(session_manager, "rotate_mission_token", rotate)
    monkeypatch.setattr(dispatch_server, "_invoke_external_ai", invoke)
    _fail_write(monkeypatch)
    output = await dispatch_server.dispatch_initialize_mission(
        session_token, "owned synthetic dispatch", auto_invoke="synthetic-executor"
    )
    rotate.assert_not_called()
    invoke.assert_not_called()
    assert "Mission persistence failed" in output
    assert SENSITIVE_CAUSE not in output


@pytest.mark.parametrize("read_failure", ["empty", "malformed", "io_error"])
@pytest.mark.parametrize("operation", ["update", "review", "finalize"])
async def test_unreadable_board_does_not_trigger_implicit_mutation(
    monkeypatch: pytest.MonkeyPatch, read_failure: str, operation: str
) -> None:
    manager, identity, state = await _board()
    if read_failure == "io_error":
        read = Mock(side_effect=OSError(SENSITIVE_CAUSE))
    else:
        read = Mock(return_value="" if read_failure == "empty" else "{invalid json")
    monkeypatch.setattr(get_resource_proxy(), "read_file_text", read)
    write = Mock()
    monkeypatch.setattr(get_resource_proxy(), "write_file_text", write)
    with pytest.raises(ValueError, match="No active mission"):
        await _mutation(operation, manager, identity, state)
    write.assert_not_called()
