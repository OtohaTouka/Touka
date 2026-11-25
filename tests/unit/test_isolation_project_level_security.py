"""專用空間的執行層（ADR-0332）：``level_for`` 與 ``executor_scope_issue``.

結構層 ``ok`` 之後：快照要是執行器層（不呼叫一般的範圍判定）、READY 的 ``project_root`` 要等於
錨點、掃描與 ``authorize_derived_path`` 要過、有人在場，才是 executor。任何一項不過都是
``refused``（直接拒絕、不跳卡、不降級），只有「沒人在場」照今天的規則（none，交給沒人在場的
拒絕）。READY 不符只影響專用空間的專案，其他代理人的指令照舊在執行器。範圍判定不看代理人資料夾。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from src.models.identity import AgentIdentity
from src.services.isolation import project_space as ps
from src.services.isolation import status as status_module
from src.services.isolation.status import LevelDecision
from src.services.resource_manager import resource_manager

from tests.unit.test_isolation_status import _executor_monitor

SOUL = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
CUSTOM = AgentIdentity.for_role("gpt-x", "Base", capabilities=[])


@pytest.fixture
def scans(monkeypatch):
    """``authorize_derived_path`` 換成記錄呼叫、回 ``scans.verdict`` 的假的."""

    class Scans:
        verdict: str | None = None
        calls: list[tuple[Path, str | None]] = []

    record = Scans()
    record.calls = []

    async def _authorize(path, _layout, owner=None):
        record.calls.append((path, owner))
        return record.verdict

    monkeypatch.setattr(status_module, "authorize_derived_path", _authorize)
    return record


def _space(tmp_path: Path) -> Path:
    anchor = (tmp_path / "workspace" / "touka-projects").resolve()
    (anchor / "repo" / "src").mkdir(parents=True)
    return anchor


async def _monitor(tmp_path: Path, *, reported: object = "anchor", **executor_overrides):
    anchor = _space(tmp_path)
    data = Path(resource_manager.get_base_path()).resolve()
    project_root = str(anchor) if reported == "anchor" else reported
    monitor, executor, layout = _executor_monitor(
        tmp_path, data_root=data, projects_root=anchor
    )
    executor.overrides = {"project_root": project_root, **executor_overrides}
    assert (await monitor.ensure_ready()).level == "executor"
    return monitor, anchor


async def test_an_attended_dedicated_project_runs_in_the_executor(tmp_path, scans):
    monitor, anchor = await _monitor(tmp_path)
    decision = await monitor.level_for(SOUL, anchor / "repo", attended=True)
    assert decision == LevelDecision("executor") and not decision.refused
    assert scans.calls == [(anchor / "repo", "Alice")]
    # 範圍判定不看代理人資料夾：custom 模型的資料夾不在執行器，也照樣在專案裡跑
    assert await monitor.level_for(CUSTOM, anchor / "repo", attended=True) == LevelDecision(
        "executor"
    )


async def test_without_a_human_the_dedicated_project_keeps_todays_unattended_rule(
    tmp_path, scans
):
    monitor, anchor = await _monitor(tmp_path)
    decision = await monitor.level_for(SOUL, anchor / "repo")
    assert decision == LevelDecision("none", status_module.PROJECT_UNATTENDED)
    assert decision.refused is False


@pytest.mark.parametrize(
    ("relative", "reason"),
    [("", ps.PROJECT_SPACE_ROOT), ("repo/src", ps.PROJECT_NOT_TOP_LEVEL)],
)
async def test_structural_refusals_are_refused_before_anything_else(
    tmp_path, scans, relative, reason
):
    monitor, anchor = await _monitor(tmp_path)
    target = anchor / relative if relative else anchor
    decision = await monitor.level_for(SOUL, target, attended=True)
    assert decision == LevelDecision("none", reason, refused=True)
    assert scans.calls == []


@pytest.mark.parametrize(
    ("reported", "reason"),
    [
        (None, status_module.EXECUTOR_NO_PROJECT_ROOT),
        ("/elsewhere/touka-projects", status_module.EXECUTOR_PROJECT_ROOT_MISMATCH),
    ],
)
async def test_a_ready_without_the_same_project_root_refuses_only_dedicated_projects(
    tmp_path, scans, reported, reason
):
    monitor, anchor = await _monitor(tmp_path, reported=reported)
    decision = await monitor.level_for(SOUL, anchor / "repo", attended=True)
    assert decision == LevelDecision("none", reason, refused=True)
    assert await monitor.level_for(SOUL, None) == LevelDecision("executor")
    assert monitor.snapshot().level == "executor"


@pytest.mark.parametrize("verdict", ["scan_limit", "probe_pending", "protected_location"])
async def test_a_failed_scan_refuses_the_dedicated_project(tmp_path, scans, verdict):
    monitor, anchor = await _monitor(tmp_path)
    scans.verdict = verdict
    decision = await monitor.level_for(SOUL, anchor / "repo", attended=True)
    assert decision == LevelDecision("none", verdict, refused=True)


async def test_without_the_executor_layer_the_dedicated_project_is_refused(tmp_path, scans):
    monitor, anchor = await _monitor(tmp_path)
    monitor.report_failure(sorted(status_module.RELAY_FAILURE_CODES)[0])  # relay 回報執行器壞了
    snap = monitor.snapshot()
    assert snap.level == "none"
    decision = await monitor.level_for(SOUL, anchor / "repo", attended=True)
    assert decision.refused and decision.level == "none"
    assert decision.reason == snap.reason
    assert scans.calls == []


async def test_executor_scope_for_dedicated_projects_ignores_the_agent_folder(tmp_path, scans):
    monitor, anchor = await _monitor(tmp_path)
    assert monitor.executor_scope_issue(CUSTOM, anchor / "repo") is None
    assert monitor.executor_scope_issue(SOUL, anchor / "repo") is None
    assert monitor.executor_scope_issue(SOUL, anchor / "repo" / "src") == ps.PROJECT_NOT_TOP_LEVEL
    outside = tmp_path / "work"
    outside.mkdir()
    assert monitor.executor_scope_issue(SOUL, outside) == "project_not_in_executor"
    assert monitor.executor_scope_issue(CUSTOM, outside) == "agent_dir_not_in_executor"


async def test_projects_outside_the_space_are_unchanged(tmp_path, scans):
    monitor, _anchor = await _monitor(tmp_path)
    outside = tmp_path / "work"
    outside.mkdir()
    decision = await monitor.level_for(SOUL, outside, attended=True)
    assert decision == LevelDecision("none", "project_not_in_executor")
    assert decision.refused is False
