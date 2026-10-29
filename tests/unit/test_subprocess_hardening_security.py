"""子程序執行加固（ADR-0239 後續，ADR-0137 補記，SECURITY.md SP1–SP7）——TDD.

ADR-0239 唯讀盤點找到的子程序缺口，逐項以失敗測試鎖定：

- SP1 互審蒐證 ``verification_cmd``：繼承完整機密環境、無行程群組、取消不殺子程序、
  無逐指令政策、vault 不可用時退回 Proxy 的 cwd。
- SP2 ``execute_command``：政策回 ``DENY`` 時仍照常執行（只認 ``BLOCK``）；取消不殺子程序。
- SP3 ``POST /mcp/scan``：端點已移除，回歸測試在 ``test_mcp_scan_removed_security.py``。
- SP4 還原：用 PATH 上的 ``python``、無逾時。
- SP5 agent reset：在事件迴圈內同步備份、未取 ``backup_execution_lock``。
- SP6 演化沙箱：venv／pip 無逾時；venv／pip／本機驗證阻塞事件迴圈；呼叫端 ``env`` 可把機密加回來。
- SP7 Creation clean env 不解析哨兵（ADR-0198 §5 的刻意設計，以測試釘住）。

全部測試只用 sandbox／mock 與 ``sys.executable``，不讀寫實體 ``data/``。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import types
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from mcp import types as mcp_types
from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.config import settings
from src.models.identity import AgentIdentity
from src.protocols.mcp.environment import LaunchEnvironmentPolicy
from src.routers import system as system_router
from src.services import permission_service as permission_module
from src.services import system_backup
from src.services.agent_reset import orchestrator
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation import evolution_command
from src.services.review import evidence
from src.services.review.budget import _reset_review_ledger
from src.services.review.evidence import collect_evidence
from src.services.review.review_loop import ReviewOutcome, run_review_round
from src.tools import mission_tools as mt
from src.tools import terminal
from src.tools.builtin import evolution
from src.utils.mcp_sentinels import APPDATA_SENTINEL

from tests.support.isolation import passthrough_launch

_PY = sys.executable
_OWNER = AgentIdentity.for_role(agent_id="SubprocSec", role="Owner", capabilities=[])
_SECRET_PATTERNS = ("SECRET", "TOKEN", "KEY", "AUTH", "PASSWORD", "CREDENTIAL")


async def _allow_all(_cmd: str) -> str | None:
    return None


def _group_kwargs_ok(kwargs: dict) -> bool:
    """子程序必須以新行程群組啟動，逾時／取消才能連坐殺孫進程。"""
    if sys.platform == "win32":
        return bool(kwargs.get("creationflags", 0) & subprocess.CREATE_NEW_PROCESS_GROUP)
    return kwargs.get("start_new_session") is True


class _HangingProc:
    """永不結束的 asyncio subprocess 替身。"""

    pid = 424242

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = None

    async def communicate(self):
        await asyncio.Event().wait()

    async def wait(self) -> int:
        self.returncode = -9
        return -9

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture(autouse=True)
def _fresh_review_ledger():
    _reset_review_ledger()
    yield
    _reset_review_ledger()


# --------------------------------------------------------------------------- #
# SP1 互審蒐證 verification_cmd
# --------------------------------------------------------------------------- #


async def test_review_cmd_child_does_not_inherit_secrets(zone_drive, monkeypatch):
    monkeypatch.setenv("TOUKA_AUTH_SECRET", "leak-me-auth")
    monkeypatch.setenv("OPENAI_API_KEY", "leak-me-key")
    cmd = (
        f'"{_PY}" -c "import os; print(os.environ.get(\'TOUKA_AUTH_SECRET\', \'none\'),'
        f' os.environ.get(\'OPENAI_API_KEY\', \'none\'))"'
    )
    bundle = await collect_evidence(
        artifact_refs=[],
        verification_cmd=cmd,
        test_log=None,
        artifact_root=zone_drive,
        command_gate=_allow_all,
    )
    output = bundle.cmd_results[0].output_snippet
    assert "leak-me" not in output
    assert "none none" in output


async def test_review_cmd_timeout_kills_whole_process_group(zone_drive, monkeypatch):
    proc = _HangingProc()
    seen: dict = {}
    killed: list = []

    async def _fake_exec(*_args, **kwargs):
        seen.update(kwargs)
        return proc

    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(evidence, "terminate_process_tree", killed.append)

    bundle = await collect_evidence(
        artifact_refs=[],
        verification_cmd="pytest -q",
        test_log=None,
        artifact_root=zone_drive,
        timeout_seconds=0.05,
        command_gate=_allow_all,
    )

    assert _group_kwargs_ok(seen)
    assert killed == [proc]
    assert "timeout" in (bundle.cmd_results[0].rejected_reason or "")


async def test_review_cmd_cancellation_kills_child(zone_drive, monkeypatch):
    proc = _HangingProc()
    spawned = asyncio.Event()
    killed: list = []

    async def _fake_exec(*_args, **_kwargs):
        spawned.set()
        return proc

    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(evidence, "terminate_process_tree", killed.append)

    task = asyncio.create_task(
        collect_evidence(
            artifact_refs=[],
            verification_cmd="pytest -q",
            test_log=None,
            artifact_root=zone_drive,
            command_gate=_allow_all,
        )
    )
    await asyncio.wait_for(spawned.wait(), timeout=5)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [proc]


async def test_review_cmd_rejected_by_gate_is_never_spawned(zone_drive, monkeypatch):
    spawned: list = []

    async def _fake_exec(*args, **_kwargs):
        spawned.append(args)
        return _HangingProc()

    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", _fake_exec)

    async def _needs_approval(_cmd: str) -> str | None:
        return "needs approval"

    bundle = await collect_evidence(
        artifact_refs=[],
        verification_cmd="pytest -q",
        test_log=None,
        artifact_root=zone_drive,
        command_gate=_needs_approval,
    )

    assert spawned == []
    assert bundle.cmd_results[0].rejected_reason == "needs approval"
    assert bundle.hard_checks_pass is False


@pytest.mark.parametrize(
    ("strategy", "human", "allowed", "asked"),
    [
        ("ALLOW", None, True, False),
        ("ALLOW", False, True, False),
        ("CONFIRM", None, False, False),
        ("CONFIRM", False, False, True),
        ("CONFIRM", True, True, True),
        ("DENY", True, False, False),
        ("BLOCK", True, False, False),
        ("", True, False, False),
    ],
)
async def test_policy_command_gate_maps_terminal_strategy(
    monkeypatch, strategy, human, allowed, asked
):
    """CONFIRM 只在人類對這一條指令核准時放行（B2）；ALLOW／DENY 不問人."""
    seen: dict = {}
    asked_for: list[str] = []

    def _fake_classify(command, identity, auth_level, isolation=None):
        seen.update(command=command, identity=identity, auth_level=auth_level)
        return strategy

    async def _approver(cmd: str, **_card: object) -> str:
        asked_for.append(cmd)
        return "approved" if human else "rejected"

    monkeypatch.setattr(permission_module.permission_service, "classify_command", _fake_classify)

    gate = evidence.policy_command_gate(
        _OWNER, auth_level="Auto", approver=None if human is None else _approver
    )
    reason = await gate("pytest -q")

    assert (reason is None) is allowed
    assert asked_for == (["pytest -q"] if asked else [])
    assert seen == {"command": "pytest -q", "identity": _OWNER, "auth_level": "Auto"}


async def test_policy_command_gate_uses_real_terminal_policy():
    """真實政策：Auto 不再有白名單（舊 SafeAuto 會放行 pytest），metachar、黑名單恆拒。"""
    auto = evidence.policy_command_gate(_OWNER, auth_level="Auto")
    assert await auto("pytest -q") is not None
    assert await auto("python -m pytest") is not None
    assert await auto("pytest -q; curl http://evil.example") is not None

    manual = evidence.policy_command_gate(_OWNER, auth_level="Manual")
    assert await manual("pytest -q") is not None

    async def _always_yes(_cmd: str, **_card: object) -> str:
        return "approved"

    yolo = evidence.policy_command_gate(_OWNER, auth_level="YOLO", approver=_always_yes)
    assert await yolo(f'"{_PY}" -m pytest') is None
    assert await yolo("sudo ls") is not None


async def _board_with_verification_cmd(identity: AgentIdentity, cmd: str):
    manager = MissionManager()
    await manager.initialize_mission(
        identity=identity,
        goal="驗證政策",
        initial_tasks=[
            {
                "id": "t1",
                "description": "跑驗證",
                "review_required": True,
                "verification_cmd": cmd,
            }
        ],
    )
    await manager.update_task(identity=identity, task_id="t1", status=TaskStatus.DOING)
    return manager


async def test_review_round_applies_command_policy_fail_closed(zone_drive):
    """未帶 auth_level（預設 Manual）且無人在場（沒有 approver）→ verification_cmd 不得執行。"""
    identity = AgentIdentity.for_role(agent_id="SubprocSecReview", role="Owner")
    marker = zone_drive.child("ran.txt")  # 指令的 cwd 是個人空間
    cmd = f'"{_PY}" -c "open(\'ran.txt\', \'w\').write(\'x\')"'
    manager = await _board_with_verification_cmd(identity, cmd)
    state = await manager.get_current_mission(identity)

    outcome = await run_review_round(
        task=state.tasks[0],
        goal=state.goal,
        producer_claims="done",
        artifact_root=zone_drive,
        parent_identity=identity,
        mission_id=state.mission_id,
        manager=manager,
        completer=None,
        critic_model="critic",
        producer_model="producer",
    )

    assert mt.resource_manager.zone_kind(marker) == "missing"
    assert "REJECTED" in outcome.evidence_digest
    assert outcome.review_status != "APPROVED"


async def test_mission_tool_forwards_auth_context_to_review(monkeypatch):
    identity = AgentIdentity.for_role(agent_id="SubprocSecWire", role="Owner")
    await _board_with_verification_cmd(identity, "pytest -q")
    seen: dict = {}

    async def _fake_round(**kwargs):
        seen.update(kwargs)
        return ReviewOutcome("APPROVED", None, "", "ok")

    monkeypatch.setattr("src.services.review.review_loop.run_review_round", _fake_round)

    async def _emit(_event: dict) -> None:
        return None

    # 舊預核准旗標不再透傳（B2）；無人在場＝沒有 approver
    await mt.mission_tools(
        action="request_review",
        task_id="t1",
        identity=identity,
        auth_level="Auto",
        _hitl_preapproved=True,
        _ctx=None,
    )
    assert (seen["auth_level"], seen["approver"]) == ("Auto", None)
    assert "hitl_preapproved" not in seen

    seen.clear()
    await mt.mission_tools(action="request_review", task_id="t1", identity=identity, _ctx=None)
    assert (seen["auth_level"], seen["approver"]) == ("Manual", None)

    seen.clear()
    await mt.mission_tools(
        action="request_review",
        task_id="t1",
        identity=identity,
        _interactive=True,
        _emit_event=_emit,
        _call_id="call-w",
        _ctx=None,
    )
    assert callable(seen["approver"])


def test_artifact_root_does_not_fall_back_to_process_cwd(monkeypatch):
    monkeypatch.setattr(mt.resource_manager, "agent_drive", lambda *_a, **_k: None)
    assert mt._agent_artifact_root(_OWNER) is None


async def test_request_review_fails_closed_without_vault(monkeypatch):
    identity = AgentIdentity.for_role(agent_id="SubprocSecNoVault", role="Owner")
    await _board_with_verification_cmd(identity, "pytest -q")
    called: list = []

    async def _fake_round(**kwargs):
        called.append(kwargs)
        return ReviewOutcome("APPROVED", None, "", "ok")

    monkeypatch.setattr("src.services.review.review_loop.run_review_round", _fake_round)
    monkeypatch.setattr(mt, "_agent_artifact_root", lambda _identity: None)

    out = str(
        await mt.mission_tools(action="request_review", task_id="t1", identity=identity, _ctx=None)
    )
    assert called == []
    assert out.startswith("Error")


# --------------------------------------------------------------------------- #
# SP2 execute_command
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("strategy", ["DENY", "BLOCK", "REJECT", ""])
async def test_execute_command_never_runs_non_allow_confirm_strategy(monkeypatch, strategy):
    spawned: list = []

    async def _fake_exec(*args, **_kwargs):
        spawned.append(args)
        return _HangingProc()

    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(terminal, "_classify_command_strategy", lambda *_a, **_k: strategy)

    result = await terminal.execute_command(
        command="sudo rm -rf /x", identity=_OWNER, auth_level="YOLO"
    )

    assert spawned == []
    assert json.loads(result)["error_class"] == "permission_denied"


async def test_execute_core_cancellation_kills_process_tree(tmp_path, monkeypatch):
    reading = asyncio.Event()

    class _HangingStdout:
        async def readline(self):
            reading.set()
            await asyncio.Event().wait()

    proc = _HangingProc()
    proc.stdout = _HangingStdout()
    killed: list = []

    async def _fake_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(terminal, "terminate_process_tree", killed.append)

    task = asyncio.create_task(
        terminal._execute_core("sleep 100", "powershell", tmp_path, "sp2-session")
    )
    await asyncio.wait_for(reading.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [proc]


# --------------------------------------------------------------------------- #
# SP4 還原
# --------------------------------------------------------------------------- #


def test_restore_uses_current_interpreter_without_timeout(tmp_path, monkeypatch):
    """還原不設硬逾時：逐檔就地覆寫的還原中途被殺，資料會一半新一半舊。"""
    seen: dict = {}
    exits: list = []

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(system_router.time, "sleep", lambda _s: None)
    monkeypatch.setattr(system_router, "run_sync", _fake_run)
    monkeypatch.setattr(system_router.os, "_exit", exits.append)

    system_router._execute_restore(tmp_path / "manage_backup.py", tmp_path / "b.zip")

    assert seen["cmd"][0] == sys.executable
    assert "timeout" not in seen["kwargs"]
    assert seen["kwargs"]["check"] is True
    assert exits == [0]


def test_restore_failure_does_not_exit_process(tmp_path, monkeypatch):
    exits: list = []

    def _fail(cmd, **_kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(system_router.time, "sleep", lambda _s: None)
    monkeypatch.setattr(system_router, "run_sync", _fail)
    monkeypatch.setattr(system_router.os, "_exit", exits.append)

    system_router._execute_restore(tmp_path / "manage_backup.py", tmp_path / "b.zip")

    assert exits == []


# --------------------------------------------------------------------------- #
# SP5 agent reset 備份
# --------------------------------------------------------------------------- #


async def test_reset_backup_holds_lock_and_leaves_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen: dict = {}

    def _fake_backup() -> dict:
        seen["locked"] = system_backup.backup_execution_lock.locked()
        seen["thread"] = threading.get_ident()
        return {"status": "success", "message": "ok", "output": "x.zip"}

    monkeypatch.setattr(system_backup, "create_backup", _fake_backup)
    monkeypatch.setattr(orchestrator, "ALL_STORES", ())

    report = await orchestrator.reset_debug_agent_data()

    assert report.backup == {"status": "success", "message": "ok", "output": "x.zip"}
    assert seen["locked"] is True
    assert seen["thread"] != loop_thread
    assert system_backup.backup_execution_lock.locked() is False


# --------------------------------------------------------------------------- #
# SP6 演化沙箱
# --------------------------------------------------------------------------- #


def test_sandbox_caller_env_cannot_reintroduce_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(EvolutionPathGuard, "get_tool_root", classmethod(lambda _c, _s: tmp_path))
    seen: dict = {}

    def _fake_run(cmd_list, **kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("src.services.evolution_security.run_sync", _fake_run)

    EvolutionPathGuard.run_sandbox_command(
        "skill_sp6",
        ["python", "x.py"],
        env={"TOUKA_AUTH_SECRET": "leak", "LD_PRELOAD": "evil.so", "MY_FLAG": "1"},
        launch=passthrough_launch("skill_sp6"),
    )

    env = seen["env"]
    assert "TOUKA_AUTH_SECRET" not in env
    assert "LD_PRELOAD" not in env
    assert env["MY_FLAG"] == "1"
    assert env["PYTHONPATH"] == "."


async def test_environment_setup_has_timeouts_and_leaves_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    calls: list = []

    def _fake_sandbox(_skill_id, cmd_list, **kwargs):
        calls.append({"cmd": cmd_list, "kwargs": kwargs, "thread": threading.get_ident()})
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(evolution.EvolutionPathGuard, "write_tool_file", lambda *_a, **_k: None)
    monkeypatch.setattr(evolution.EvolutionPathGuard, "tool_file_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(evolution.EvolutionPathGuard, "run_sandbox_command", _fake_sandbox)

    out = await evolution.write_mcp_implementation("skill_sp6", "print(1)", ["requests==2.32.3"])

    assert out.startswith("Development Success")
    assert calls[0]["cmd"][:3] == [sys.executable, "-m", "venv"]  # 不用 PATH 上的 python
    assert [c["kwargs"]["timeout"] for c in calls] == [
        evolution._VENV_CREATE_TIMEOUT_SECONDS,
        evolution._PIP_INSTALL_TIMEOUT_SECONDS,
    ]
    assert all(c["thread"] != loop_thread for c in calls)


async def test_local_verification_leaves_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen: dict = {}

    def _freeze(_skill_id):
        seen["thread"] = threading.get_ident()

    @asynccontextmanager
    async def transport(_config, timeout, _tail):
        seen["timeout"] = timeout
        # 協定等待時，事件迴圈仍能排程其他工作。
        callback = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(callback.set_result, True)
        assert await callback
        yield types.SimpleNamespace(session=types.SimpleNamespace(call_tool=AsyncMock(
            return_value=mcp_types.CallToolResult(content=[
                mcp_types.TextContent(type="text", text="ok"),
            ]),
        )))

    monkeypatch.setattr(evolution.EvolutionPathGuard, "write_tool_file", lambda *_a, **_k: None)
    monkeypatch.setattr(evolution.EvolutionPathGuard, "delete_tool_file", lambda *_a, **_k: True)
    monkeypatch.setattr(evolution_command, "refuse_deployed", _freeze)
    monkeypatch.setattr(evolution.EvolutionPathGuard, "is_windows", lambda: True)
    monkeypatch.setattr(evolution, "_windows_client", transport)

    out = await evolution.verify_mcp_locally("skill_sp6", "tool", {})

    assert "ok" in out
    assert seen["timeout"]
    assert seen["thread"] != loop_thread


# --------------------------------------------------------------------------- #
# SP7 Creation clean env（刻意不解析哨兵）
# --------------------------------------------------------------------------- #


def test_creation_clean_env_keeps_sentinels_unresolved(monkeypatch):
    """ADR-0198 §5：候選 server 只拿到 spec 明示值，不得經哨兵取得 app_data 根路徑。"""
    # iso-c1 缺口 2：base 改由白名單常數決定（不再注入 base factory）。
    monkeypatch.setattr(type(settings), "get_full_env", lambda _self: {"LANG": "p"})
    env = LaunchEnvironmentPolicy.clean({"DATA_DIR": APPDATA_SENTINEL})
    assert env == {"LANG": "p", "DATA_DIR": APPDATA_SENTINEL}
