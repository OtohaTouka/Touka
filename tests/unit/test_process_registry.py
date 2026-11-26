"""執行中子進程登記表 (process_registry) 單元測試 — 硬中斷的底層狀態管理。

ADR-0293 R11：登記時可帶 ``agent_id``，重置與刪除以 ``terminate_agent`` 跨 session 停掉這個
代理人的全部子行程。
"""
from __future__ import annotations

import threading
from typing import Any

from src.models.identity import AgentIdentity
from src.services.process_registry import ProcessRegistry

ALICE = AgentIdentity.from_str("Alice")


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
    """沒有 session 也沒有 agent_id 才略過。"""
    reg = ProcessRegistry()
    reg.register(None, _FakeProc())
    reg.register("", _FakeProc())
    assert reg.count("") == 0


def test_reload_all_clears():
    reg = ProcessRegistry()
    reg.register("s1", _FakeProc())
    reg.register(None, _FakeProc(), agent_id="Alice")
    reg.reload_all()
    assert reg.count("s1") == 0
    assert reg.terminate_agent(ALICE) == 0


def test_terminate_agent_kills_its_procs_across_sessions():
    """跨 session、沒有 session 的也算；ID 不分大小寫；別的代理人不受影響。"""
    reg = ProcessRegistry()
    a, b, other = _FakeProc(), _FakeProc(), _FakeProc()
    reg.register("s1", a, agent_id="Alice")
    reg.register(None, b, agent_id="alice")
    reg.register("s1", other, agent_id="Bob")
    assert reg.terminate_agent(AgentIdentity.from_str("ALICE")) == 2
    assert a.killed and b.killed and not other.killed
    assert reg.terminate_agent(ALICE) == 0  # 已取出
    assert reg.count("s1") == 2  # session 的登記照舊由行程結束時註銷


def test_unregister_with_agent_id_removes_both():
    reg = ProcessRegistry()
    p = _FakeProc()
    reg.register("s1", p, agent_id="Alice")
    reg.unregister("s1", p, agent_id="Alice")
    assert reg.count("s1") == 0
    assert reg._agent_processes == {}
    assert reg.terminate_agent(ALICE) == 0 and not p.killed


class _PausingAdd(dict):
    """登記時拿到集合之後、加入之前停住（模擬事件迴圈正要加入時，執行緒插進來取出）。"""

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.entered, self.release, self.armed = entered, release, False

    def setdefault(self, key: Any, default: Any = None) -> Any:
        found = super().setdefault(key, default)
        if self.armed:
            self.armed = False
            self.entered.set()
            self.release.wait(5)
        return found


def test_a_proc_registered_while_the_agent_is_being_stopped_is_not_lost():
    """ADR-0333：子代理的監看一直在執行緒裡收；取出和加入要互斥，不然新行程加進已取出的集合。"""
    reg = ProcessRegistry()
    entered, release = threading.Event(), threading.Event()
    table = _PausingAdd(entered, release)
    reg._agent_processes = table
    first, late = _FakeProc(), _FakeProc()
    reg.register(None, first, agent_id="Alice")
    table.armed = True
    adder = threading.Thread(target=reg.register, args=(None, late), kwargs={"agent_id": "Alice"})
    adder.start()
    assert entered.wait(5)
    stopper = threading.Thread(target=reg.terminate_agent, args=(ALICE,))
    stopper.start()
    stopper.join(0.2)  # 沒有鎖時，這段時間它已經取出舊集合、收完
    release.set()
    adder.join(5)
    stopper.join(5)
    assert first.killed
    assert late.killed or reg.terminate_agent(ALICE) == 1
