"""P5d：terminate_process_tree — bash 孫進程連坐殺（POSIX killpg／Windows taskkill /T）。"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from src.services.process_registry import terminate_process_tree


class _FakeProc:
    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        self.returncode = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_no_pid_falls_back_to_kill():
    """proc 無 pid（尚未啟動）→ 回退至 proc.kill()。"""
    p = _FakeProc(pid=None)
    terminate_process_tree(p)
    assert p.killed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX killpg path")
def test_posix_uses_killpg(monkeypatch):
    """POSIX：以 os.killpg 殺整個行程群組（pgid = 進程組長 pid）。"""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "src.services.process_registry.os.getpgid", lambda pid: pid
    )
    monkeypatch.setattr(
        "src.services.process_registry.os.killpg",
        lambda pgid, sig: calls.append((pgid, sig)),
    )
    terminate_process_tree(_FakeProc(pid=4242))
    assert calls and calls[0][0] == 4242


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill path")
def test_windows_uses_taskkill(monkeypatch):
    """Windows：以 taskkill /F /T /PID 殺整個進程樹。"""
    calls: list[tuple] = []
    monkeypatch.setattr(
        "src.services.process_registry.subprocess.run",
        lambda *a, **k: calls.append((a, k)),
    )
    terminate_process_tree(_FakeProc(pid=4242))
    assert calls
    argv = calls[0][0][0]
    assert argv[0] == "taskkill"
    assert "/T" in argv
    assert "4242" in argv


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill path")
def test_windows_taskkill_failure_falls_back_to_direct_kill(monkeypatch):
    """受限環境拒絕 taskkill 時，至少要終止直接子進程。"""
    monkeypatch.setattr(
        "src.services.process_registry.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    proc = _FakeProc(pid=4242)

    terminate_process_tree(proc)

    assert proc.killed


def test_already_finished_process_is_swallowed(monkeypatch):
    """已結束的進程（getpgid/taskkill 拋 ProcessLookupError）→ 不外拋例外。"""
    if sys.platform == "win32":
        def _raise(*a, **k):
            raise ProcessLookupError

        monkeypatch.setattr(
            "src.services.process_registry.subprocess.run", _raise
        )
    else:
        def _raise(_pid):
            raise ProcessLookupError

        monkeypatch.setattr(
            "src.services.process_registry.os.getpgid", _raise
        )
    # 不應拋出
    terminate_process_tree(_FakeProc(pid=999999))
