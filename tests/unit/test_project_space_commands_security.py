"""專用空間的專案指令從哪裡開始（ADR-0332）.

- 終端（前景、背景）與互審的驗證指令：START 的 cwd 是專案根、kind ``project``、owner 是資料夾
  名稱；本機 relay 從 kernel 資料夾啟動，不 chdir 進代理人寫得到的資料夾（禁令 68 ③，同 kind mcp）。
- 「這次有人在場」一路帶到層級：終端看 ``is_human_present(ctx)``；互審看有沒有人能回卡
  （``review_loop`` 只在有人時給 approver 與 consent）。
- 互審的驗證指令：專用空間被拒絕（``refused``）時回 ``NO_ISOLATED_ENVIRONMENT``（不問人、不送審、
  不計次數）。
- 專案說明：專用空間第一層的專案多一句「指令從這裡開始、用相對路徑」；其他專案、被拒絕的專案不變。
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from src.middleware.layers import context as context_layer
from src.middleware.layers.context import ContextLayer
from src.models.context import PipelineContext
from src.sandbox_kernel.spec import kernel_digest
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation import agent_command, profiles
from src.services.isolation.executor_session import PROJECT_ROOT_NAME
from src.services.isolation.status import LevelDecision, isolation_monitor
from src.services.resource_manager import resource_manager
from src.services.review import evidence, review_loop
from src.tools import terminal

from tests.support.isolation import install_real_broker, pinned_snapshot
from tests.support.zone_roots import point_zone_roots
from tests.unit.test_executor_runner_security import (
    ALICE,
    ALL,
    REAL_COMMAND_PROFILE,
    _DoneProc,
    _layout,
    _plan,
    _ready,
    _request,
)


class _Spawn:
    """``spawn_exec`` 的替身：記下本機 cwd 與 START."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, dict[str, Any]]] = []

    async def __call__(self, *argv: Any, **kwargs: Any) -> Any:
        fd = int(argv[argv.index("--request-fd") + 1])
        self.calls.append((kwargs.get("cwd"), json.loads(os.read(fd, 1 << 20))["start"]))
        return _DoneProc(b"ok\n", 0)


class _Space:
    def __init__(self, anchor: Path, kernel: Path, spawn: _Spawn) -> None:
        self.anchor, self.kernel, self.spawn = anchor, kernel, spawn
        self.attended: list[bool] = []

    @property
    def repo(self) -> Path:
        return self.anchor / "repo"


async def _scan_ok(_path, _layout, _owner=None):
    return None


@pytest.fixture
def space(monkeypatch, tmp_path) -> _Space:
    anchor = (tmp_path / "workspace" / PROJECT_ROOT_NAME).resolve()
    (anchor / "repo").mkdir(parents=True)
    layout = replace(_layout(tmp_path), in_container=False, projects_root=anchor)
    point_zone_roots(monkeypatch, layout.data_root, layout.state_root)  # 個人空間（ADR-0293）
    (layout.data_root / "personal_drive" / "Alice").mkdir(parents=True)
    ready = _ready(project_root=str(anchor))
    snap = replace(pinned_snapshot("executor"), executor=ready, features=ALL)
    monitor = install_real_broker(monkeypatch, snap, layout)
    monitor.checked_kernel_digest = kernel_digest(layout.kernel_dir)
    spawn = _Spawn()
    result = _Space(anchor, layout.kernel_dir, spawn)

    async def _level_for(_identity, _root, *, attended: bool = False) -> LevelDecision:
        result.attended.append(attended)
        return LevelDecision("executor")

    async def _root(_ctx, _session_id):
        return anchor / "repo"

    monkeypatch.setattr(isolation_monitor, "level_for", _level_for)
    monkeypatch.setattr(isolation_monitor, "executor_scope_issue", lambda *_: None)
    monkeypatch.setattr(agent_command, "build_command_profile", REAL_COMMAND_PROFILE)
    monkeypatch.setattr(agent_command, "current_layout", lambda: layout)
    monkeypatch.setattr(profiles, "authorize_derived_path", _scan_ok)
    monkeypatch.setattr(EvolutionPathGuard, "get_clean_env", lambda: {"PATH": "/usr/bin"})
    monkeypatch.setattr(terminal, "session_project_root", _root)
    monkeypatch.setattr(terminal, "spawn_exec", spawn)
    monkeypatch.setattr(evidence, "spawn_exec", spawn)
    return result


def _assert_started_in_the_project(space: _Space) -> None:
    ((local_cwd, start),) = space.spawn.calls
    assert (start["cwd"], start["kind"], start["owner"]) == (str(space.repo), "project", "repo")
    assert local_cwd == str(space.kernel)


# --- 本機 relay 的 cwd ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "local"), [("project_command", "kernel"), ("agent_command", "profile")]
)
def test_the_relay_for_a_project_starts_from_the_kernel_folder(tmp_path, name, local):
    project = tmp_path / "workspace" / PROJECT_ROOT_NAME / "repo"
    _runner, plan = _plan(tmp_path, name=name, cwd=project, owner="repo")
    try:
        start = _request(plan)["start"]
    finally:
        plan.release()
        plan.read_status()
    assert start["cwd"] == str(project)  # 執行器裡的起點照 profile
    assert plan.cwd == (tmp_path / "kernel" if local == "kernel" else project)


# --- 終端：前景、背景 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ctx", "attended"),
    [
        ({"resumable_stream": True}, True),
        ({"resumable_stream": True, "heartbeat_run": True}, False),
    ],
)
async def test_a_foreground_command_starts_in_the_project(space, ctx, attended):
    result = await terminal.execute_command(
        "git status", ALICE, auth_level="Auto", _hitl_preapproved=True, session_id="s1",
        _call_id="c1", _ctx={"identity": ALICE, **ctx},
    )
    assert "[EXIT_CODE: 0]" in result
    _assert_started_in_the_project(space)
    assert space.attended == [attended]


async def test_a_background_command_starts_in_the_project(space):
    handle = json.loads(
        await terminal.execute_command(
            "pytest -q", ALICE, background=True, auth_level="Auto", _hitl_preapproved=True,
            session_id="s1", _call_id="c2", _ctx={"identity": ALICE, "resumable_stream": True},
        )
    )
    await terminal._ASYNC_RESULTS[handle["execution_id"]]["_task"]
    _assert_started_in_the_project(space)


# --- 互審的驗證指令 -------------------------------------------------------------------------


async def _allow(_cmd: str) -> None:
    return None


@pytest.mark.parametrize("attended", [True, False])
async def test_a_verification_command_starts_in_the_project(space, attended):
    bundle = await evidence.collect_evidence(
        artifact_refs=[], verification_cmd="pytest -q", test_log=None,
        artifact_root=resource_manager.agent_drive(ALICE), command_gate=_allow,
        identity=ALICE, project_root=space.repo,
        consent=(lambda _cmd: None) if attended else None,
    )
    assert bundle.cmd_results[0].exit_code == 0
    _assert_started_in_the_project(space)
    assert space.attended == [attended]


class _Approver:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def __call__(self, cmd: str, **_kw: Any) -> str:
        self.asked.append(cmd)
        return "approved"

    def take_consent(self, _cmd: str) -> None:
        return None


@pytest.mark.parametrize("mode", ["Manual", "Auto", "YOLO"])
async def test_a_refused_project_never_asks_and_is_not_run(monkeypatch, mode):
    seen: list[bool] = []

    async def _refused(_identity, _root, *, attended: bool = False) -> LevelDecision:
        seen.append(attended)
        return LevelDecision("none", "project_linked", refused=True)

    async def _ready_now():
        return None

    monkeypatch.setattr(isolation_monitor, "ensure_ready", _ready_now)
    monkeypatch.setattr(isolation_monitor, "level_for", _refused)
    approver = _Approver()
    gate = evidence.policy_command_gate(
        ALICE, auth_level=mode, approver=approver, project_root=Path("/p/touka-projects/repo")
    )
    assert await gate("rm -rf build") == evidence.NO_ISOLATED_ENVIRONMENT
    assert approver.asked == [] and seen == [True]  # 有 approver＝有人在場


def test_no_isolated_environment_is_a_human_gate():
    """回這個原因時 review_loop 不送審、不計次數、不上鎖（既有規則，這裡釘住）."""
    assert evidence.NO_ISOLATED_ENVIRONMENT in review_loop._HUMAN_GATE


# --- 專案說明 -------------------------------------------------------------------------------


async def _project_text(monkeypatch, tmp_path, root: Path | None) -> str:
    anchor = (tmp_path / PROJECT_ROOT_NAME).resolve()
    (anchor / "repo" / "src").mkdir(parents=True)

    async def _path(_project_id):
        return None if root is None else str(anchor / root)

    jailed = AsyncMock(side_effect=AssertionError)  # 只讀登記的根，不經工作區檢查（不每輪記警告）

    monkeypatch.setattr(context_layer, "get_project_context", AsyncMock(return_value="[P: repo]"))
    monkeypatch.setattr(context_layer, "get_project_root_path", _path)
    monkeypatch.setattr(context_layer.resource_manager, "get_project_path", jailed)
    monkeypatch.setattr(context_layer, "configured_projects_root", lambda: anchor)
    request = SimpleNamespace(
        model="m", project_id="p1", ide_context=None, messages=[{"role": "user", "content": "hi"}]
    )
    await ContextLayer().process(request, PipelineContext(project_authorized=True, identity=ALICE))
    return " ".join(
        str(m.get("content", "") if isinstance(m, dict) else getattr(m, "content", ""))
        for m in request.messages
    )


@pytest.mark.parametrize(
    ("root", "starts_here"),
    [(Path("repo"), True), (Path("repo/src"), False), (Path(".."), False), (None, False)],
)
async def test_only_dedicated_projects_say_commands_start_here(
    monkeypatch, tmp_path, root, starts_here
):
    text = await _project_text(monkeypatch, tmp_path, root)
    assert "[P: repo]" in text
    assert ("use paths relative to it" in text) is starts_here
    # 每輪說明寫明被擋時直接拒絕、不跳卡（一頁計畫）
    assert ("refused outright (no approval card" in text) is starts_here
