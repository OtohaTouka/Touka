"""備份腳本與代理人資料夾的連結（ADR-0293）.

個人空間與自訂模型（``custom/<id>`` 的設定、人格與 ``drive``）備份時不跟隨連結、一次開一個檔案
寫進 zip；還原時不跟隨連結寫回。data 根取自 ``zone_data_root``，腳本在同一個行程裡呼叫（測試
絆線看得到）。全部在 tmp_path 底下，零真實資料。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from tests.support.links import LINK_FOLLOWED, make_dir_link

_SCRIPT = Path(__file__).resolve().parents[2] / "ToukaProxy" / "scripts" / "manage_backup.py"
_SECRET = b"SECRET users.json"
_FILES = {
    "personal_drive/Alice/notes.md": b"alice notes",
    "personal_drive/Alice/sub/deep.bin": b"\x00\x01\x02",
    "custom/M1/config.json": b'{"model": "m1"}',
    "custom/M1/system_prompt.md": b"be helpful",
    "custom/M1/drive/a.txt": b"drive file",
    "soul/Alice/core.json": b"{}",
}


def _load():
    spec = importlib.util.spec_from_file_location("manage_backup_links_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(data: Path) -> None:
    for rel, content in _FILES.items():
        (data / rel).parent.mkdir(parents=True, exist_ok=True)
        (data / rel).write_bytes(content)


def _resolver(mod, tmp_path: Path, data: Path):
    project = tmp_path.resolve() / "project"
    config = project / "ToukaProxy" / "config"
    config.mkdir(parents=True)
    env = project / ".env"
    env.write_text(f"SANCTUARY_DATA_PATH={data.as_posix()}\n", encoding="utf-8")
    return mod.PathResolver(project, env_path=env, data_root=data, config_dir=config)


def _fake_config(tmp_path: Path) -> Path:
    fake = tmp_path.resolve() / "fake_config"
    fake.mkdir()
    (fake / "users.json").write_bytes(_SECRET)
    return fake


def _backup(mod, resolver, tmp_path: Path) -> Path:
    return mod.BackupEngine(resolver).run_backup(tmp_path / "backups", "Touka_Backup")


def _contents(output: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(output) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def test_backup_and_restore_round_trip_the_agent_folders(zone_data_root, tmp_path):
    """個人空間、自訂模型的設定與人格、drive：備份後還原到全新的 data 路徑，逐檔相同."""
    _seed(zone_data_root)
    mod = _load()
    resolver = _resolver(mod, tmp_path, zone_data_root)
    output = _backup(mod, resolver, tmp_path)
    assert {f"data/{rel}" for rel in _FILES} <= set(_contents(output))
    fresh = tmp_path.resolve() / "fresh_data"
    mod.RestoreEngine(resolver).run_restore(str(output), str(fresh))
    for rel, content in _FILES.items():
        assert (fresh / rel).read_bytes() == content


def test_backup_does_not_follow_links_in_agent_folders(zone_data_root, tmp_path, capsys):
    """個人空間、``custom/<id>`` 與它的 drive 裡的連結：不跟隨、不打包另一端，略過數印出來."""
    _seed(zone_data_root)
    fake = _fake_config(tmp_path)
    make_dir_link(zone_data_root / "personal_drive" / "Bob", fake)
    make_dir_link(zone_data_root / "custom" / "M1" / "secrets", fake)
    make_dir_link(zone_data_root / "custom" / "M1" / "drive" / "docs", fake)
    mod = _load()
    output = _backup(mod, _resolver(mod, tmp_path, zone_data_root), tmp_path)
    contents = _contents(output)
    assert all(_SECRET not in content for content in contents.values()), LINK_FOLLOWED
    assert contents["data/custom/M1/config.json"] == _FILES["custom/M1/config.json"]
    assert "Skipped 3 links" in capsys.readouterr().out


def test_backup_does_not_follow_a_folder_swapped_after_collection(
    zone_data_root, tmp_path, monkeypatch
):
    """收集之後才把個人空間裡的資料夾換成連結：寫進 zip 時不跟隨."""
    fake = _fake_config(tmp_path)
    docs = zone_data_root / "personal_drive" / "Alice" / "docs"
    docs.mkdir(parents=True)
    (docs / "users.json").write_bytes(b"original")
    mod = _load()
    collect = mod.BackupEngine.collect_items

    def _collect_then_swap(self):
        items = collect(self)
        (docs / "users.json").unlink()
        docs.rmdir()
        make_dir_link(docs, fake)
        return items

    monkeypatch.setattr(mod.BackupEngine, "collect_items", _collect_then_swap)
    output = _backup(mod, _resolver(mod, tmp_path, zone_data_root), tmp_path)
    assert all(_SECRET not in content for content in _contents(output).values()), LINK_FOLLOWED


def test_restore_does_not_write_through_links(zone_data_root, tmp_path):
    """還原的目的地裡個人空間或 ``custom/<id>`` 是連結：不寫到另一端."""
    _seed(zone_data_root)
    mod = _load()
    resolver = _resolver(mod, tmp_path, zone_data_root)
    output = _backup(mod, resolver, tmp_path)
    fresh = tmp_path.resolve() / "fresh_data"
    fake_drive, fake_custom = tmp_path.resolve() / "fake_drive", tmp_path.resolve() / "fake_custom"
    for folder in (fresh / "personal_drive", fresh / "custom", fake_drive, fake_custom):
        folder.mkdir(parents=True)
    make_dir_link(fresh / "personal_drive" / "Alice", fake_drive)
    make_dir_link(fresh / "custom" / "M1", fake_custom)
    mod.RestoreEngine(resolver).run_restore(str(output), str(fresh))
    assert list(fake_drive.iterdir()) == [], LINK_FOLLOWED
    assert list(fake_custom.iterdir()) == [], LINK_FOLLOWED
    assert (fresh / "soul" / "Alice" / "core.json").read_bytes() == b"{}"


def test_restore_skips_an_alias_of_an_agent_folder(zone_data_root, tmp_path, capsys):
    """別名（短檔名、大小寫不分的掛載）比不到代理人資料夾的名稱，走一般還原.

    這台機器做不出掛載，用 data 根的連結模擬同一個檔案身分；落點以身分判斷在區內就略過。
    名稱用備份會寫的資料夾（``stats``）：其他名稱先被允許清單略過（ADR-0293 PR 2 Q11）。
    """
    mod = _load()
    resolver = _resolver(mod, tmp_path, zone_data_root)
    source = tmp_path / "alias.zip"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr(".env", "SANCTUARY_DATA_PATH=x\n")
        zf.writestr("data/stats/Alice/x.txt", b"bad")
        zf.writestr("data/soul/Alice/core.json", b"{}")
    fresh = tmp_path.resolve() / "fresh_data"
    fake = tmp_path.resolve() / "fake_drive"
    for folder in (fresh / "personal_drive", fake):
        folder.mkdir(parents=True)
    make_dir_link(fresh / "personal_drive" / "Alice", fake)
    make_dir_link(fresh / "stats", fresh / "personal_drive")
    mod.RestoreEngine(resolver).run_restore(str(source), str(fresh))
    assert list(fake.iterdir()) == [], LINK_FOLLOWED
    assert (fresh / "soul" / "Alice" / "core.json").read_bytes() == b"{}"
    assert "'data/stats/Alice/x.txt'" in capsys.readouterr().out


def test_restore_skips_names_that_leave_the_data_path(
    zone_data_root, tmp_path, capsys, monkeypatch
):
    """``data//…`` 去掉 ``data/`` 後從根開始，``.``、``..`` 比不到資料夾名稱：略過並列出.

    建資料夾與搬檔只准在 tmp_path 底下：改壞這條檢查時落點是檔案系統根，先擋下來、不真的寫。
    第一段是備份會寫的資料夾、後面才走出去的（``soul/../../..``、Windows 的 ``C:x.db``）只有這條
    擋得到：允許清單只看第一段（ADR-0293 PR 2 Q11）。
    """
    mod = _load()
    resolver = _resolver(mod, tmp_path, zone_data_root)
    inside = tmp_path.resolve()

    def _only_inside(real, index):
        def _call(*args, **kwargs):
            where = Path(os.path.abspath(args[index]))
            assert where.is_relative_to(inside), f"wrote outside the test folder: {where}"
            return real(*args, **kwargs)

        return _call

    monkeypatch.setattr(Path, "mkdir", _only_inside(Path.mkdir, 0))
    monkeypatch.setattr(shutil, "move", _only_inside(shutil.move, 1))
    source = tmp_path / "escape.zip"
    names = [
        "data//personal_drive/Alice/x.txt",
        "data/./soul/x.txt",
        "data/../outside.txt",
        "data/soul/../../../outside.txt",
    ]
    if os.name == "nt":  # 磁碟代號開頭的只有 Windows 會當成根
        names += ["data/C:/escape/x.txt", "data/C:x.txt", "data/C:x.db"]
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr(".env", "SANCTUARY_DATA_PATH=x\n")
        for name in names:
            zf.writestr(name, b"bad")
        zf.writestr("data/soul/Alice/core.json", b"{}")
    fresh = inside / "fresh_data"
    mod.RestoreEngine(resolver).run_restore(str(source), str(fresh))
    assert (fresh / "soul" / "Alice" / "core.json").read_bytes() == b"{}"
    assert not (inside / "outside.txt").exists()
    assert not (inside.parent / "outside.txt").exists()
    out = capsys.readouterr().out
    assert all(f"'{name}'" in out for name in names), out


def test_restore_skips_a_folder_entry_that_is_an_agent_anchor(zone_data_root, tmp_path, capsys):
    """資料夾項目的落點是錨點本身（這裡用 data 根的連結當別名）：略過，不清掉整個個人空間.

    名稱用備份會寫的資料夾（``stats``），理由同上面的別名測試。
    """
    mod = _load()
    resolver = _resolver(mod, tmp_path, zone_data_root)
    source = tmp_path / "folder.zip"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr(".env", "SANCTUARY_DATA_PATH=x\n")
        zf.writestr("data/stats/", b"")
    fresh = tmp_path.resolve() / "fresh_data"
    (fresh / "personal_drive" / "Alice").mkdir(parents=True)
    (fresh / "personal_drive" / "Alice" / "keep.txt").write_bytes(b"keep")
    make_dir_link(fresh / "stats", fresh / "personal_drive")
    mod.RestoreEngine(resolver).run_restore(str(source), str(fresh))
    assert (fresh / "personal_drive" / "Alice" / "keep.txt").read_bytes() == b"keep"
    assert "'data/stats/'" in capsys.readouterr().out


def test_restore_skips_config_names_that_leave_the_config_folder(
    zone_data_root, tmp_path, capsys, monkeypatch
):
    """``config/proxy/`` 底下的名稱也要是一般相對路徑：雙斜線、``..`` 的略過並列出，不搬設定."""
    mod = _load()
    resolver = _resolver(mod, tmp_path, zone_data_root)
    inside = tmp_path.resolve() / "project"

    def _only_inside(*args, **kwargs):
        where = Path(os.path.abspath(args[1]))
        assert where.is_relative_to(inside), f"moved outside the project: {where}"
        return real_move(*args, **kwargs)

    real_move = shutil.move
    monkeypatch.setattr(shutil, "move", _only_inside)
    config = Path(resolver.get_proxy_config_dir())
    (config / "kept.json").write_bytes(b"kept")
    source = tmp_path / "config.zip"
    names = ["config/proxy//escape.json", "config/proxy/../../kept.json"]
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr(".env", "SANCTUARY_DATA_PATH=x\n")
        for name in names:
            zf.writestr(name, b"bad")
        zf.writestr("config/proxy/restored.json", b"ok")
    mod.RestoreEngine(resolver).run_restore(str(source), str(tmp_path.resolve() / "fresh_data"))
    assert (config / "kept.json").read_bytes() == b"kept"
    assert (config / "restored.json").read_bytes() == b"ok"
    out = capsys.readouterr().out
    assert all(f"'{name}'" in out for name in names), out


def test_restore_skips_member_names_that_are_not_a_segment(zone_data_root, tmp_path, capsys):
    """成員名稱只用 ``/`` 拆，每段都要合格；不合格的略過並列出；開頭空白的名稱照常寫回."""
    mod = _load()
    resolver = _resolver(mod, tmp_path, zone_data_root)
    source = tmp_path / "crafted.zip"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr(".env", "SANCTUARY_DATA_PATH=x\n")
        zf.writestr("data/personal_drive/Alice/ok.txt", b"ok")
        zf.writestr("data/personal_drive/Alice/../../soul/x.txt", b"bad")
        zf.writestr("data/custom/M1/drive/./x.txt", b"bad")
        zf.writestr("data/custom/M1/drive/ lead.txt", b"keep")
    fresh = tmp_path.resolve() / "fresh_data"
    mod.RestoreEngine(resolver).run_restore(str(source), str(fresh))
    assert (fresh / "personal_drive" / "Alice" / "ok.txt").read_bytes() == b"ok"
    assert (fresh / "custom" / "M1" / "drive" / " lead.txt").read_bytes() == b"keep"
    written = sorted(p.name for p in fresh.rglob("*") if p.is_file())
    assert written == [" lead.txt", "ok.txt"]
    out = capsys.readouterr().out
    assert "Skipped 0 links, 2 bad names" in out
    assert "'data/custom/M1/drive/./x.txt'" in out
    assert "'data/personal_drive/Alice/../../soul/x.txt'" in out


def test_restore_skips_members_the_backup_never_writes(zone_data_root, tmp_path, capsys):
    """備份只寫 ``SANCTUARY_FOLDERS`` 與 data 根的 ``*.db``；其他成員略過並列出.

    ``temp/sandbox_state`` 不在任何錨點底下（還原的第二層不知道 ST 在哪），只有允許清單擋得到；
    ``mcp_servers/managed`` 是舊程式沒有執法的錨點。兩處路上都有連結，寫過去就是跟隨了
    （ADR-0293 PR 2 Q11）。
    """
    mod = _load()
    resolver = _resolver(mod, tmp_path, zone_data_root)
    source = tmp_path / "extra.zip"
    names = [
        "data/temp/sandbox_state/proxy/agents/Alice/link/x.txt",
        "data/mcp_servers/managed/tool/server.py",
        "data/readme.txt",
    ]
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr(".env", "SANCTUARY_DATA_PATH=x\n")
        for name in names:
            zf.writestr(name, b"bad")
        zf.writestr("data/ui_sessions.db", b"db")
        zf.writestr("data/soul/Alice/core.json", b"{}")
    fresh = tmp_path.resolve() / "fresh_data"
    owner = fresh / "temp" / "sandbox_state" / "proxy" / "agents" / "Alice"
    fake_home, fake_tool = tmp_path.resolve() / "fake_home", tmp_path.resolve() / "fake_tool"
    for folder in (owner, fresh / "mcp_servers" / "managed", fake_home, fake_tool):
        folder.mkdir(parents=True)
    make_dir_link(owner / "link", fake_home)
    make_dir_link(fresh / "mcp_servers" / "managed" / "tool", fake_tool)
    mod.RestoreEngine(resolver).run_restore(str(source), str(fresh))
    assert list(fake_home.iterdir()) == [], LINK_FOLLOWED
    assert list(fake_tool.iterdir()) == [], LINK_FOLLOWED
    assert not (fresh / "readme.txt").exists()
    assert (fresh / "ui_sessions.db").read_bytes() == b"db"
    assert (fresh / "soul" / "Alice" / "core.json").read_bytes() == b"{}"
    out = capsys.readouterr().out
    assert all(f"'{name}'" in out for name in names), out


@pytest.mark.skipif(os.name == "nt", reason="大小寫不同的名稱只在大小寫有別的檔案系統才是另一個")
def test_restore_compares_folder_names_exactly(zone_data_root, tmp_path, capsys):
    """允許清單和代理人資料夾的分流用同一個鍵（``normcase`` 後完全相等）.

    大小寫不同的略過並列出。
    """
    mod = _load()
    resolver = _resolver(mod, tmp_path, zone_data_root)
    source = tmp_path / "case.zip"
    names = ["data/Personal_Drive/Alice/x.txt", "data/Soul/Alice/core.json"]
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr(".env", "SANCTUARY_DATA_PATH=x\n")
        for name in names:
            zf.writestr(name, b"bad")
    fresh = tmp_path.resolve() / "fresh_data"
    mod.RestoreEngine(resolver).run_restore(str(source), str(fresh))
    assert not (fresh / "Personal_Drive").exists()
    assert not (fresh / "Soul").exists()
    out = capsys.readouterr().out
    assert all(f"'{name}'" in out for name in names), out
