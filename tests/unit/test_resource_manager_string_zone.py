"""ResourceManager 收字串路徑的方法遇到代理人資料夾（ADR-0293 Z2、R4）.

- 單一路徑：在區裡（含 ``..`` 與經連結進去的）就丟 ``ZonePathRequiredError``；錨點本身與
  ``custom/<id>/config.json`` 照常。
- 兩個路徑參數：任一邊在區裡或包含區就拒絕。
- 遞迴的列、找、統計：剪掉區、不跟隨連結，照常可用。
- 遞迴的刪、打包、解壓：根包含區就拒絕；走訪途中遇到區根也整個拒絕；連結刪本身、打包時略過。

data 根是 ``zone_data_root`` 給的 ``tmp_path``；「專案」是它的上層或旁邊的資料夾。
"""

from __future__ import annotations

import sys
import zipfile
from typing import TYPE_CHECKING, Any

import pytest
from memory_server.utils.zone_paths import ZonePathRequiredError
from src.services import resource_manager as rm_module
from src.services.resource_manager import resource_manager as rm

from tests.support.links import LINK_FOLLOWED, make_dir_link
from tests.support.zone_records import raises_zone

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

CANARY = "CANARY"

SINGLE: dict[str, Callable[[Path], Any]] = {
    "read_file_text": lambda p: rm.read_file_text(p),
    "read_file_binary": lambda p: rm.read_file_binary(p),
    "read_file_prefix": lambda p: rm.read_file_prefix(p, 4),
    "read_json": lambda p: rm.read_json(p),
    "exists": lambda p: rm.exists(p),
    "ensure_no_links": lambda p: rm.ensure_no_links(p),
    "read_file_text_no_follow": lambda p: rm.read_file_text_no_follow(p),
    "read_file_binary_no_follow": lambda p: rm.read_file_binary_no_follow(p),
    "write_file_text": lambda p: rm.write_file_text(p, "x"),
    "write_file_text_no_follow": lambda p: rm.write_file_text_no_follow(p, "x"),
    "write_json": lambda p: rm.write_json(p, {}),
    "write_file_binary": lambda p: rm.write_file_binary(p, b"x"),
    "append_file_text": lambda p: rm.append_file_text(p, "x"),
    "append_file_text_no_follow": lambda p: rm.append_file_text_no_follow(p, "x"),
    "append_csv_row": lambda p: rm.append_csv_row(p, ["x"]),
    "make_dir": lambda p: rm.make_dir(p),
    "remove_file": lambda p: rm.remove_file(p),
    "remove_file_no_follow": lambda p: rm.remove_file_no_follow(p),
    "make_writable": lambda p: rm.make_writable(p),
    "fsync_file": lambda p: rm.fsync_file(p),
    "fsync_dir": lambda p: rm.fsync_dir(p),
}


@pytest.fixture
def data(zone_data_root: Path) -> Path:
    alice = zone_data_root / "personal_drive" / "Alice"
    alice.mkdir(parents=True)
    (alice / "notes.txt").write_text(CANARY, encoding="utf-8")
    m1 = zone_data_root / "custom" / "M1"
    (m1 / "drive").mkdir(parents=True)
    (m1 / "drive" / "d.txt").write_text(CANARY, encoding="utf-8")
    (m1 / "config.json").write_text("{}", encoding="utf-8")
    return zone_data_root


@pytest.fixture
def project(data: Path) -> Path:
    """data 根的上層當專案根（管理員在 Touka 自己的 repo 上工作）."""
    root = data.parent
    (root / "src").mkdir()
    (root / "src" / "a.txt").write_text("a", encoding="utf-8")
    return root


def _drives_untouched(data: Path) -> bool:
    alice = data / "personal_drive" / "Alice"
    drive = data / "custom" / "M1" / "drive"
    return (
        sorted(p.name for p in alice.iterdir()) == ["notes.txt"]
        and (alice / "notes.txt").read_text("utf-8") == CANARY
        and sorted(p.name for p in drive.iterdir()) == ["d.txt"]
        and (drive / "d.txt").read_text("utf-8") == CANARY
    )


@pytest.mark.parametrize("method", sorted(SINGLE))
@pytest.mark.parametrize(
    "inside",
    [
        "personal_drive/Alice/notes.txt",
        "custom/M1/drive/d.txt",
        "personal_drive/Alice",
        "stats/../personal_drive/Alice/notes.txt",
    ],
)
def test_a_string_path_inside_a_drive_is_refused(data: Path, method: str, inside: str):
    with raises_zone(ZonePathRequiredError):
        SINGLE[method](data.joinpath(*inside.split("/")))
    assert _drives_untouched(data)


def test_a_string_path_through_a_link_into_a_drive_is_refused(data: Path, tmp_path: Path):
    alias = tmp_path.resolve() / "alias"
    make_dir_link(alias, data / "personal_drive" / "Alice")
    with raises_zone(ZonePathRequiredError):
        rm.read_file_text(alias / "notes.txt")
    with raises_zone(ZonePathRequiredError):
        rm.write_file_text(alias / "new.txt", "x")
    assert _drives_untouched(data)


def test_the_anchor_itself_and_files_beside_the_drive_stay_plain(data: Path):
    assert rm.exists(data / "personal_drive")
    rm.make_dir(data / "personal_drive")
    assert rm.read_file_text(data / "custom" / "M1" / "config.json") == "{}"
    rm.write_file_text(data / "custom" / "M1" / "system_prompt.md", "p")
    assert rm.read_json(data / "custom" / "M1" / "config.json") == {}
    assert rm.exists(data / "custom" / "M1")


@pytest.mark.parametrize("method", ["copy_file", "move_file", "copy_file_metadata"])
def test_two_path_methods_refuse_either_side(data: Path, tmp_path: Path, method: str):
    plain = tmp_path.resolve() / "plain.txt"
    plain.write_text("p", encoding="utf-8")
    inside = data / "personal_drive" / "Alice" / "notes.txt"
    call = getattr(rm, method)
    for src, dst in (
        (inside, tmp_path.resolve() / "copy.txt"),
        (plain, data / "personal_drive" / "Alice" / "new.txt"),
    ):
        with raises_zone(ZonePathRequiredError):
            call(src, dst)
    assert _drives_untouched(data)
    assert plain.read_text("utf-8") == "p"
    assert not (tmp_path / "copy.txt").exists()


def test_moving_a_folder_that_holds_a_drive_is_refused(data: Path, tmp_path: Path):
    with raises_zone(ZonePathRequiredError):
        rm.move_file(data / "custom" / "M1", tmp_path.resolve() / "M1")
    with raises_zone(ZonePathRequiredError):
        rm.move_file(data / "personal_drive", tmp_path.resolve() / "pd")
    assert _drives_untouched(data)


def test_listing_finding_and_counting_a_project_skip_the_drives(project: Path, data: Path):
    files = {p.relative_to(project).as_posix() for p in rm.iter_files(project)}
    assert {"src/a.txt", "zone_data/custom/M1/config.json"} <= files
    assert not any(name.endswith(("notes.txt", "d.txt")) for name in files), LINK_FOLLOWED
    assert {rel for rel, _size, _mtime in rm.stat_files(project)} == files
    tree = "\n".join(rm.list_directory(project, max_depth=6))
    assert "config.json" in tree
    assert "notes.txt" not in tree
    assert "d.txt" not in tree
    found = rm.glob_search(project, "*.txt")
    assert "src/a.txt" in found
    assert not any(name.endswith(("notes.txt", "d.txt")) for name in found)
    assert _drives_untouched(data)


@pytest.mark.parametrize("folder", ["", "zone_data/custom/M1", "zone_data/personal_drive"])
def test_deleting_or_packing_a_folder_that_holds_a_drive_is_refused(
    project: Path, data: Path, tmp_path_factory: pytest.TempPathFactory, folder: str
):
    root = project.joinpath(*folder.split("/")) if folder else project
    out = tmp_path_factory.mktemp("out") / "p.zip"
    with raises_zone(ZonePathRequiredError):
        rm.remove_dir(root)
    with raises_zone(ZonePathRequiredError):
        rm.create_archive(root, out)
    assert not out.exists()
    assert (project / "src" / "a.txt").exists()
    assert _drives_untouched(data)


def test_extracting_into_a_folder_that_holds_a_drive_is_refused(
    project: Path, data: Path, tmp_path_factory: pytest.TempPathFactory
):
    archive = tmp_path_factory.mktemp("zip") / "x.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("zone_data/personal_drive/Alice/notes.txt", "overwritten")
    with raises_zone(ZonePathRequiredError):
        rm.extract_archive(archive, project)
    assert _drives_untouched(data)


def test_a_drive_met_during_the_walk_refuses_the_whole_delete_and_archive(
    project: Path, data: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch
):
    """根的判斷沒擋到（例如經別的掛載路徑進到 data）：走訪途中遇到區根也整個拒絕，不做一半."""
    monkeypatch.setattr(rm_module, "contains_zone", lambda *_args, **_kwargs: False)
    out = tmp_path_factory.mktemp("out") / "p.zip"
    with raises_zone(ZonePathRequiredError):
        rm.remove_dir(project)
    with raises_zone(ZonePathRequiredError):
        rm.create_archive(project, out)
    assert not out.exists()
    assert (project / "src" / "a.txt").exists()
    assert _drives_untouched(data)


def test_a_link_in_a_project_is_skipped_when_packing_and_only_unlinked_when_deleting(
    data: Path, tmp_path_factory: pytest.TempPathFactory
):
    bob = data / "personal_drive" / "Bob"
    bob.mkdir()
    (bob / "x.txt").write_text(CANARY, encoding="utf-8")
    proj = tmp_path_factory.mktemp("proj")
    (proj / "a.txt").write_text("a", encoding="utf-8")
    make_dir_link(proj / "bob", bob)
    out = tmp_path_factory.mktemp("out") / "p.zip"
    rm.create_archive(proj, out)
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == [f"{proj.name}/a.txt"], LINK_FOLLOWED
    assert rm.iter_files(proj) == [proj / "a.txt"], LINK_FOLLOWED
    assert rm.glob_search(proj, "*.txt") == ["a.txt"], LINK_FOLLOWED
    rm.remove_dir(proj)
    assert not proj.exists()
    assert (bob / "x.txt").read_text("utf-8") == CANARY, LINK_FOLLOWED


def test_a_file_link_in_a_project_is_skipped_when_packing_and_only_unlinked_when_deleting(
    data: Path, tmp_path_factory: pytest.TempPathFactory
):
    """檔案的符號連結也一樣（R4）；這台 Windows 建不了符號連結，只在 POSIX 跑（CI 的 Linux）."""
    if sys.platform == "win32":
        return
    secret = data / "personal_drive" / "Bob" / "x.txt"
    secret.parent.mkdir()
    secret.write_text(CANARY, encoding="utf-8")
    proj = tmp_path_factory.mktemp("proj")
    (proj / "a.txt").write_text("a", encoding="utf-8")
    (proj / "peek.txt").symlink_to(secret)
    out = tmp_path_factory.mktemp("out") / "p.zip"
    rm.create_archive(proj, out)
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == [f"{proj.name}/a.txt"], LINK_FOLLOWED
    rm.remove_dir(proj)
    assert not proj.exists()
    assert secret.read_text("utf-8") == CANARY, LINK_FOLLOWED


def test_a_plain_project_lists_and_finds_as_before(tmp_path_factory: pytest.TempPathFactory):
    proj = tmp_path_factory.mktemp("proj")
    (proj / "a.txt").write_text("a", encoding="utf-8")
    (proj / "sub").mkdir()
    (proj / "sub" / "b.txt").write_text("b", encoding="utf-8")
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "c.txt").write_text("c", encoding="utf-8")
    target = tmp_path_factory.mktemp("target")
    make_dir_link(proj / "link", target)
    assert rm.list_directory(proj, max_depth=2, exclude=frozenset({"node_modules"})) == [
        f"📁 {proj}:",
        "├── a.txt",
        "├── link (link, not followed)",
        "└── sub/",
        "    └── b.txt",
    ]
    assert rm.list_directory(proj, max_depth=1, exclude=frozenset({"node_modules"}))[-1] == (
        "    ... (max depth 1 reached)"
    )
    assert sorted(rm.glob_search(proj, "*.txt")) == ["a.txt", "sub/b.txt"]
    assert sorted(rm.glob_search(proj, "**/*.txt")) == ["a.txt", "sub/b.txt"]
    assert rm.glob_search(proj, "sub/*.txt") == ["sub/b.txt"]
    rows = rm.stat_files(proj, exclude_names=frozenset({"node_modules"}))
    assert [rel for rel, _size, _mtime in rows] == ["a.txt", "sub/b.txt"]
    missing = proj / "missing"
    assert rm.list_directory(missing) == [f"Error: Directory not found: {missing}"]
