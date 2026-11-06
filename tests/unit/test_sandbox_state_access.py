"""ST 的兩個存取函式與經過它們的呼叫端（ADR-0293 PR 2：P7、Q4、Q5）.

存取函式直接打（不經呼叫端）測五種順序：版面沒設回 None（不看 RM）、只有一邊設或兩邊解析後
仍不同丟 ``ProfileError(…, SANDBOX_STATE_UNCONFIGURED)``、名稱兩個標準都要過、正常給區路徑。
呼叫端（``state_dirs``、MCP 分類的基準、演化凍結、MCP 啟動分類、狀態檢查、權限自癒）各有一個
測試對應存取函式的 None 與例外。版面與 RM 都在 ``tmp_path``（``zone_data_root``），不碰 ``data/``。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from memory_server.utils.zone_paths import Anchor, ZonePath
from src import config as app_config
from src.services.isolation import evolution_command, status
from src.services.isolation.evolution_command import refuse_deployed
from src.services.isolation.mcp_launch import McpLauncher
from src.services.isolation.mcp_profiles import mcp_classification_bases, mcp_state_dirs
from src.services.isolation.profiles import (
    SANDBOX_STATE_UNCONFIGURED,
    ProfileError,
    current_layout,
    proxy_state_owner,
    proxy_state_zone,
    state_dirs,
)
from src.services.isolation.runners import IsolationUnavailableError
from src.services.resource_manager import resource_manager as rm
from src.utils import config_utils
from src.utils.mcp_origin import OriginRecord, RawMcpEntry

from tests.support.links import make_dir_link
from tests.support.zone_roots import point_zone_roots

ST = Anchor.SANDBOX_STATE


@pytest.fixture
def layout(zone_data_root):
    """版面與 RM 的 ST 是同一處（``tmp_path/zone_state``）."""
    return current_layout()


@pytest.fixture
def rm_unset(monkeypatch):
    """RM 這邊的 ST 沒設：容器裡沒設環境變數（P7 的 ``sandbox_state_root`` 回 None）."""
    monkeypatch.setattr(app_config.settings, "TOUKA_SANDBOX_STATE_DIR", "")
    monkeypatch.setattr(app_config, "is_container_runtime", lambda: True)
    assert rm.sandbox_state_root() is None


def _unconfigured(raised: pytest.ExceptionInfo[ProfileError]) -> None:
    assert raised.value.reason == SANDBOX_STATE_UNCONFIGURED


# ── 存取函式（順序 1–5） ─────────────────────────────────────────────────────


def test_normal_gives_zone_paths(layout):
    assert proxy_state_zone(layout) == ZonePath(ST, ("proxy",))
    alice = ZonePath(ST, ("proxy", "agents", "Alice"))
    assert proxy_state_owner(layout, "agents", "Alice") == alice


def test_layout_unset_is_none_whatever_rm_says(layout):
    """順序 1：版面沒設就回 None，不看 RM（RM 這裡有設），也不看名稱."""
    unset = replace(layout, state_root=None)
    assert rm.sandbox_state_root() is not None
    assert proxy_state_zone(unset) is None
    assert proxy_state_owner(unset, "agents", "Alice") is None
    assert proxy_state_owner(unset, "agents", "a/b") is None


def test_rm_unset_while_the_layout_is_set_raises(layout, rm_unset):
    """順序 2：只有版面設了（RM 是 None）不是「沒設」."""
    with pytest.raises(ProfileError) as raised:
        proxy_state_zone(layout)
    _unconfigured(raised)
    with pytest.raises(ProfileError) as raised:
        proxy_state_owner(layout, "agents", "Alice")
    _unconfigured(raised)


def test_different_roots_raise(layout, tmp_path):
    """順序 3：兩邊都設、解析後仍不同."""
    other = replace(layout, state_root=(tmp_path / "other_state").resolve())
    with pytest.raises(ProfileError) as raised:
        proxy_state_zone(other)
    _unconfigured(raised)
    with pytest.raises(ProfileError) as raised:
        proxy_state_owner(other, "agents", "Alice")
    _unconfigured(raised)


def test_roots_are_compared_after_resolving(zone_data_root, tmp_path, monkeypatch):
    """順序 3：RM 的設定經過人做的連結、版面是解析後的寫法，仍是同一處."""
    real = tmp_path.resolve() / "real_state"
    real.mkdir()
    linked = tmp_path.resolve() / "linked_state"
    make_dir_link(linked, real)
    point_zone_roots(monkeypatch, zone_data_root, linked)
    layout = current_layout()
    assert layout.state_root == real.resolve()
    assert proxy_state_zone(layout) == ZonePath(ST, ("proxy",))
    alice = ZonePath(ST, ("proxy", "agents", "Alice"))
    assert proxy_state_owner(layout, "agents", "Alice") == alice


@pytest.mark.parametrize(
    ("kind", "owner"),
    [
        ("agents", "a/b"),
        ("agents", ".."),
        ("agents", " lead"),  # 過 is_zone_segment、不過 is_safe_segment
        ("agents", "trail "),
        ("a/b", "Alice"),
        ("..", "Alice"),
    ],
)
def test_names_that_are_not_one_segment_raise(layout, kind, owner):
    """順序 4：名稱不合格丟 ``ProfileError(owner)``（不帶原因、不回 None）."""
    with pytest.raises(ProfileError) as raised:
        proxy_state_owner(layout, kind, owner)
    assert raised.value.reason is None
    assert str(raised.value) == owner


@pytest.mark.skipif(sys.platform != "win32", reason="這些名稱只在 Windows 不能當一段")
@pytest.mark.parametrize("owner", ["con", "x:y", "trail."])
def test_windows_names_that_pass_the_old_check_raise(layout, owner):
    """順序 4：只過 ``is_safe_segment``（今天的標準）的名稱也拒絕（只會更嚴）."""
    with pytest.raises(ProfileError) as raised:
        proxy_state_owner(layout, "agents", owner)
    assert raised.value.reason is None


# ── state_dirs（Q5） ─────────────────────────────────────────────────────────


def test_state_dirs_creates_home_and_tmp_and_gives_zone_text(layout, zone_data_root, tmp_path):
    home, tmp = state_dirs(layout, "proxy", "agents", "Alice")
    owner = ZonePath(ST, ("proxy", "agents", "Alice"))
    assert str(home) == rm.zone_text(owner.child("home"))
    assert str(tmp) == rm.zone_text(owner.child("tmp"))
    state = tmp_path.resolve() / "zone_state"
    assert (state / "proxy" / "agents" / "Alice" / "home").is_dir()
    assert (state / "proxy" / "agents" / "Alice" / "tmp").is_dir()


def test_state_dirs_without_create_does_not_create(layout, tmp_path):
    home, _tmp = state_dirs(layout, "proxy", "agents", "Bob", create=False)
    assert not home.exists()
    assert not (tmp_path / "zone_state" / "proxy").exists()


def test_state_dirs_unset_layout_is_unconfigured(layout):
    with pytest.raises(ProfileError) as raised:
        state_dirs(replace(layout, state_root=None), "proxy", "agents", "Alice")
    _unconfigured(raised)


def test_state_dirs_different_roots_is_unconfigured(layout, tmp_path):
    other = replace(layout, state_root=(tmp_path / "other_state").resolve())
    with pytest.raises(ProfileError) as raised:
        state_dirs(other, "proxy", "agents", "Alice")
    _unconfigured(raised)
    assert not (tmp_path / "other_state").exists()
    assert not (tmp_path / "zone_state" / "proxy").exists()


def test_state_dirs_bad_name_is_refused(layout):
    with pytest.raises(ProfileError) as raised:
        state_dirs(layout, "proxy", "agents", "a/b")
    assert raised.value.reason is None


def test_state_dirs_only_knows_the_proxy_location(layout):
    with pytest.raises(ProfileError):
        state_dirs(layout, "executor", "agents", "Alice")  # type: ignore[arg-type]


def test_mcp_state_dirs_are_under_the_state_zone(layout):
    home, tmp = mcp_state_dirs("self", "srv", layout=layout, location="proxy")
    assert home.is_dir() and tmp.is_dir()
    assert str(home).startswith(rm.zone_text(ZonePath(ST, ("proxy",))))


# ── MCP 分類的基準（Q4） ─────────────────────────────────────────────────────


def test_classification_bases_without_state_are_only_the_base_dir(layout, tmp_path):
    base = tmp_path / "base"
    assert mcp_classification_bases(
        "srv", layout=replace(layout, state_root=None), base_dir=base
    ) == (base,)


def test_classification_bases_give_both_homes_without_creating(layout, tmp_path):
    base = tmp_path / "base"
    bases = mcp_classification_bases("srv", layout=layout, base_dir=base)
    assert bases[0] == base
    assert len(bases) == 3
    assert all(not home.exists() for home in bases[1:])


def test_classification_bases_raise_when_the_roots_differ(layout, tmp_path):
    other = replace(layout, state_root=(tmp_path / "other_state").resolve())
    with pytest.raises(ProfileError) as raised:
        mcp_classification_bases("srv", layout=other, base_dir=tmp_path / "base")
    _unconfigured(raised)


def test_refuse_deployed_turns_a_state_mismatch_into_config_unreadable(
    layout, tmp_path, monkeypatch
):
    """演化凍結判斷不了就拒絕：版面與 RM 的 ST 不同時是 ``config_unreadable``."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "server_config.json").write_text(
        json.dumps({"mcpServers": {"renamed": {"command": "python", "args": ["x.py"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_utils, "CONFIG_DIR", config)
    other = replace(layout, state_root=(tmp_path / "other_state").resolve())
    monkeypatch.setattr(evolution_command, "current_layout", lambda: other)
    with pytest.raises(evolution_command.EvolutionRefused) as raised:
        refuse_deployed("sample")
    assert raised.value.reason == "config_unreadable"


def test_mcp_classification_with_a_state_mismatch_is_isolation_unavailable(layout, tmp_path):
    """MCP 啟動的分類組不出基準：當隔離不可用（帶 ST 的原因），不啟動."""
    other = replace(layout, state_root=(tmp_path / "other_state").resolve())
    launcher = McpLauncher(layout_provider=lambda: other, base_dir=lambda: tmp_path / "base")
    entry = RawMcpEntry(sys.executable, (str(tmp_path / "server.py"),), {}, None)
    with pytest.raises(IsolationUnavailableError) as raised:
        launcher._classify_with("srv", entry, OriginRecord("present", "creation"), "ok", ())
    assert str(raised.value) == SANDBOX_STATE_UNCONFIGURED


# ── 狀態檢查與權限自癒（Q5） ─────────────────────────────────────────────────


def test_state_issue_unset_layout_is_unset(layout):
    assert status._state_issue(replace(layout, state_root=None)) == status.STATE_UNSET


def test_state_issue_rm_unset_is_not_writable(layout, rm_unset):
    assert status._state_issue(layout) == "not_writable"


def test_state_issue_different_roots_is_not_writable(layout, tmp_path):
    other = replace(layout, state_root=(tmp_path / "other_state").resolve())
    assert status._state_issue(other) == "not_writable"
    assert not (tmp_path / "other_state").exists()


def test_state_issue_creates_the_proxy_folder(layout, tmp_path):
    assert status._state_issue(layout) is None
    assert (tmp_path / "zone_state" / "proxy").is_dir()


class _Repair:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def repair(self, reason: str, paths: tuple[str, ...], *, retry: bool) -> None:
        assert retry is False
        self.calls.append((reason, paths))


def test_repair_gets_the_zone_text_of_the_proxy_folder(layout):
    hook = _Repair()
    status._repair_state_dir(hook, layout)
    assert hook.calls == [("state_issue", (rm.zone_text(ZonePath(ST, ("proxy",))),))]


@pytest.mark.parametrize("which", ["unset", "different"])
def test_repair_is_not_asked_without_a_usable_state(layout, tmp_path, which):
    if which == "unset":
        bad = replace(layout, state_root=None)
    else:
        bad = replace(layout, state_root=(tmp_path / "other_state").resolve())
    hook = _Repair()
    status._repair_state_dir(hook, bad)
    assert hook.calls == []


def test_selftest_is_not_a_state_kind():
    assert status.SELFTEST_STATE_DIR not in status.STATE_KINDS


def test_the_proxy_folder_is_not_writable_through_os_access_alone(layout, tmp_path):
    """``ST/proxy`` 不可寫照實判 not_writable（不是因為兩邊不同）."""
    if os.name == "nt":
        pytest.skip("Windows 的資料夾沒有可寫位元")
    proxy = tmp_path / "zone_state" / "proxy"
    proxy.mkdir(parents=True)
    proxy.chmod(0o555)
    try:
        if os.access(proxy, os.W_OK):
            pytest.skip("以 root 執行時權限位元不算數")
        assert status._state_issue(layout) == "not_writable"
    finally:
        proxy.chmod(0o755)
    assert proxy_state_zone(layout) == ZonePath(ST, ("proxy",))
    assert Path(rm.zone_text(ZonePath(ST, ("proxy",)))) == proxy.resolve()
