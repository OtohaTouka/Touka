"""專案根只有一個來源（iso-c2 U1c：``session_project_root`` 與政策層級部分）.

專案根只來自 ``ctx["project_id"]`` 或 session 綁定的專案；模型給的 ``args``（含
``project_id``、``human_present``、``isolation``、``_hitl_preapproved``）改不動層級與授權。
終端與互審驗證指令建 profile 時也只用 session 的專案，不用 ``project_id`` kwarg（U1e）。
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import pytest
from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity
from src.services.isolation import agent_command
from src.services.isolation import policy as policy_module
from src.services.isolation.consent import (
    ConsentKey,
    PolicyRecord,
    consent_ledger,
    consent_subject,
)
from src.services.isolation.policy import policy_level, session_project_root
from src.services.isolation.profiles import SandboxProfile
from src.services.isolation.status import LevelDecision, isolation_monitor
from src.services.resource_manager import resource_manager
from src.services.review import evidence
from src.services.review.budget import _reset_review_ledger
from src.tools import mission_tools, terminal
from src.utils.async_db import session_service

from tests.support.isolation import pinned_snapshot

if TYPE_CHECKING:
    from pathlib import Path

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
NOISY_ARGS = {
    "command": "pytest -q",
    "project_id": "attacker-project",
    "human_present": True,
    "isolation": "executor",
    "_hitl_preapproved": True,
}


@pytest.fixture
def projects(monkeypatch, tmp_path):
    roots = {"bound": tmp_path / "bound", "ctx": tmp_path / "ctx", "attacker-project": tmp_path}
    lookups: list[tuple[str, AgentIdentity | None]] = []

    async def _project_path(project_id: str, identity: AgentIdentity | None = None) -> str | None:
        lookups.append((project_id, identity))
        root = roots.get(project_id)
        return str(root) if root else None

    async def _session(session_id: str) -> dict | None:
        return {"id": session_id, "project_id": "bound"} if session_id == "s1" else None

    monkeypatch.setattr(resource_manager, "get_project_path", _project_path)
    monkeypatch.setattr(session_service, "get_session_by_id", _session)
    return roots, lookups


@pytest.fixture
def level_calls(monkeypatch):
    calls: list[Path | None] = []

    async def _ready():
        return isolation_monitor.snapshot()

    async def _level_for(_identity, project_root):
        calls.append(project_root)
        if project_root is not None and project_root.name == "bound":
            return LevelDecision("none", "protected_location")
        return LevelDecision("landlock")

    monkeypatch.setattr(isolation_monitor, "snapshot", lambda: pinned_snapshot("landlock"))
    monkeypatch.setattr(isolation_monitor, "ensure_ready", _ready)
    monkeypatch.setattr(isolation_monitor, "level_for", _level_for)
    yield calls
    consent_ledger.clear()


def test_session_project_root_takes_no_tool_arguments():
    assert list(inspect.signature(session_project_root).parameters) == ["ctx", "session_id"]


async def test_session_bound_project_is_used(projects):
    roots, lookups = projects
    assert await session_project_root({"identity": ALICE}, "s1") == roots["bound"]
    assert lookups == [("bound", ALICE)]


async def test_ctx_project_wins_over_session(projects):
    roots, _ = projects
    ctx = {"identity": ALICE, "project_id": "ctx"}
    assert await session_project_root(ctx, "s1") == roots["ctx"]


async def test_no_project_bound_is_none(projects):
    assert await session_project_root({"identity": ALICE}, "other") is None
    assert await session_project_root({"identity": ALICE}, None) is None


async def test_policy_level_ignores_model_controlled_args(projects, level_calls):
    roots, _ = projects
    clean = await policy_level(
        ALICE, {"identity": ALICE}, "s1", "c1", tool="execute_command",
        args={"command": "pytest -q"},
    )
    noisy = await policy_level(
        ALICE, {"identity": ALICE}, "s1", "c2", tool="execute_command", args=NOISY_ARGS
    )
    assert clean == noisy == LevelDecision("none", "protected_location")
    assert level_calls == [roots["bound"], roots["bound"]]


async def test_policy_level_records_normalized_subject(projects, level_calls):
    await policy_level(ALICE, {"identity": ALICE}, "s1", "c9", tool="bash", args=NOISY_ARGS)
    key = ConsentKey.of(ALICE, "s1", "c9")
    assert consent_ledger.grant(key, fallback="direct") is True
    subject = consent_subject("execute_command", {"command": "pytest -q"})
    grant = consent_ledger.take(key, subject=subject)
    assert grant is not None and grant.policy_level == "none"


async def test_policy_level_records_level_reason_and_session_project(projects, level_calls):
    """審批卡從帳本取層級、原因與專案根（算降級去處）；專案根同樣只來自 session。"""
    roots, _ = projects
    await policy_level(
        ALICE, {"identity": ALICE}, "s1", "c10", tool="execute_command", args=NOISY_ARGS
    )
    record = consent_ledger.pending(ConsentKey.of(ALICE, "s1", "c10"))
    assert record == PolicyRecord("none", "protected_location", roots["bound"])


async def test_isolation_tools_without_project_use_no_project_root(projects, level_calls):
    _, lookups = projects
    # 已接上 runner 的隔離必要工具（create_mcp 還沒接，一律 none）
    decision = await policy_level(
        ALICE, {"identity": ALICE}, "s1", "c3", tool="evolution_implement_and_verify",
        args={"skill_id": "x", "code": "print(1)", "project_id": "attacker-project"},
    )
    assert decision == LevelDecision("landlock")
    assert level_calls == [None]
    assert lookups == []


async def test_non_isolation_tool_is_not_recorded(projects, level_calls):
    decision = await policy_level(
        ALICE, {"identity": ALICE}, "s1", "c4", tool="vault_read", args={"path": "a"}
    )
    assert decision.level == "landlock"
    assert len(consent_ledger) == 0
    assert level_calls == []


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
def profile_roots(monkeypatch) -> list[Path | None]:
    """記錄建 profile 時用的專案根（spawn 換成替身）。"""
    seen: list[Path | None] = []

    async def _profile(scope, cwd, _location="proxy", _executor_state=None):
        seen.append(scope.project_root)
        return SandboxProfile(name="agent_command", cwd=cwd, rules=(), block_ports=())

    async def _spawn(*_argv, **_kwargs):
        return _DoneProc()

    monkeypatch.setattr(agent_command, "build_command_profile", _profile)
    monkeypatch.setattr(terminal, "spawn_exec", _spawn)
    monkeypatch.setattr(evidence, "spawn_exec", _spawn)
    return seen


async def test_terminal_uses_the_session_project_not_the_project_id_kwarg(
    projects, level_calls, profile_roots
):
    roots, _ = projects
    result = await terminal.execute_command(
        "pytest -q", ALICE, auth_level="Manual", _hitl_preapproved=True, session_id="s1",
        _call_id="c5", _ctx={"identity": ALICE}, project_id="attacker-project",
    )
    assert "[EXIT_CODE: 0]" in result
    assert profile_roots == [roots["bound"]]
    assert level_calls == [roots["bound"]]


async def test_verification_command_uses_the_session_project_not_the_kwarg(
    projects, level_calls, profile_roots, monkeypatch, zone_data_root
):
    roots, _ = projects

    async def _isolatable(_identity, project_root):
        # 驗證指令的分類也看專案層級：要讓指令跑到建 profile，這裡的專案得放得進隔離
        level_calls.append(project_root)
        return LevelDecision("landlock")

    monkeypatch.setattr(isolation_monitor, "level_for", _isolatable)
    _reset_review_ledger()
    manager = MissionManager()
    await manager.initialize_mission(
        identity=ALICE,
        goal="verify",
        initial_tasks=[
            {"id": "t1", "description": "run", "review_required": True,
             "verification_cmd": "pytest -q"}
        ],
    )
    await manager.update_task(identity=ALICE, task_id="t1", status=TaskStatus.DOING)
    await mission_tools.mission_tools(
        action="request_review", task_id="t1", identity=ALICE, auth_level="YOLO",
        session_id="s1", _ctx={"identity": ALICE}, project_id="attacker-project",
    )
    _reset_review_ledger()
    assert profile_roots == [roots["bound"]]
    assert set(level_calls) == {roots["bound"]}


def test_policy_module_reads_no_args_project():
    source = inspect.getsource(policy_module)
    assert 'args.get("project_id")' not in source and "args['project_id']" not in source
