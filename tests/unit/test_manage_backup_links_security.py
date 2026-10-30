"""備份腳本與代理人資料夾的連結（ADR-0293）.

個人空間與自訂模型（``custom/<id>`` 的設定、人格與 ``drive``）備份時不跟隨連結、一次開一個檔案
寫進 zip；還原時不跟隨連結寫回。data 根取自 ``zone_data_root``，腳本在同一個行程裡呼叫（測試
絆線看得到）。全部在 tmp_path 底下，零真實資料。
"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

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
    assert "skipped 3 links" in capsys.readouterr().out


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
    """``data/./personal_drive`` 比不到代理人資料夾的名稱，走一般還原.

    落點以身分判斷在區內就略過。
    """
    mod = _load()
    resolver = _resolver(mod, tmp_path, zone_data_root)
    source = tmp_path / "alias.zip"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr(".env", "SANCTUARY_DATA_PATH=x\n")
        zf.writestr("data/./personal_drive/Alice/x.txt", b"bad")
        zf.writestr("data/soul/Alice/core.json", b"{}")
    fresh = tmp_path.resolve() / "fresh_data"
    fake = tmp_path.resolve() / "fake_drive"
    for folder in (fresh / "personal_drive", fake):
        folder.mkdir(parents=True)
    make_dir_link(fresh / "personal_drive" / "Alice", fake)
    mod.RestoreEngine(resolver).run_restore(str(source), str(fresh))
    assert list(fake.iterdir()) == [], LINK_FOLLOWED
    assert (fresh / "soul" / "Alice" / "core.json").read_bytes() == b"{}"
    assert "'data/./personal_drive/Alice/x.txt'" in capsys.readouterr().out


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
    assert "skipped 0 links, 2 bad names" in out
    assert "'data/custom/M1/drive/./x.txt'" in out
    assert "'data/personal_drive/Alice/../../soul/x.txt'" in out
