"""執行中子進程登記表 (process_registry) 單元測試 — 硬中斷的底層狀態管理。"""
from __future__ import annotations

from src.services.process_registry import ProcessRegistry


class _FakeProc:
    def __init__(self) -> None:
        self.returncode = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_register_and_count():
    reg = ProcessRegistry()
    reg.register("s1", _FakeProc())
    assert reg.count("s1") == 1
    reg.register("s1", _FakeProc())
    assert reg.count("s1") == 2


def test_unregister_removes_and_cleans_empty():
    reg = ProcessRegistry()
    p = _FakeProc()
    reg.register("s1", p)
    reg.unregister("s1", p)
    assert reg.count("s1") == 0
    assert "s1" not in reg._session_processes


def test_terminate_session_kills_running_procs():
    reg = ProcessRegistry()
    p1, p2 = _FakeProc(), _FakeProc()
    reg.register("s1", p1)
    reg.register("s1", p2)
    killed = reg.terminate_session("s1")
    assert killed == 2
    assert p1.killed and p2.killed
    assert reg.count("s1") == 0


def test_terminate_skips_already_finished_procs():
    reg = ProcessRegistry()
    done = _FakeProc()
    done.returncode = 0  # 已結束
    reg.register("s1", done)
    assert reg.terminate_session("s1") == 0
    assert not done.killed


def test_terminate_unknown_session_is_noop():
    reg = ProcessRegistry()
    assert reg.terminate_session("nope") == 0


def test_register_ignores_empty_session():
    reg = ProcessRegistry()
    reg.register(None, _FakeProc())
    reg.register("", _FakeProc())
    assert reg.count("") == 0


def test_reload_all_clears():
    reg = ProcessRegistry()
    reg.register("s1", _FakeProc())
    reg.reload_all()
    assert reg.count("s1") == 0
