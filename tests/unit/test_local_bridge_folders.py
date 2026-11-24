"""小幫手回報的開放名單與版本不符（ADR-0330）：只放記憶體、分得出新舊、過期就不顯示。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from src.models.local_bridge import PROTOCOL_VERSION, LocalFolderReport
from src.services.local_bridge import (
    FOLDER_REPORT_MAX_AGE_SECONDS,
    ONLINE_WINDOW_SECONDS,
    LocalBridge,
)

GENERATION = 1
FIRST = "a" * 22
SECOND = "b" * 22


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def bridge(clock: _Clock) -> LocalBridge:
    return LocalBridge(clock=clock)


def _poll(bridge: LocalBridge, generation: int = GENERATION) -> None:
    asyncio.run(bridge.poll(generation, "0.5.0", hold=0))


def _report(
    instance: str = FIRST, revision: int = 0, names: tuple[str, ...] = ("Open",), **extra: Any
) -> LocalFolderReport:
    return LocalFolderReport(
        protocol=PROTOCOL_VERSION,
        instance=instance,
        revision=revision,
        exposed=extra.get("exposed", False),
        folders=[
            {"name": name, "path": f"Notes/{name}", "writable": False, "usable": True}
            for name in names
        ],
        omitted=extra.get("omitted", 0),
    )


def _names(bridge: LocalBridge) -> list[str] | None:
    listed = bridge.folder_list()
    return None if listed.folders is None else [entry.name for entry in listed.folders]


def test_nothing_is_shown_while_the_helper_is_offline(bridge: LocalBridge) -> None:
    assert bridge.folder_list().state == "offline"
    _poll(bridge)
    bridge.accept_report(GENERATION, _report())
    assert bridge.folder_list().state == "ok"


def test_online_without_a_report_is_waiting(bridge: LocalBridge) -> None:
    _poll(bridge)
    listed = bridge.folder_list()
    assert listed.state == "waiting"
    assert listed.folders is None


def test_a_report_is_listed_with_its_flags(bridge: LocalBridge) -> None:
    _poll(bridge)
    bridge.accept_report(GENERATION, _report(exposed=True, omitted=2))
    listed = bridge.folder_list()
    assert listed.state == "ok"
    assert listed.exposed is True
    assert listed.omitted == 2
    assert _names(bridge) == ["Open"]


def test_an_old_report_is_not_shown(bridge: LocalBridge, clock: _Clock) -> None:
    _poll(bridge)
    bridge.accept_report(GENERATION, _report())
    clock.now += FOLDER_REPORT_MAX_AGE_SECONDS + 1
    _poll(bridge)
    listed = bridge.folder_list()
    assert listed.state == "stale"
    assert listed.folders is None


def test_an_older_revision_from_the_same_run_is_ignored(bridge: LocalBridge) -> None:
    _poll(bridge)
    bridge.accept_report(GENERATION, _report(revision=2, names=("New",)))
    bridge.accept_report(GENERATION, _report(revision=1, names=("Old",)))
    assert _names(bridge) == ["New"]
    # 同一次的重送照樣收（更新時間）
    bridge.accept_report(GENERATION, _report(revision=2, names=("New", "Again")))
    assert _names(bridge) == ["New", "Again"]


def test_a_restarted_helper_replaces_the_list(bridge: LocalBridge) -> None:
    _poll(bridge)
    bridge.accept_report(GENERATION, _report(FIRST, revision=5, names=("Before",)))
    # 重開之後修改次數從頭算，靠執行編號分辨
    bridge.accept_report(GENERATION, _report(SECOND, revision=0, names=("After",)))
    assert _names(bridge) == ["After"]


def test_a_report_from_another_pairing_is_ignored(bridge: LocalBridge) -> None:
    _poll(bridge)
    bridge.accept_report(GENERATION + 1, _report())
    assert bridge.folder_list().state == "waiting"
    # 換配對前發出、晚到的回報，不能把目前這份蓋掉
    bridge.accept_report(GENERATION, _report(names=("Mine",)))
    bridge.accept_report(GENERATION + 1, _report(SECOND, names=("Other",)))
    assert _names(bridge) == ["Mine"]


def test_a_new_pairing_forgets_the_list(bridge: LocalBridge) -> None:
    _poll(bridge)
    bridge.accept_report(GENERATION, _report())
    bridge.invalidate(GENERATION + 1)
    _poll(bridge, GENERATION + 1)
    assert bridge.folder_list().state == "waiting"


def test_a_new_pairing_with_the_same_number_still_forgets_the_list(bridge: LocalBridge) -> None:
    # 配對檔不見重建時世代號會從頭算，可能和舊的一樣；新配對的人不能看到前一位的路徑
    _poll(bridge)
    bridge.accept_report(GENERATION, _report())
    bridge.invalidate(GENERATION)
    _poll(bridge)
    listed = bridge.folder_list()
    assert listed.state == "waiting"
    assert listed.folders is None


# --- 版本不符 ---


@pytest.mark.parametrize(
    ("protocol", "problem"),
    [(PROTOCOL_VERSION - 1, "helper_older"), (PROTOCOL_VERSION + 1, "touka_older")],
)
def test_a_mismatched_poll_says_which_side_is_older(
    bridge: LocalBridge, clock: _Clock, protocol: int, problem: str
) -> None:
    bridge.note_mismatch(protocol)
    assert bridge.version_problem == problem
    clock.now += ONLINE_WINDOW_SECONDS + 1
    assert bridge.version_problem is None


@pytest.mark.parametrize("protocol", [0, -1, 100, PROTOCOL_VERSION])
def test_odd_protocol_numbers_are_not_recorded(bridge: LocalBridge, protocol: int) -> None:
    bridge.note_mismatch(protocol)
    assert bridge.version_problem is None


def test_once_online_there_is_no_version_problem(bridge: LocalBridge) -> None:
    # 重裝新版之後一連上就不再說版本不符
    bridge.note_mismatch(PROTOCOL_VERSION - 1)
    _poll(bridge)
    assert bridge.version_problem is None


def test_a_new_pairing_forgets_the_mismatch(bridge: LocalBridge) -> None:
    bridge.note_mismatch(PROTOCOL_VERSION - 1)
    bridge.invalidate(GENERATION + 1)
    assert bridge.version_problem is None
