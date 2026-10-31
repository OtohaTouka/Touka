"""R15：Touka 在 ST 裡的讀寫不跟隨連結（ADR-0293 PR 2 Q5）.

用真的 ResourceManager；版面與 RM 的 ST 都是 ``zone_data_root`` 給的 ``tmp_path/zone_state``，
連結另一端是測試自己建的 ``outside``，每個案例都核對它沒被動到。代理人在隔離裡只拿得到自己的
``home``、``tmp`` 底下（威脅模型），所以連結放在那裡；``<owner>`` 以上的連結只有以 Touka 權限
執行的程式做得到，也測（只刪連結本身或照舊拒絕）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from memory_server.utils.zone_paths import HandoffKind, is_handed_off
from src.models.identity import AgentIdentity
from src.sandbox_kernel.spec import SelfTestResult
from src.services.isolation import home_wipe, status
from src.services.isolation.profiles import (
    SANDBOX_STATE_UNCONFIGURED,
    ProfileError,
    current_layout,
    state_dirs,
)

from tests.support.links import LINK_FOLLOWED, make_dir_link
from tests.support.zone_records import expect_zone_refusal

if TYPE_CHECKING:
    from pathlib import Path

    from src.services.isolation.profiles import SandboxLayout

SOUL = "Jin"


@pytest.fixture
def state(zone_data_root, tmp_path) -> Path:
    return tmp_path.resolve() / "zone_state"


@pytest.fixture
def outside(tmp_path) -> Path:
    folder = tmp_path.resolve() / "outside"
    folder.mkdir()
    (folder / "keep.txt").write_text("keep", encoding="utf-8")
    return folder


def _untouched(outside: Path) -> None:
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"], LINK_FOLLOWED
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep", LINK_FOLLOWED


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (sys.platform == "win32" and path.is_junction())


# ── state_dirs：建 home／tmp ───────────────────────────────────────────────


def test_state_dirs_do_not_create_through_a_linked_kind_folder(state, outside):
    (state / "proxy").mkdir(parents=True)
    make_dir_link(state / "proxy" / "agents", outside)
    layout = current_layout()
    with expect_zone_refusal("ZoneLinkRefusedError"):
        try:
            state_dirs(layout, "proxy", "agents", SOUL)
        except ProfileError as exc:
            refused: ProfileError | None = exc
        else:
            refused = None
        _untouched(outside)
    assert refused is not None
    assert refused.reason == SANDBOX_STATE_UNCONFIGURED


# ── 刪除靈魂時清 proxy 的 ST（home_wipe） ───────────────────────────────────


@pytest.fixture
def proxy_only(monkeypatch):
    """本機那一層就好：預期是 Landlock，不清執行器."""
    ready = SimpleNamespace(expected="landlock")
    monitor = SimpleNamespace(ensure_ready=AsyncMock(return_value=ready))
    monkeypatch.setattr(home_wipe, "isolation_monitor", monitor)


def _owner(state: Path) -> Path:
    home, _tmp = state_dirs(current_layout(), "proxy", "agents", SOUL)
    (home / "token").write_text("old soul secret", encoding="utf-8")
    return state / "proxy" / "agents" / SOUL


async def test_wipe_removes_links_in_home_and_tmp_but_not_their_targets(
    state, outside, proxy_only
):
    owner = _owner(state)
    make_dir_link(owner / "home" / "out", outside)
    make_dir_link(owner / "tmp" / "out", outside)

    await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))

    assert not owner.exists()
    _untouched(outside)


async def test_wipe_removes_a_linked_owner_folder_itself(state, outside, proxy_only):
    (state / "proxy" / "agents").mkdir(parents=True)
    owner = state / "proxy" / "agents" / SOUL
    make_dir_link(owner, outside)

    await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))

    assert not _is_link(owner) and not owner.exists()
    _untouched(outside)


async def test_wipe_refuses_a_linked_kind_folder(state, outside, proxy_only):
    (state / "proxy").mkdir(parents=True)
    (outside / SOUL / "home").mkdir(parents=True)
    make_dir_link(state / "proxy" / "agents", outside)

    with (
        expect_zone_refusal("ZoneLinkRefusedError", at_most=2),
        pytest.raises(home_wipe.HomeWipeError) as raised,
    ):
        await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))

    assert raised.value.reason == "proxy_state_link"
    assert (outside / SOUL / "home").is_dir(), LINK_FOLLOWED
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


# ── 開機清理 tmp ─────────────────────────────────────────────────────────────


def _clear_tmp() -> None:
    for trash in status._rename_proxy_tmp(current_layout()):
        status._remove_trash([trash])


def test_boot_cleanup_removes_links_in_tmp_but_not_their_targets(state, outside):
    owner = _owner(state)
    make_dir_link(owner / "tmp" / "out", outside)
    (owner / "tmp" / "scratch.txt").write_text("x", encoding="utf-8")

    _clear_tmp()

    assert sorted(p.name for p in owner.iterdir()) == ["home"]
    assert (owner / "home" / "token").exists()
    _untouched(outside)


def test_boot_cleanup_removes_a_linked_tmp_itself(state, outside):
    owner = _owner(state)
    (owner / "tmp").rmdir()
    make_dir_link(owner / "tmp", outside)

    _clear_tmp()

    assert not _is_link(owner / "tmp") and not (owner / "tmp").exists()
    _untouched(outside)


def test_boot_cleanup_skips_a_linked_kind_folder(state, outside):
    (state / "proxy").mkdir(parents=True)
    (outside / SOUL / "tmp").mkdir(parents=True)
    make_dir_link(state / "proxy" / "agents", outside)

    with expect_zone_refusal("ZoneLinkRefusedError", at_least=0):
        _clear_tmp()

    assert (outside / SOUL / "tmp").is_dir(), LINK_FOLLOWED
    assert _is_link(state / "proxy" / "agents")


def test_boot_cleanup_leaves_other_kinds_and_old_trash_is_removed(state):
    owner = _owner(state)
    (owner / "tmp.trash-old").mkdir()
    (owner / "tmp.trash-old" / "x").write_text("x", encoding="utf-8")
    other = state / "proxy" / "elsewhere" / "Bob" / "tmp"
    other.mkdir(parents=True)

    _clear_tmp()

    assert sorted(p.name for p in owner.iterdir()) == ["home"]
    assert other.is_dir()


# ── ST 的狀態檢查與自測 ─────────────────────────────────────────────────────


def test_state_issue_does_not_trust_a_linked_proxy_folder(state, outside):
    state.mkdir(parents=True)
    make_dir_link(state / "proxy", outside)
    with expect_zone_refusal("ZoneLinkRefusedError", at_least=0):
        assert status._state_issue(current_layout()) == "not_writable"
    _untouched(outside)


class _Probe:
    def __init__(self) -> None:
        self.dirs: list[Path] = []

    async def __call__(self, _kernel: Path, test_dir: Path) -> SelfTestResult:
        assert is_handed_off(str(test_dir), HandoffKind.SUBPROCESS_CWD)
        self.dirs.append(test_dir)
        test_dir.mkdir()
        (test_dir / "probe.txt").write_text("x", encoding="utf-8")
        return SelfTestResult(ok=True, abi=6, error=None, net=True)


def _monitor(probe: _Probe, tmp_path: Path) -> tuple[status.IsolationMonitor, SandboxLayout]:
    kernel = tmp_path / "kernel"
    kernel.mkdir(exist_ok=True)
    (kernel / status.LAUNCHER_NAME).write_text("# kernel\n", encoding="utf-8")
    layout = replace(current_layout(), kernel_dir=kernel)
    monitor = status.IsolationMonitor(
        layout_provider=lambda: layout, selftest=probe, platform="linux", start_background=False
    )
    return monitor, layout


async def test_state_selftest_runs_in_its_own_folder_and_removes_it(state, tmp_path):
    probe = _Probe()
    monitor, layout = _monitor(probe, tmp_path)
    stale = state / "proxy" / "selftest" / f"{status.SELFTEST_DIR_PREFIX}old"
    stale.mkdir(parents=True)

    result = await monitor._probe(layout, "state")

    assert result.status == "available"
    (used,) = probe.dirs
    assert used.parent == state / "proxy" / "selftest"
    assert used.name.startswith(status.SELFTEST_DIR_PREFIX)
    assert not is_handed_off(str(used), HandoffKind.SUBPROCESS_CWD)
    assert list((state / "proxy" / "selftest").iterdir()) == []


async def test_state_selftest_refuses_a_linked_selftest_folder(state, outside, tmp_path):
    probe = _Probe()
    monitor, layout = _monitor(probe, tmp_path)
    (state / "proxy").mkdir(parents=True)
    stale = outside / f"{status.SELFTEST_DIR_PREFIX}old"
    stale.mkdir()
    make_dir_link(state / "proxy" / "selftest", outside)

    with expect_zone_refusal("ZoneLinkRefusedError", at_least=0):
        result = await monitor._probe(layout, "state")

    assert result.status == "unavailable"
    assert probe.dirs == []
    assert stale.is_dir(), LINK_FOLLOWED
    assert sorted(p.name for p in outside.iterdir()) == sorted(["keep.txt", stale.name])


@pytest.mark.skipif(os.name == "nt", reason="file links are only made on POSIX here")
def test_boot_cleanup_removes_a_file_link_in_tmp_only(state, outside):
    owner = _owner(state)
    (owner / "tmp" / "keep").symlink_to(outside / "keep.txt")

    _clear_tmp()

    assert not (owner / "tmp").exists()
    _untouched(outside)
