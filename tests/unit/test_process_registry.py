"""執行中子進程登記表 (process_registry) 單元測試 — 硬中斷的底層狀態管理。

ADR-0293 R11：登記時可帶 ``agent_id``，重置與刪除以 ``terminate_agent`` 跨 session 停掉這個
代理人的全部子行程。
"""
from __future__ import annotations

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
