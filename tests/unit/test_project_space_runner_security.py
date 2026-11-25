"""專用空間的專案指令：profile 與 runner 的最後一道（ADR-0332）.

- ``build_project_command_profile``：只在執行器位置；讀寫只有這個專案與它自己的
  ``project/<資料夾名>/{home,tmp}``，不給個人空間；名稱、位置要正好是錨點第一層；掃描不過標成
  沒授權（``ungranted``）。
- ``build_command_profile``：專用空間的專案一律走它；在 proxy 位置組不出來就拒絕，
  不會改用「未隔離」的不完整 profile（就算有降級同意）。
- ``RunnerBroker``：專用空間的專案只用執行器，有同意也不退回本機 Landlock 或直接執行；
  執行器位置組出的 profile 有沒授權的就拒絕（自建 MCP 等其他來源照舊）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.services.isolation import agent_command, profiles
from src.services.isolation.agent_command import CommandScope
from src.services.isolation.consent import ExecutionGrant
from src.services.isolation.profiles import PathRule, ProfileError
from src.services.isolation.runners import (
    ExecutorRunner,
    IsolationUnavailableError,
    ProfileSource,
)

from tests.unit.test_executor_runner_security import (
    ALL,
    STATE,
    _broker,
    _grant,
    _layout,
    _monitor,
    _ready,
    _Source,
)

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
# 全域的隔離替身把它換成直通（tests/support/isolation.py）；這裡要測真的
REAL_COMMAND_PROFILE = agent_command.build_command_profile
PORTS = (2375, 2376, 8000)


def _space_layout(tmp_path: Path) -> tuple[profiles.SandboxLayout, Path]:
    anchor = (tmp_path / "workspace" / "touka-projects").resolve()
    (anchor / "repo").mkdir(parents=True)
    layout = _layout(tmp_path)
    return profiles.SandboxLayout(**{**layout.__dict__, "projects_root": anchor}), anchor


@pytest.fixture
def scan(monkeypatch):
    verdicts: dict[str, str | None] = {"verdict": None}

    async def _authorize(_path, _layout, _owner=None):
        return verdicts["verdict"]

    monkeypatch.setattr(profiles, "authorize_derived_path", _authorize)
    return verdicts


async def test_the_project_profile_grants_only_the_project_and_its_own_state(tmp_path, scan):
    layout, anchor = _space_layout(tmp_path)
    profile = await profiles.build_project_command_profile(
        "repo", anchor / "repo", agent_id="Alice", layout=layout, location="executor",
        block_ports=PORTS, executor_state=STATE,
    )
    base = Path(STATE) / "project" / "repo"
    home, tmp = base / "home", base / "tmp"
    assert (profile.name, profile.owner) == ("project_command", "repo")
    assert profile.cwd == anchor / "repo"
    assert profile.project_root == anchor / "repo" and profile.ungranted == ()
    writable = {rule.path for rule in profile.rules if rule.access == "rwx"}
    assert writable == {anchor / "repo", home, tmp}
    assert not any("personal_drive" in str(rule.path) for rule in profile.rules)
    assert profile.env_set["HOME"] == str(home) and profile.block_ports == PORTS


async def test_a_failed_scan_marks_the_project_profile_ungranted(tmp_path, scan):
    layout, anchor = _space_layout(tmp_path)
    scan["verdict"] = "scan_limit"
    profile = await profiles.build_project_command_profile(
        "repo", anchor / "repo", agent_id="Alice", layout=layout, location="executor",
        block_ports=PORTS, executor_state=STATE,
    )
    assert profile.ungranted == ("scan_limit",)
    assert PathRule(anchor / "repo", "rwx") not in profile.rules


@pytest.mark.parametrize(
    ("name", "relative", "location", "space"),
    [
        ("repo", "repo", "proxy", True),
        ("repo", "other", "executor", True),
        ("repo", "repo/src", "executor", True),
        ("..", "..", "executor", True),
        (None, "repo", "executor", True),
        ("repo", None, "executor", True),
        ("repo", "repo", "executor", False),
    ],
)
async def test_the_project_profile_is_refused_unless_everything_lines_up(
    tmp_path, scan, name, relative, location, space
):
    layout, anchor = _space_layout(tmp_path)
    if not space:
        layout = profiles.SandboxLayout(**{**layout.__dict__, "projects_root": None})
    path = anchor / relative if relative is not None else None
    with pytest.raises(ProfileError):
        await profiles.build_project_command_profile(
            name, path, agent_id="Alice", layout=layout, location=location,
            block_ports=PORTS, executor_state=STATE,
        )


def _wire(monkeypatch, layout: profiles.SandboxLayout) -> None:
    monkeypatch.setattr(agent_command, "current_layout", lambda: layout)
    monkeypatch.setattr(agent_command, "build_command_profile", REAL_COMMAND_PROFILE)
    monkeypatch.setattr(agent_command.isolation_monitor, "block_ports", lambda _location: PORTS)


async def test_build_command_profile_uses_the_project_profile_for_dedicated_projects(
    tmp_path, scan, monkeypatch
):
    layout, anchor = _space_layout(tmp_path)
    _wire(monkeypatch, layout)
    scope = CommandScope(ALICE, ExecutionGrant("executor"), anchor / "repo")
    profile = await agent_command.build_command_profile(scope, anchor / "repo", "executor", STATE)
    assert profile.name == "project_command" and profile.cwd == anchor / "repo"


@pytest.mark.parametrize("fallback", ["direct", "landlock"])
async def test_dedicated_projects_never_get_the_unisolated_profile(
    tmp_path, scan, monkeypatch, fallback
):
    layout, anchor = _space_layout(tmp_path)
    _wire(monkeypatch, layout)
    scope = CommandScope(ALICE, _grant(fallback), anchor / "repo")
    with pytest.raises(IsolationUnavailableError):
        await agent_command.build_command_profile(scope, anchor / "repo", "proxy")


async def test_prepare_command_marks_dedicated_projects_for_the_broker(
    tmp_path, scan, monkeypatch
):
    layout, anchor = _space_layout(tmp_path)
    _wire(monkeypatch, layout)
    seen: list[ProfileSource] = []

    async def _acquire(_grant: Any, source: ProfileSource) -> Any:
        seen.append(source)
        raise IsolationUnavailableError("stop_here")

    monkeypatch.setattr(agent_command.runner_broker, "acquire", _acquire)
    monkeypatch.setattr(agent_command.isolation_monitor, "executor_scope_issue", lambda *_: None)
    scope = CommandScope(ALICE, ExecutionGrant("executor"), anchor / "repo")
    with pytest.raises(IsolationUnavailableError):
        await agent_command.prepare_command(scope, ["git", "status"], {}, anchor / "repo")
    assert len(seen) == 1 and seen[0].dedicated and seen[0].executor_issue is None
    outside = CommandScope(ALICE, ExecutionGrant("executor"), None)
    with pytest.raises(IsolationUnavailableError):
        await agent_command.prepare_command(outside, ["ls"], {}, tmp_path)
    assert seen[1].dedicated is False


class _Dedicated(_Source):
    def source(self) -> ProfileSource:
        return ProfileSource(self, executor_issue=self.issue, dedicated=True)


async def test_a_dedicated_project_with_a_card_still_uses_only_the_executor(tmp_path):
    monitor = _monitor(ready=_ready(), executor=ALL)
    source = _Dedicated(tmp_path)
    acquired = await _broker(tmp_path, monitor).acquire(_grant("direct"), source.source())
    assert isinstance(acquired.runner, ExecutorRunner)
    assert source.built == [("executor", STATE)]


@pytest.mark.parametrize("fallback", ["direct", "landlock"])
async def test_a_dedicated_project_never_falls_back_when_the_executor_is_gone(tmp_path, fallback):
    monitor = _monitor(local_status="available")  # 本機 Landlock 可用，也不退回
    source = _Dedicated(tmp_path)
    with pytest.raises(IsolationUnavailableError):
        await _broker(tmp_path, monitor).acquire(_grant(fallback), source.source())
    assert source.built == []


async def test_an_ungranted_dedicated_profile_is_refused_in_the_executor(tmp_path):
    monitor = _monitor("executor", ready=_ready(), executor=ALL)
    source = _Dedicated(tmp_path, ungranted=("scan_limit",))
    with pytest.raises(IsolationUnavailableError) as raised:
        await _broker(tmp_path, monitor).acquire(ExecutionGrant("executor"), source.source())
    assert raised.value.reason == "scan_limit"


async def test_other_sources_with_ungranted_rules_still_start_in_the_executor(tmp_path):
    """自建 MCP 引用多個技能時帶 ``CODE_DIR_REFUSED``，照樣在執行器啟動（行為不變）."""
    monitor = _monitor("executor", ready=_ready(), executor=ALL)
    source = _Source(tmp_path, ungranted=("code_dir_refused",))
    acquired = await _broker(tmp_path, monitor).acquire(
        ExecutionGrant("executor"), source.source()
    )
    assert isinstance(acquired.runner, ExecutorRunner)
