"""ResourceManager archive 能力測試 (ADR-0066 Phase 4).

驗證 iter_files / create_archive / extract_archive，含排除規則、
path traversal 與 zip bomb 防護。
"""

from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path

import pytest
from src.core.exceptions import ToukaException
from src.services.resource_manager import resource_manager


@pytest.fixture
def src_skill(tmp_path):
    """建來源 skill 目錄：SKILL.md + scripts/ + 應排除的雜訊。"""
    skill = tmp_path / "skills" / "demo"
    resource_manager.make_dir(skill / "scripts")
    resource_manager.make_dir(skill / "__pycache__")
    resource_manager.write_file_text(skill / "SKILL.md", "---\nname: demo\n---\nbody")
    resource_manager.write_file_text(skill / "scripts" / "run.py", "print(1)")
    resource_manager.write_file_text(skill / "scripts" / "cache.pyc", "junk")
    resource_manager.write_file_text(skill / "__pycache__" / "x.txt", "junk")
    resource_manager.write_file_text(skill / ".DS_Store", "junk")
    return skill


def test_create_archive_preserves_prefix_and_excludes(src_skill, tmp_path):
    """打包保留 <id>/ 前綴；排除 __pycache__ / *.pyc / .DS_Store。"""
    out = tmp_path / "demo.skill"
    resource_manager.create_archive(src_skill, out, arc_base=src_skill.parent)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "demo/SKILL.md" in names
    assert "demo/scripts/run.py" in names
    assert not any("__pycache__" in n for n in names)
    assert not any(n.endswith(".pyc") for n in names)
    assert not any(".DS_Store" in n for n in names)


def test_iter_files_no_50_limit(tmp_path):
    """iter_files 無 50 檔上限 (glob_search 的限制)。"""
    d = tmp_path / "many"
    resource_manager.make_dir(d)
    for i in range(60):
        resource_manager.write_file_text(d / f"f{i}.txt", "x")
    assert len(resource_manager.iter_files(d)) == 60


def test_extract_round_trip(src_skill, tmp_path):
    """create → extract round-trip 內容一致。"""
    out = tmp_path / "demo.skill"
    resource_manager.create_archive(src_skill, out, arc_base=src_skill.parent)
    dest = tmp_path / "extracted"
    resource_manager.extract_archive(out, dest)
    assert resource_manager.exists(dest / "demo" / "SKILL.md")
    content = resource_manager.read_file_text(dest / "demo" / "SKILL.md")
    assert content == "---\nname: demo\n---\nbody"


def test_extract_blocks_path_traversal(tmp_path):
    """惡意 zip 含 ../ entry → ToukaException (path traversal 防護)。"""
    bad = tmp_path / "evil.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../escape.txt", "pwned")
    dest = tmp_path / "dest"
    with pytest.raises(ToukaException):
        resource_manager.extract_archive(bad, dest)


def test_extract_blocks_too_many_entries(tmp_path):
    """entry 數超限 → ToukaException (REQUEST_TOO_LARGE)。"""
    z = tmp_path / "many.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for i in range(10):
            zf.writestr(f"f{i}.txt", "x")
    dest = tmp_path / "dest"
    with pytest.raises(ToukaException):
        resource_manager.extract_archive(z, dest, max_entries=5)


def test_extract_blocks_zip_bomb_size(tmp_path):
    """解壓總量超限 → ToukaException (zip bomb 防護)。"""
    z = tmp_path / "bomb.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("big.txt", "A" * 10000)
    dest = tmp_path / "dest"
    with pytest.raises(ToukaException):
        resource_manager.extract_archive(z, dest, max_total_size=100)


# ── 清除連備份用的 I/O 原語（R01～R04）─────────────────────────────────────


def _member_meta(path):
    with zipfile.ZipFile(path) as zf:
        return [
            (i.filename, i.CRC, i.file_size, i.date_time, i.compress_type, i.external_attr)
            for i in zf.infolist()
        ]


def test_r01_rewrite_archive_drops_replaces_and_keeps_metadata(tmp_path):
    src = tmp_path / "src.zip"
    big = bytes(range(256)) * 8192  # 2 MiB：超過串流的 chunk
    with zipfile.ZipFile(src, "w") as zf:
        zf.comment = b"snapshot"
        info = zipfile.ZipInfo("data/keep.bin", date_time=(2020, 1, 2, 3, 4, 6))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o640 << 16
        zf.writestr(info, big)
        zf.writestr(zipfile.ZipInfo("data/dir/", date_time=(2020, 1, 1, 0, 0, 0)), b"")
        zf.writestr("data/drop.txt", b"gone")
        zf.writestr("data/swap.db", b"old")
        zf.writestr("data/meta.json", b"{}")
    replacement = tmp_path / "new.db"
    replacement.write_bytes(b"new-db-bytes")
    out = tmp_path / "out.tmp"
    resource_manager.rewrite_archive(
        src,
        out,
        drop={"data/drop.txt"},
        replace={"data/swap.db": replacement, "data/meta.json": b'{"a": 1}'},
    )
    before = {m[0]: m for m in _member_meta(src)}
    after = _member_meta(out)
    assert [m[0] for m in after] == ["data/keep.bin", "data/dir/", "data/swap.db", "data/meta.json"]
    assert after[0] == before["data/keep.bin"]
    assert after[1] == before["data/dir/"]
    with zipfile.ZipFile(out) as zf:
        assert zf.comment == b"snapshot"
        assert zf.testzip() is None
        assert zf.read("data/keep.bin") == big
        assert zf.read("data/swap.db") == b"new-db-bytes"
        assert zf.read("data/meta.json") == b'{"a": 1}'
        assert zf.getinfo("data/swap.db").date_time == before["data/swap.db"][3]


def test_r02_copy_file_metadata(tmp_path):
    src = tmp_path / "a.zip"
    dst = tmp_path / "b.tmp"
    src.write_bytes(b"a")
    dst.write_bytes(b"b")
    os.utime(src, ns=(1_600_000_000_000_000_000, 1_600_000_000_123_456_789))
    resource_manager.copy_file_metadata(src, dst)
    assert dst.stat().st_mtime_ns == src.stat().st_mtime_ns


def test_r02_copy_file_metadata_refuses_protected_target(tmp_path):
    protected = Path(resource_manager.get_base_path()) / "soul" / "zz-scrub-a" / "x.md"
    with pytest.raises(PermissionError):
        resource_manager.copy_file_metadata(tmp_path / "a", protected)


def test_r03_fsync_helpers(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"x")
    resource_manager.fsync_file(f)
    resource_manager.fsync_dir(tmp_path)


def test_r04_read_prefix_and_make_writable(tmp_path):
    f = tmp_path / "ro.bin"
    f.write_bytes(b"PK\x03\x04rest")
    assert resource_manager.read_file_prefix(f, 4) == b"PK\x03\x04"
    os.chmod(f, stat.S_IREAD)
    resource_manager.make_writable(f)
    resource_manager.remove_file(f)
    assert not f.exists()


@pytest.mark.parametrize("call", ["rewrite_archive", "make_writable", "fsync_file"])
def test_r05_new_writers_refuse_protected_paths(tmp_path, call):
    protected = Path(resource_manager.get_base_path()) / "soul" / "zz-scrub-a" / "x.zip"
    with pytest.raises(PermissionError):
        if call == "rewrite_archive":
            resource_manager.rewrite_archive(tmp_path / "a.zip", protected, drop=set(), replace={})
        elif call == "make_writable":
            resource_manager.make_writable(protected)
        else:
            resource_manager.fsync_file(protected)
