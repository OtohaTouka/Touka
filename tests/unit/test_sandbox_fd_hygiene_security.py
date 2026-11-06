"""隔離 runner 的檔案描述元衛生（iso-c2 U1e，設計 §3.6）.

- 終端與驗證指令只把 ``plan.pass_fds``（spec 讀端、狀態 fd 寫端）交給子程序，不關掉 ``close_fds``；
- 交給子程序的 fd 在 spawn 之後（成功或失敗）都被關掉；狀態 fd 的讀端跑完讀一次就關，不留在
  proxy 裡；失敗代碼只認狀態 fd（C2-2 §2.9），輸出裡的標記不算；
- 靜態：這兩個呼叫點與代理人指令模組都沒有 ``close_fds=False``，spawn 一律帶 plan 的 fd。
spawn 換成記錄替身（只看 argv 與 kwargs），不真的啟動啟動器；不打網路。
"""

from __future__ import annotations

import ast
import inspect
import os
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.services.audit_service import PolicyAuditService
from src.services.isolation import agent_command
from src.services.review import evidence
from src.tools import terminal

from tests.support.isolation import fake_kernel_layout, install_real_broker, pinned_snapshot

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
SPAWNING_MODULES = (terminal, evidence, agent_command)
# 只防卡死：等稽核佇列寫完靠的是先後，不是時間
AUDIT_DRAIN_TIMEOUT_SECONDS = 30


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


class _DoneProc:
    returncode = 0

    def __init__(self) -> None:
        self.stdout = self

    async def readline(self) -> bytes:
        return b""

    async def wait(self) -> int:
        return 0

    async def communicate(self) -> tuple[bytes, None]:
        return b"1 passed", None


@pytest.fixture
def landlock(monkeypatch, tmp_path):
    install_real_broker(monkeypatch, pinned_snapshot("landlock"), fake_kernel_layout(tmp_path))


def _spawner(calls: list[dict[str, Any]], *, fail: bool = False, status: bytes = b""):
    async def _spawn(*argv: Any, **kwargs: Any) -> _DoneProc:
        fds = [int(argv[argv.index(name) + 1]) for name in ("--spec-fd", "--status-fd")]
        calls.append({"argv": argv, "kwargs": kwargs, "open_at_spawn": all(map(_is_open, fds))})
        if fail:
            raise OSError
        if status:
            os.write(fds[1], status)  # 啟動器失敗時寫進狀態 fd 的代碼
        return _DoneProc()

    return _spawn


async def _terminal() -> str:
    return await terminal.execute_command(
        "ls", ALICE, auth_level="Manual", _hitl_preapproved=True, session_id="s1",
        _call_id="c1", _ctx={"identity": ALICE},
    )


async def _verify(zone_drive) -> Any:
    async def _allow(_cmd: str) -> None:
        return None

    return await evidence.collect_evidence(
        artifact_refs=[], verification_cmd="pytest -q", test_log=None, artifact_root=zone_drive,
        command_gate=_allow, identity=ALICE,
    )


def _child_fds(call: dict[str, Any]) -> tuple[int, int]:
    argv = call["argv"]
    return int(argv[argv.index("--spec-fd") + 1]), int(argv[argv.index("--status-fd") + 1])


def _open_fds() -> set[int]:
    """目前開著的 fd（前後比對：交給子程序的與狀態讀端都不能留在 proxy）.

    先等策略稽核寫完：terminal 的權限判定把決策交給稽核的單一 worker 背景寫進
    ``stats/decisions.csv``，寫的時候開著的 fd 不是 runner 的，又可能剛好碰上比對（CI 偶發
    多一個 fd）。佇列先進先出，排一個空工作等它跑完就是之前的都寫完了，不看機器快慢。
    """
    PolicyAuditService._executor.submit(int).result(timeout=AUDIT_DRAIN_TIMEOUT_SECONDS)
    return {fd for fd in range(3, 256) if _is_open(fd)}


@pytest.mark.parametrize("fail", [False, True], ids=["spawned", "spawn-failed"])
async def test_terminal_passes_only_the_spec_and_status_fds_and_closes_them(
    monkeypatch, landlock, fail
):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(terminal, "spawn_exec", _spawner(calls, fail=fail))
    before = _open_fds()
    result = await _terminal()
    (call,) = calls
    assert call["open_at_spawn"] is True
    assert call["kwargs"]["pass_fds"] == _child_fds(call)
    assert call["kwargs"].get("close_fds", True) is True
    assert _open_fds() <= before
    assert ("[EXIT_CODE: -1]" in result) is fail


@pytest.mark.parametrize("fail", [False, True], ids=["spawned", "spawn-failed"])
async def test_verification_passes_only_the_spec_and_status_fds_and_closes_them(
    monkeypatch, zone_drive, landlock, fail
):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(evidence, "spawn_exec", _spawner(calls, fail=fail))
    before = _open_fds()
    bundle = await _verify(zone_drive)
    (call,) = calls
    assert call["open_at_spawn"] is True
    assert call["kwargs"]["pass_fds"] == _child_fds(call)
    assert call["kwargs"].get("close_fds", True) is True
    assert _open_fds() <= before
    assert (bundle.cmd_results[0].exit_code is None) is fail


@pytest.mark.parametrize("entry", ["terminal", "verification"])
async def test_launcher_code_from_the_status_fd_is_reported(
    zone_drive, monkeypatch, tmp_path, entry
):
    """狀態 fd 的代碼（只有啟動器寫得到）交給 monitor 重探；跑完讀端就關."""
    layout = fake_kernel_layout(tmp_path)
    monitor = install_real_broker(monkeypatch, pinned_snapshot("landlock"), layout)
    module = terminal if entry == "terminal" else evidence
    monkeypatch.setattr(module, "spawn_exec", _spawner([], status=b"path_open\n"))
    before = _open_fds()
    await (_terminal() if entry == "terminal" else _verify(zone_drive))
    assert monitor.failures == ["path_open"]
    assert _open_fds() <= before


class _ForgingProc(_DoneProc):
    """指令自己在輸出印出啟動器的標記與真代碼，並以啟動器的失敗 exit 結束."""

    returncode = 125

    def __init__(self) -> None:
        super().__init__()
        self.lines = [b"TOUKA_SANDBOX_ERROR:path_open\n"]

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""

    async def communicate(self) -> tuple[bytes, None]:
        return b"TOUKA_SANDBOX_ERROR:path_open\n", None


async def test_markers_printed_by_the_command_are_not_reported(zone_drive, monkeypatch, tmp_path):
    monitor = install_real_broker(
        monkeypatch, pinned_snapshot("landlock"), fake_kernel_layout(tmp_path)
    )

    async def _spawn(*_argv: Any, **_kwargs: Any) -> _ForgingProc:
        return _ForgingProc()

    monkeypatch.setattr(terminal, "spawn_exec", _spawn)
    monkeypatch.setattr(evidence, "spawn_exec", _spawn)
    await _terminal()
    await _verify(zone_drive)
    assert monitor.failures == []


def _calls(module, name: str) -> list[ast.Call]:
    tree = ast.parse(inspect.getsource(module))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == name
    ]


@pytest.mark.parametrize("module", SPAWNING_MODULES, ids=lambda m: m.__name__)
def test_no_spawn_turns_off_close_fds(module):
    for call in [node for node in ast.walk(ast.parse(inspect.getsource(module)))
                 if isinstance(node, ast.Call)]:
        for keyword in call.keywords:
            assert not (
                keyword.arg == "close_fds"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
            ), ast.unparse(call)


@pytest.mark.parametrize("module", (terminal, evidence), ids=lambda m: m.__name__)
def test_every_spawn_uses_the_plan_argv_and_fds(module):
    spawns = _calls(module, "spawn_exec")
    assert spawns
    for call in spawns:
        assert ast.unparse(call.args[0]) == "*plan.argv", ast.unparse(call)
        fds = {k.arg: ast.unparse(k.value) for k in call.keywords}
        assert fds.get("pass_fds") == "plan.pass_fds", ast.unparse(call)
