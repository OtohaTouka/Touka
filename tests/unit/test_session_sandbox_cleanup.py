"""Session sandbox teardown: release what pins the folder, then delete it.

Covers the stdlib helper and the release steps conftest injects. The real
session sandbox is never touched here: every deletion targets tmp_path, and
conftest's own hooks run with the helper replaced.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from memory_server.factual import FactualStore
from src.core.logging_config import close_sandbox_log_handlers
from src.services.audit_service import PolicyAuditService

import tests.conftest as session_conftest
from tests import sandbox_cleanup
from tests.sandbox_cleanup import remove_session_sandbox

PINNED = PermissionError(32, "The process cannot access the file (synthetic lock)")
CLOSE_FAILURE = OSError("synthetic close failure")


def _sandbox(tmp_path):
    root = tmp_path / "touka_test_session_unit"
    (root / "vault" / "_qdrant_db").mkdir(parents=True)
    (root / "vault" / "_qdrant_db" / ".lock").write_text("", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    monkeypatch.setattr(sandbox_cleanup, "RMTREE_RETRY_SECONDS", 0)


def _pinned_rmtree(events: list[str], released: list[bool], real_rmtree):
    """Behave like Windows: deleting fails with WinError 32 until the handle is released."""

    def rmtree(path, ignore_errors=False):
        events.append("rmtree")
        if released:
            return real_rmtree(path, ignore_errors=ignore_errors)
        if not ignore_errors:
            raise PINNED
        return None

    return rmtree


def test_release_steps_run_before_delete(tmp_path, monkeypatch):
    root = _sandbox(tmp_path)
    events: list[str] = []
    released: list[bool] = []
    real_rmtree = sandbox_cleanup.shutil.rmtree
    monkeypatch.setattr(
        sandbox_cleanup,
        "shutil",
        SimpleNamespace(rmtree=_pinned_rmtree(events, released, real_rmtree)),
    )

    def release():
        events.append("release")
        released.append(True)

    assert remove_session_sandbox(root, [("close_qdrant_clients", release)]) is None
    assert not root.exists()
    assert events[0] == "release"


def test_failed_release_step_does_not_block_later_steps(tmp_path, caplog):
    root = _sandbox(tmp_path)
    ran: list[str] = []

    def broken():
        raise CLOSE_FAILURE

    steps = [("broken_step", broken), ("later_step", lambda: ran.append("later"))]
    with caplog.at_level(logging.WARNING, logger=sandbox_cleanup.__name__):
        assert remove_session_sandbox(root, steps) is None

    assert ran == ["later"]
    assert not root.exists()
    assert "broken_step" in caplog.text


def test_background_write_queued_before_cleanup_cannot_recreate_sandbox(tmp_path):
    root = _sandbox(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1)
    gate = threading.Event()

    def late_audit_write():
        gate.wait(5)
        (root / "stats").mkdir(parents=True, exist_ok=True)
        (root / "stats" / "decisions.csv").write_text("row\n", encoding="utf-8")

    try:
        executor.submit(late_audit_write)
        steps = [
            ("open_gate", gate.set),
            ("drain_writes", lambda: executor.submit(int).result(timeout=5)),
        ]
        assert remove_session_sandbox(root, steps) is None
        executor.shutdown(wait=True)
        assert not root.exists()
    finally:
        gate.set()
        executor.shutdown(wait=True)


def test_undeletable_sandbox_is_returned_as_residue(tmp_path, monkeypatch):
    root = _sandbox(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        sandbox_cleanup,
        "shutil",
        SimpleNamespace(rmtree=_pinned_rmtree(events, [], None)),
    )

    assert remove_session_sandbox(root, []) == root
    assert root.exists()
    assert events.count("rmtree") == sandbox_cleanup.RMTREE_ATTEMPTS + 1


def test_missing_sandbox_is_left_alone_and_cleanup_is_repeatable(tmp_path):
    root = _sandbox(tmp_path)
    calls: list[str] = []
    steps = [("count", lambda: calls.append("step"))]

    assert remove_session_sandbox(root, steps) is None
    assert remove_session_sandbox(root, steps) is None
    assert calls == ["step"]


def test_conftest_release_steps_cover_every_known_handle():
    steps = dict(session_conftest.SANDBOX_RELEASE_STEPS)

    assert set(steps) >= {
        "close_log_handlers",
        "close_qdrant_clients",
        "close_sqlite_connections",
        "drain_policy_audit_writes",
    }
    assert steps["close_log_handlers"].func is close_sandbox_log_handlers
    assert steps["close_log_handlers"].args == (session_conftest.SESSION_TMP_DATA_ROOT,)
    assert steps["close_sqlite_connections"] == FactualStore.close_all_connections
    assert steps["drain_policy_audit_writes"] is session_conftest._drain_policy_audit_writes


def test_qdrant_step_closes_clients_of_a_loaded_vector_module(monkeypatch):
    client = MagicMock()
    cache = {"vault/_qdrant_db": client}

    def close_all_clients():
        for cached in cache.values():
            cached.close()
        cache.clear()

    fake_vector = SimpleNamespace(VectorStore=SimpleNamespace(close_all_clients=close_all_clients))
    monkeypatch.setitem(sys.modules, "memory_server.vector", fake_vector)

    dict(session_conftest.SANDBOX_RELEASE_STEPS)["close_qdrant_clients"]()

    client.close.assert_called_once_with()
    assert cache == {}


def test_qdrant_step_is_a_no_op_before_the_vector_module_loads(monkeypatch):
    monkeypatch.delitem(sys.modules, "memory_server.vector", raising=False)

    dict(session_conftest.SANDBOX_RELEASE_STEPS)["close_qdrant_clients"]()


def test_policy_audit_drain_waits_for_queued_writes(tmp_path, monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(PolicyAuditService, "_executor", executor)
    written = tmp_path / "decisions.csv"

    def slow_write():
        time.sleep(0.3)
        written.write_text("row\n", encoding="utf-8")

    try:
        executor.submit(slow_write)
        session_conftest._drain_policy_audit_writes()
        assert written.exists()
    finally:
        executor.shutdown(wait=True)


class _RefusingExecutor:
    def __init__(self, message: str) -> None:
        self.message = message

    def submit(self, *_args):
        raise RuntimeError(self.message)


def test_policy_audit_drain_treats_interpreter_shutdown_as_drained(monkeypatch):
    """At interpreter exit concurrent.futures already joined the worker before atexit runs."""
    refusing = _RefusingExecutor("cannot schedule new futures after interpreter shutdown")
    monkeypatch.setattr(PolicyAuditService, "_executor", refusing)

    session_conftest._drain_policy_audit_writes()


def test_policy_audit_drain_reports_other_refusals(monkeypatch):
    refusing = _RefusingExecutor("cannot schedule new futures after shutdown")
    monkeypatch.setattr(PolicyAuditService, "_executor", refusing)

    with pytest.raises(RuntimeError, match="after shutdown"):
        session_conftest._drain_policy_audit_writes()


def test_exit_cleanup_reports_residue_on_stderr(tmp_path, monkeypatch, capsys):
    leftover = tmp_path / "touka_test_session_leftover"
    monkeypatch.setattr(session_conftest, "remove_session_sandbox", lambda _root, _steps: leftover)
    monkeypatch.setattr(session_conftest, "_sandbox_residue", [])

    session_conftest._cleanup_session_sandbox_at_exit()

    assert "測試沙盒沒刪乾淨：" in capsys.readouterr().err


def test_exit_cleanup_in_a_forked_child_leaves_the_parent_sandbox(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(session_conftest, "remove_session_sandbox", lambda *a: calls.append(a))
    monkeypatch.setattr(session_conftest, "_SANDBOX_OWNER_PID", -1)

    session_conftest._cleanup_session_sandbox_at_exit()

    assert calls == []


@pytest.mark.parametrize("residue", [None, "leftover"])
def test_terminal_summary_lists_sandbox_residue(tmp_path, monkeypatch, residue):
    leftover = None if residue is None else tmp_path / "touka_test_session_leftover"
    monkeypatch.setattr(session_conftest, "remove_session_sandbox", lambda _root, _steps: leftover)
    monkeypatch.setattr(session_conftest, "_sandbox_residue", [])
    lines: list[str] = []

    session_conftest.pytest_sessionfinish()
    session_conftest.pytest_terminal_summary(SimpleNamespace(write_line=lines.append))

    assert lines == ([] if leftover is None else [f"測試沙盒沒刪乾淨：{leftover}"])
