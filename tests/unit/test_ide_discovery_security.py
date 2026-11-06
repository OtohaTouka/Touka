"""ToukaIDE 連線資訊檔的信任判斷（ADR-0294）。

POSIX 上連線資訊檔在大家共用的暫存區，Touka 只讀自己的、別人不能寫的資料夾與檔案。判斷是純函式；
流程測試替換「目前使用者」與 stat 結果，所以 Windows 本機也跑得到。真實連結與 FIFO 的行為靠同一個
底層函式的既有測試（``test_file_ops_no_follow_security.py``）；Linux CI 上 ``test_ide_discovery.py``
走的是真的檢查。全部在 ``tmp_path``，不碰真的暫存區。
"""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from memory_server.utils.no_follow import LinkRefusedError
from src.protocols.ide import discovery

ME = 1000
OTHER = 1001

PAYLOAD = {
    "ideInfo": {"name": "ToukaUI"},
    "port": 3005,
    "authToken": "test-token",
    "transport": {"type": "streamable-http", "endpoint": "/mcp", "protocolMode": "2026-07-28"},
}


def _stat(mode: int, uid: int) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, uid, uid, 0, 0, 0, 0))


# ---------------------------------------------------------------------------
# 純函式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [stat.S_IFDIR | 0o700, stat.S_IFDIR | 0o755])
def test_accepts_own_folder_only_the_user_can_write(mode: int):
    assert discovery._is_private(_stat(mode, ME), ME, directory=True)


@pytest.mark.parametrize(
    ("mode", "uid"),
    [
        (stat.S_IFLNK | 0o777, ME),
        (stat.S_IFREG | 0o700, ME),
        (stat.S_IFDIR | 0o700, OTHER),
        (stat.S_IFDIR | 0o770, ME),
        (stat.S_IFDIR | 0o707, ME),
    ],
)
def test_rejects_folder_that_is_a_link_not_a_folder_someone_elses_or_writable(mode: int, uid: int):
    assert not discovery._is_private(_stat(mode, uid), ME, directory=True)


@pytest.mark.parametrize("mode", [stat.S_IFREG | 0o600, stat.S_IFREG | 0o644])
def test_accepts_own_file_only_the_user_can_write(mode: int):
    assert discovery._is_private(_stat(mode, ME), ME, directory=False)


@pytest.mark.parametrize(
    ("mode", "uid"),
    [
        (stat.S_IFLNK | 0o777, ME),
        (stat.S_IFDIR | 0o700, ME),
        (stat.S_IFIFO | 0o600, ME),
        (stat.S_IFREG | 0o600, OTHER),
        (stat.S_IFREG | 0o620, ME),
        (stat.S_IFREG | 0o602, ME),
    ],
)
def test_rejects_file_that_is_not_regular_someone_elses_or_writable(mode: int, uid: int):
    assert not discovery._is_private(_stat(mode, uid), ME, directory=False)


def test_default_folder_starts_from_the_resolved_temp_folder(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(discovery.tempfile, "gettempdir", lambda: str(tmp_path / "a" / ".." / "t"))
    assert discovery._default_ide_dir() == (tmp_path / "t").resolve() / "gemini" / "ide"


# ---------------------------------------------------------------------------
# 流程（模擬 POSIX）
# ---------------------------------------------------------------------------


@pytest.fixture
def ide_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "gemini" / "ide"
    folder.mkdir(parents=True)
    (folder / "gemini-ide-server-touka.json").write_text(json.dumps(PAYLOAD), encoding="utf-8")
    return folder


@pytest.fixture
def posix(monkeypatch, ide_dir: Path) -> SimpleNamespace:
    """目前使用者是 ME；兩層資料夾與檔案的 stat 由測試指定（預設都是自己的、別人不能寫）。

    ``folders`` 是不跟隨連結看到的（lstat），``followed`` 是跟隨連結會看到的
    （連結另一端，預設是自己的、別人不能寫）。程式若改成跟隨連結，就會看到 ``followed``
    而放行，測試因此變紅，不靠執行者的 uid。
    """
    trusted_dir = _stat(stat.S_IFDIR | 0o700, ME)
    trusted_file = _stat(stat.S_IFREG | 0o600, ME)
    discovery_file = ide_dir / "gemini-ide-server-touka.json"
    state = SimpleNamespace(
        folders={ide_dir.parent: trusted_dir, ide_dir: trusted_dir},
        followed={ide_dir.parent: trusted_dir, ide_dir: trusted_dir, discovery_file: trusted_file},
        file=trusted_file,
        open_error=None,
        access_error=None,
        opened=[],
    )
    real_lstat = os.lstat
    real_stat = os.stat

    def fake_lstat(path, *args, **kwargs):
        if state.access_error is not None and Path(path) == ide_dir:
            raise state.access_error
        return state.folders.get(Path(path)) or real_lstat(path, *args, **kwargs)

    def fake_stat(path, *args, **kwargs):
        if state.access_error is not None and Path(path) == ide_dir:
            raise state.access_error
        return state.followed.get(Path(path)) or real_stat(path, *args, **kwargs)

    def fake_open(path):
        state.opened.append(Path(path))
        if state.open_error is not None:
            raise state.open_error
        return io.BytesIO(Path(path).read_bytes()), state.file

    monkeypatch.setattr(discovery, "_current_uid", lambda: ME)
    monkeypatch.setattr(discovery.os, "lstat", fake_lstat)
    monkeypatch.setattr(discovery.os, "stat", fake_stat)
    monkeypatch.setattr(discovery, "open_read_no_follow", fake_open)
    return state


async def test_discovers_the_users_own_private_file(posix, ide_dir: Path):
    result = await discovery.discover_ide_servers(ide_base_dir=ide_dir)
    assert result["ToukaUI"]["url"] == "http://localhost:3005/mcp"
    assert posix.opened == [ide_dir / "gemini-ide-server-touka.json"]


@pytest.mark.parametrize("layer", ["gemini", "ide"])
@pytest.mark.parametrize(
    "bad",
    [
        _stat(stat.S_IFLNK | 0o777, ME),
        _stat(stat.S_IFDIR | 0o700, OTHER),
        _stat(stat.S_IFDIR | 0o775, ME),
    ],
    ids=["link", "someone-elses", "group-writable"],
)
async def test_skips_everything_under_an_untrusted_folder(posix, ide_dir: Path, layer: str, bad):
    folder = ide_dir.parent if layer == "gemini" else ide_dir
    posix.folders[folder] = bad
    if not stat.S_ISLNK(bad.st_mode):
        posix.followed[folder] = bad
    assert await discovery.discover_ide_servers(ide_base_dir=ide_dir) == {}


async def test_skips_a_folder_it_cannot_enter_instead_of_failing(posix, ide_dir: Path, caplog):
    # 別人先建了只有他能進的 gemini 時，ide 那層 stat、lstat 都會 EACCES
    posix.access_error = PermissionError(13, "Permission denied", str(ide_dir))
    assert await discovery.discover_ide_servers(ide_base_dir=ide_dir) == {}
    assert "略過 IDE 連線資訊資料夾" in caplog.text


@pytest.mark.parametrize(
    "bad",
    [_stat(stat.S_IFREG | 0o600, OTHER), _stat(stat.S_IFREG | 0o666, ME)],
    ids=["someone-elses", "writable-by-others"],
)
async def test_skips_a_file_someone_else_owns_or_can_write(posix, ide_dir: Path, bad):
    posix.file = bad
    assert await discovery.discover_ide_servers(ide_base_dir=ide_dir) == {}


async def test_skips_a_file_reached_through_a_link(posix, ide_dir: Path):
    posix.open_error = LinkRefusedError(40, "link", str(ide_dir))
    assert await discovery.discover_ide_servers(ide_base_dir=ide_dir) == {}


async def test_windows_reads_without_owner_checks(monkeypatch, ide_dir: Path):
    monkeypatch.setattr(discovery, "_current_uid", lambda: None)
    result = await discovery.discover_ide_servers(ide_base_dir=ide_dir)
    assert "ToukaUI" in result
