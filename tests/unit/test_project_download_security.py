"""從網頁下載專用空間的專案（``GET /ui/projects/{id}/download``，ADR-0332 PR B）.

- 誰能下載：只有專案的主人（人類本人）；沒有主人的舊專案只給人類管理員。管理員代打、靈魂、
  診斷身分、別的使用者、非主人的管理員都不行。
- 只有專用空間裡的專案：一般專案 422 ``not_dedicated_space``；結構層拒絕的照它的原因。
- 放哪些檔案只看 git 的名單（``.git/index``，純 Python 讀，不跑 git）：沒追蹤的不放；``.git``、
  解壓後會讓資料夾變成 repository 的（HEAD、其他版本控制工具的資料夾；用真的 git 驗證）、
  Windows 不能用的名稱、雙向控制字元、捷徑類檔案、``.env``、不是 UTF-8、路徑太長或太深、只差
  大小寫的重複、連結、子模組、硬連結、不是一般檔案、不見了、讀不到的都略過，寫進 zip 根的報告
  （每個原因只列前幾個、名稱截短）。
- 檔案在 ``<專案資料夾>/`` 底下；不跟隨連結地開；沒追蹤的資料夾不進去；碰到代理人資料夾整個拒絕。
- 被拒時說得出原因（根目錄檢查、別的專案也綁著、兩語的說明都有）；沒有追蹤任何檔案不給空 zip。
- 上限：名單大小與筆數、檔案數、資料夾數、總大小、走訪筆數與時間；同時只做一個（429）；斷線時
  刪掉 zip。
全部在 ``tmp_path`` 與測試沙盒；git 名單用這裡的寫入器手工組（每個欄位看得見）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from memory_server.utils.no_follow import LinkRefusedError, WalkReport
from memory_server.utils.zone_paths import ZonePathRequiredError
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.routers import ui_sessions
from src.services import execution_broker, project_download
from src.services import resource_manager as rm_module
from src.services.isolation import project_binding, project_space
from src.services.isolation.executor_session import PROJECT_ROOT_NAME
from src.services.isolation.project_binding import owns_folder as real_owns_folder
from src.services.project_download import REPORT_NAME
from src.services.resource_manager import resource_manager
from src.utils import git_index
from src.utils.temp_file_response import TempFileResponse
from src.utils.touka_dirs import PROTECTED_LOCATION, SCAN_LIMIT
from starlette.requests import ClientDisconnect

from tests.support.zone_records import expect_zone_refusal

if sys.platform == "win32":
    import _winapi

PROJECT_ID = "p_download"
FOLDER = "demo"
REGULAR, LINK, GITLINK = 0o100644, 0o120000, 0o160000

OWNER = AgentIdentity(agent_id="dlOwner", human_role="normal", is_human=True)
OTHER = AgentIdentity(agent_id="dlOther", human_role="normal", is_human=True)
ADMIN = AgentIdentity(agent_id="dlAdmin", human_role="admin", is_human=True)
ADMIN_ASSUMING = AgentIdentity(agent_id="dlOwner", human_role="admin", is_human=False)
SOUL = AgentIdentity.for_role(agent_id="dlSoul", role="Base", is_human=False, capabilities=[])
OWNER_NAMED_AGENT = AgentIdentity.for_role(
    agent_id="dlOwner", role="Owner", is_human=False, capabilities=[]
)
DEBUGGER = AgentIdentity.for_role(
    agent_id="DebugAgent", role="Debugger", is_human=False, capabilities=[]
)


def _index_bytes(entries: dict[bytes, int]) -> bytes:
    """git index v2（依名稱排序、SHA-1 檢查碼）."""
    body = b"DIRC" + struct.pack(">II", 2, len(entries))
    for path in sorted(entries):
        head = struct.pack(">10I", 0, 0, 0, 0, 0, 0, entries[path], 0, 0, 0) + bytes(20)
        head += struct.pack(">H", min(len(path), 0xFFF))
        size = (len(head) + len(path) + 8) & ~7
        body += (head + path).ljust(size, b"\0")
    return body + hashlib.sha1(body, usedforsecurity=False).digest()


def _make_dir_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        _winapi.CreateJunction(str(target), str(link))


@dataclass
class Space:
    project: Path
    temp: Path
    anchor: Path

    def track(self, files: dict[str, bytes], index_only: dict[bytes, int] | None = None) -> None:
        """把 ``files`` 寫到專案裡並列進 git 名單；``index_only`` 只列進名單（名稱是 bytes）."""
        entries = dict(index_only or {})
        for rel, data in files.items():
            target = self.project / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            entries[rel.encode("utf-8")] = REGULAR
        (self.project / ".git").mkdir(exist_ok=True)
        (self.project / ".git" / "index").write_bytes(_index_bytes(entries))


@pytest.fixture
def space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Space:
    workspace = (tmp_path / "workspace").resolve()
    anchor = workspace / PROJECT_ROOT_NAME
    project = anchor / FOLDER
    project.mkdir(parents=True)
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setattr(project_binding, "configured_projects_root", lambda: anchor)
    monkeypatch.setattr(project_binding, "owns_folder", AsyncMock(return_value=True))
    monkeypatch.setattr(resource_manager, "_get_workspace_root", AsyncMock(return_value=workspace))
    monkeypatch.setattr(resource_manager, "_is_docker", False)
    monkeypatch.setattr(ui_sessions, "get_project_root_path", AsyncMock(return_value=str(project)))
    monkeypatch.setattr(
        ui_sessions.session_service, "get_project_owner", AsyncMock(return_value=OWNER.agent_id)
    )
    return Space(project, temp, anchor)


async def _download(identity: AgentIdentity = OWNER) -> dict[str, bytes]:
    response = await ui_sessions.download_project(PROJECT_ID, identity)
    try:
        with zipfile.ZipFile(response.path) as zf:
            return {name: zf.read(name) for name in zf.namelist()}
    finally:
        resource_manager.remove_file(response.path)


async def _refused(identity: AgentIdentity = OWNER) -> ToukaException:
    with pytest.raises(ToukaException) as raised:
        await ui_sessions.download_project(PROJECT_ID, identity)
    return raised.value


def _report(contents: dict[str, bytes]) -> dict[str, list[str]]:
    """報告的 ``[原因]`` 標題與底下縮排兩格的路徑."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in contents[REPORT_NAME].decode("utf-8").splitlines():
        if line.startswith("[") and "]" in line:
            current = line[1 : line.index("]")]
            sections[current] = []
        elif line.startswith("  ") and current is not None:
            sections[current].append(line[2:])
    return sections


def _temp_zips(space: Space) -> list[str]:
    return [p.name for p in space.temp.iterdir() if p.name.startswith("touka_project_")]


# --- 誰能下載 ----------------------------------------------------------------------------


async def test_the_owner_gets_the_tracked_files_under_the_folder_name(space):
    space.track({"README.md": b"hello", "src/app.py": b"print(1)\n"})
    (space.project / "untracked.txt").write_bytes(b"not listed")
    contents = await _download()
    assert contents == {f"{FOLDER}/README.md": b"hello", f"{FOLDER}/src/app.py": b"print(1)\n"}
    assert _temp_zips(space) == []


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param(OTHER, id="another-user"),
        pytest.param(ADMIN, id="admin-who-is-not-the-owner"),
        pytest.param(ADMIN_ASSUMING, id="admin-assuming-a-soul"),
        pytest.param(SOUL, id="soul"),
        pytest.param(OWNER_NAMED_AGENT, id="agent-with-the-owners-id"),
        pytest.param(DEBUGGER, id="debugger"),
    ],
)
async def test_only_the_owner_may_download(space, identity):
    space.track({"README.md": b"hello"})
    refused = await _refused(identity)
    assert refused.detail["code"] == ErrorCode.AUTH_PERMISSION_DENIED.value


@pytest.mark.parametrize(
    ("identity", "allowed"),
    [
        pytest.param(ADMIN, True, id="human-admin"),
        pytest.param(OWNER, False, id="user"),
        pytest.param(ADMIN_ASSUMING, False, id="admin-assuming-a-soul"),
        pytest.param(DEBUGGER, False, id="debugger"),
    ],
)
async def test_an_ownerless_project_is_only_for_human_admins(space, monkeypatch, identity, allowed):
    monkeypatch.setattr(
        ui_sessions.session_service, "get_project_owner", AsyncMock(return_value=None)
    )
    space.track({"README.md": b"hello"})
    if allowed:
        assert await _download(identity) == {f"{FOLDER}/README.md": b"hello"}
    else:
        assert (await _refused(identity)).detail["code"] == ErrorCode.AUTH_PERMISSION_DENIED.value


async def test_a_project_without_a_root_is_not_found(space, monkeypatch):
    monkeypatch.setattr(ui_sessions, "get_project_root_path", AsyncMock(return_value=None))
    assert (await _refused()).detail["code"] == ErrorCode.SESSION_NOT_FOUND.value


# --- 只有專用空間 ------------------------------------------------------------------------


async def test_a_project_outside_the_dedicated_space_is_refused(space, monkeypatch):
    outside = space.anchor.parent / "plain"
    outside.mkdir()
    monkeypatch.setattr(ui_sessions, "get_project_root_path", AsyncMock(return_value=str(outside)))
    refused = await _refused()
    assert refused.status_code == 422
    assert refused.detail["code"] == ErrorCode.VALIDATION_ERROR.value
    assert refused.detail["reason"] == "not_dedicated_space"


@pytest.mark.parametrize(
    ("where", "reason"),
    [("anchor", "project_space_root"), ("second_level", "project_not_top_level")],
)
async def test_space_shapes_the_structure_refuses_keep_their_reason(
    space, monkeypatch, where, reason
):
    roots = {"anchor": space.anchor, "second_level": space.project / "src"}
    (space.project / "src").mkdir()
    monkeypatch.setattr(
        ui_sessions, "get_project_root_path", AsyncMock(return_value=str(roots[where]))
    )
    refused = await _refused()
    assert refused.status_code == 422 and refused.detail["reason"] == reason


@pytest.mark.parametrize("reason", ["protected_location", "scan_limit"])
async def test_a_root_the_files_page_refuses_says_why(space, monkeypatch, reason):
    """和登記相同的根目錄檢查（容器裡會掃 Touka 目錄；這裡讓它回原因）：422 帶原因."""
    space.track({"README.md": b"hello"})
    monkeypatch.setattr(ui_sessions.profiles, "project_root_reason", AsyncMock(return_value=reason))
    refused = await _refused()
    assert refused.status_code == 422 and refused.detail["reason"] == reason
    assert _temp_zips(space) == []


async def test_a_folder_another_project_also_claims_is_not_downloaded(space, monkeypatch):
    """舊資料裡別的專案也綁著同一個資料夾：不下載（409）."""
    space.track({"README.md": b"hello"})
    monkeypatch.setattr(project_binding, "owns_folder", AsyncMock(return_value=False))
    assert (await _refused()).detail["code"] == ErrorCode.PROJECT_FOLDER_TAKEN.value


@pytest.mark.parametrize(
    ("claims", "owns"),
    [
        pytest.param([(PROJECT_ID, FOLDER)], True, id="only-this-project"),
        pytest.param([], False, id="not-bound"),
        pytest.param([(PROJECT_ID, FOLDER), ("p_other", "Demo")], False, id="also-another"),
        pytest.param([(PROJECT_ID, "Demo")], False, id="other-letter-case"),
    ],
)
async def test_owning_a_folder_means_the_only_exact_claim(space, monkeypatch, claims, owns):
    monkeypatch.setattr(project_binding, "_claims", AsyncMock(return_value=claims))
    scope = project_space.structural_scope(space.project, space.anchor)
    assert await real_owns_folder(PROJECT_ID, scope) is owns


def _linked_project(space: Space, tmp_path: Path) -> Path:
    """專用空間裡的一個連結，指向外面一個有 git 名單的資料夾（檢查之後才被換成連結的情形）."""
    real = tmp_path / "real_project"
    (real / ".git").mkdir(parents=True)
    (real / "README.md").write_bytes(b"outside")
    (real / ".git" / "index").write_bytes(_index_bytes({b"README.md": REGULAR}))
    link = space.anchor / "linked"
    _make_dir_link(link, real)
    return link


def test_reading_the_list_does_not_follow_a_linked_root(space, tmp_path):
    with pytest.raises(LinkRefusedError):
        resource_manager.read_git_index(_linked_project(space, tmp_path), 1024)


def test_the_walk_does_not_follow_a_linked_root(space, tmp_path):
    walk = resource_manager.project_walk(
        _linked_project(space, tmp_path), WalkReport(),
        want=lambda _entry: True, skip_dir=lambda _parts: False,
    )
    with pytest.raises(LinkRefusedError):
        list(walk)


def test_a_root_swapped_for_a_link_after_the_check_is_refused(space, tmp_path):
    link = _linked_project(space, tmp_path)
    with pytest.raises(ToukaException) as raised:
        project_download.build_project_zip("linked", link)
    assert raised.value.detail["reason"] == "project_linked"
    assert _temp_zips(space) == []


@pytest.fixture
def drive_project(zone_data_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """data 根的上層當專案根，git 名單列著 Alice 個人空間裡的檔案（R4）."""
    root = zone_data_root.parent
    drive = zone_data_root / "personal_drive" / "Alice"
    drive.mkdir(parents=True)
    (drive / "notes.txt").write_bytes(b"CANARY")
    (root / ".git").mkdir()
    listed = f"{zone_data_root.name}/personal_drive/Alice/notes.txt".encode()
    (root / ".git" / "index").write_bytes(_index_bytes({listed: REGULAR}))
    temp = root / "zips"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    return root


@pytest.mark.parametrize("root_check", ["real", "missed"])
def test_a_download_that_reaches_an_agent_folder_is_refused(drive_project, monkeypatch, root_check):
    """根包含代理人資料夾，或根的檢查沒擋到、走訪途中才遇到：整個拒絕，不留 zip（R4）."""
    if root_check == "missed":
        monkeypatch.setattr(rm_module, "contains_zone", lambda *_args, **_kwargs: False)
    with expect_zone_refusal(ZonePathRequiredError), pytest.raises(ToukaException) as refused:
        project_download.build_project_zip("p", drive_project)
    assert refused.value.detail["reason"] == "protected_location"
    assert list((drive_project / "zips").iterdir()) == []


async def test_a_missing_project_folder_says_so(space, monkeypatch):
    """資料夾不在就說不在（先於根目錄檢查：容器裡掃不到不在的資料夾，會變成「受保護」）."""
    gone = space.anchor / "gone"
    monkeypatch.setattr(ui_sessions, "get_project_root_path", AsyncMock(return_value=str(gone)))
    monkeypatch.setattr(
        ui_sessions.profiles, "project_root_reason", AsyncMock(return_value="protected_location")
    )
    assert (await _refused()).detail["code"] == ErrorCode.PROJECT_FOLDER_MISSING.value


# --- git 的名單 -------------------------------------------------------------------------


def _no_git(space: Space) -> None:
    pass


def _git_is_a_file(space: Space) -> None:
    (space.project / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")


def _git_is_a_link(space: Space) -> None:
    elsewhere = space.anchor.parent / "elsewhere_git"
    elsewhere.mkdir()
    (elsewhere / "index").write_bytes(_index_bytes({b"README.md": REGULAR}))
    _make_dir_link(space.project / ".git", elsewhere)


def _no_index(space: Space) -> None:
    (space.project / ".git").mkdir()


def _index_is_a_folder(space: Space) -> None:
    (space.project / ".git" / "index").mkdir(parents=True)


def _broken_index(space: Space) -> None:
    (space.project / ".git").mkdir()
    (space.project / ".git" / "index").write_bytes(b"DIRC" + struct.pack(">II", 2, 0) + b"x" * 30)


def _empty_index(space: Space) -> None:
    (space.project / ".git").mkdir()
    (space.project / ".git" / "index").write_bytes(_index_bytes({}))


def _split_index(space: Space) -> None:
    (space.project / ".git").mkdir()
    data = _index_bytes({b"README.md": REGULAR})[:-20] + b"link" + struct.pack(">I", 0)
    (space.project / ".git" / "index").write_bytes(
        data + hashlib.sha1(data, usedforsecurity=False).digest()
    )


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (_no_git, "no_git"),
        (_git_is_a_file, "git_not_folder"),
        (_git_is_a_link, "git_is_link"),
        (_no_index, "no_index"),
        (_empty_index, "no_index"),
        (_index_is_a_folder, "index_invalid"),
        (_broken_index, "index_invalid"),
        (_split_index, "index_unsupported"),
    ],
)
async def test_git_problems_are_refused_with_their_reason(space, make, reason):
    (space.project / "README.md").write_bytes(b"hello")
    make(space)
    refused = await _refused()
    assert refused.status_code == 422
    assert refused.detail["code"] == ErrorCode.VALIDATION_ERROR.value
    assert refused.detail["reason"] == reason
    assert _temp_zips(space) == []


async def test_an_index_that_is_a_link_is_not_followed(space, tmp_path):
    elsewhere = tmp_path / "elsewhere_index"
    elsewhere.write_bytes(_index_bytes({b"README.md": REGULAR}))
    (space.project / "README.md").write_bytes(b"hello")
    (space.project / ".git").mkdir()
    try:
        (space.project / ".git" / "index").symlink_to(elsewhere)
    except OSError:
        pytest.skip("this machine cannot make file symlinks")
    refused = await _refused()
    assert refused.status_code == 422 and refused.detail["reason"] == "index_invalid"


async def test_an_index_bigger_than_the_limit_is_not_read(space, monkeypatch):
    space.track({"README.md": b"hello"})
    monkeypatch.setattr(project_download, "MAX_INDEX_BYTES", 16)
    refused = await _refused()
    assert refused.status_code == 413 and refused.detail["reason"] == "index_too_large"


async def test_an_index_with_too_many_entries_is_refused(space, monkeypatch):
    space.track({"a.txt": b"a", "b.txt": b"b"})
    monkeypatch.setattr(project_download, "MAX_INDEX_ENTRIES", 1)
    refused = await _refused()
    assert refused.status_code == 413 and refused.detail["reason"] == "index_too_large"


# --- 略過的檔案與報告 ----------------------------------------------------------------------

SKIPPED_BY_NAME = {
    "git_folder": [
        b".git/config", b"sub/.GIT/hooks/post-checkout", b"sub/git~1/config",
        ".git‌/config".encode(), b"sub/.git. /config", b"nested/.git",
    ],
    "vcs_metadata": [
        b"HEAD", b"bare/HEAD", b"sub/head ", b".hg/hgrc", b"sub/.SVN/entries",
        b"a/.bzr/branch.conf", b"_darcs/prefs/defaults", b".jj/repo/config.toml",
    ],
    "bad_name": [
        b"bad:name.txt", b"CON.txt", b"aux", b"com1.log", "COM¹.txt".encode(), b"CONIN$",
        b"lpt9", b"trail.", b"trail ", b"a\\b.txt", b"ctrl\x01.txt", b"a\nb.txt",
    ],
    "bidi": ["evil‮txt.exe".encode(), "x⁦y.txt".encode()],
    "shortcut": [
        b"desktop.ini", b"sub/Desktop.INI", b"x.lnk", b"y.URL", b"z.scf", b"q.library-ms",
        b"w.searchconnector-ms", b"t.theme", b"s.website", b"x.themepack", b"y.deskthemepack",
        b"kde/.directory",
    ],
    "env_file": [b".env", b"config/.env.local", b"sub/.ENV"],
    "not_utf8": [b"bad\xff.txt"],
    # 超過字數、超過層數、位元組多到不用解碼就知道太長（解碼的話是 not_utf8）
    "too_long": [b"d/" + b"x" * 250, b"/".join([b"d"] * 33), b"\xff" * 801],
}


async def test_names_that_must_not_reach_the_zip_are_skipped_and_reported(space):
    index_only = {name: REGULAR for names in SKIPPED_BY_NAME.values() for name in names}
    space.track({"README.md": b"hello", ".env.example": b"KEY=", "docs/ok.txt": b"ok"}, index_only)
    contents = await _download()
    files = {name for name in contents if name != REPORT_NAME}
    assert files == {f"{FOLDER}/README.md", f"{FOLDER}/.env.example", f"{FOLDER}/docs/ok.txt"}
    report = _report(contents)
    for reason, names in SKIPPED_BY_NAME.items():
        assert len(report[reason]) == len(names), reason


async def test_the_report_escapes_names_that_could_trick_a_reader(space):
    space.track(
        {"README.md": b"hello"},
        {"evil‮txt.exe".encode(): REGULAR, b"bad\xff.txt": REGULAR, b"a\nb.txt": REGULAR},
    )
    report = _report(await _download())
    assert report["bidi"] == ["evil\\u202etxt.exe"]
    assert report["not_utf8"] == ["bad\\xff.txt"]
    assert report["bad_name"] == ["a\\u000ab.txt"]
    text = "\n".join(line for lines in report.values() for line in lines)
    assert "‮" not in text and "\n" not in "".join(report["bad_name"])


GIT = shutil.which("git")


def _git_dir(folder: Path) -> Path | None:
    """git 從 ``folder`` 找到的 repository（找不到回 None）；允許 bare repository."""
    assert GIT is not None
    found = execution_broker.run_sync(
        [GIT, "-C", str(folder), "-c", "safe.bareRepository=all", "rev-parse",
         "--absolute-git-dir"],
        capture_output=True, text=True, check=False,
    )
    return Path(found.stdout.strip()).resolve() if found.returncode == 0 else None


async def test_a_tracked_bare_repository_is_not_one_after_unzipping(space, tmp_path):
    """追蹤一組 bare repository 的檔案：不放 HEAD，解壓後 git 不會把那個資料夾當 repository."""
    if GIT is None:
        pytest.skip("git is not installed here")
    space.track({
        "README.md": b"hello",
        "bare/HEAD": b"ref: refs/heads/main\n",
        "bare/config": b"[core]\n\tbare = true\n",
        "bare/objects/info/keep": b"",
        "bare/refs/heads/keep": b"",
    })
    assert _git_dir(space.project / "bare") == (space.project / "bare").resolve()  # 對照
    contents = await _download()
    assert _report(contents)["vcs_metadata"] == ["bare/HEAD"]
    out = tmp_path / "unzipped"
    for name, data in contents.items():
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(data)
    assert (out / FOLDER / "bare" / "config").exists()
    assert _git_dir(out / FOLDER / "bare") != (out / FOLDER / "bare").resolve()


async def test_the_report_lists_the_first_names_and_counts_the_rest(space, monkeypatch):
    monkeypatch.setattr(project_download, "MAX_REPORT_LINES", 2)
    space.track({"README.md": b"hello"}, {f"bad{i}:.txt".encode(): REGULAR for i in range(5)})
    contents = await _download()
    assert _report(contents)["bad_name"] == ["bad0:.txt", "bad1:.txt"]
    assert "... and 3 more / 另外還有 3 個" in contents[REPORT_NAME].decode("utf-8")


async def test_long_names_are_shortened_in_the_report(space):
    long = "d/" + "x" * 600
    space.track({"README.md": b"hello"}, {long.encode(): REGULAR})
    assert _report(await _download())["too_long"] == [long[:300] + "..."]


async def test_case_duplicates_keep_the_first_one(space):
    space.track({"README.md": b"first"}, {b"Readme.MD": REGULAR, b"readme.md/x": REGULAR})
    contents = await _download()
    assert contents[f"{FOLDER}/README.md"] == b"first"
    assert _report(contents)["case_duplicate"] == ["Readme.MD", "readme.md/x"]


async def test_links_and_submodules_in_the_index_are_skipped(space):
    (space.project / "vendor" / "lib").mkdir(parents=True)
    (space.project / "vendor" / "lib" / "inner.txt").write_bytes(b"inside the submodule")
    space.track({"README.md": b"hello"}, {b"link.txt": LINK, b"vendor/lib": GITLINK})
    (space.project / "link.txt").write_bytes(b"a plain file where git has a link")
    contents = await _download()
    assert set(contents) == {f"{FOLDER}/README.md", REPORT_NAME}
    report = _report(contents)
    assert report["link"] == ["link.txt"] and report["submodule"] == ["vendor/lib"]


async def test_what_is_on_disk_decides_too(space, tmp_path):
    """名單說是檔案，但磁碟上是硬連結、資料夾、不見了：都略過並寫進報告."""
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_bytes(b"another project's file")
    (space.project / "docs").mkdir()
    os.link(elsewhere, space.project / "docs" / "hard.txt")
    space.track(
        {"README.md": b"hello"},
        {b"docs/hard.txt": REGULAR, b"gone.txt": REGULAR, b"dirfile": REGULAR},
    )
    (space.project / "dirfile").mkdir()
    contents = await _download()
    assert set(contents) == {f"{FOLDER}/README.md", REPORT_NAME}
    report = _report(contents)
    assert report["hardlink"] == ["docs/hard.txt"]
    assert report["not_a_file"] == ["dirfile"]
    assert report["missing"] == ["gone.txt"]


async def test_a_tracked_file_swapped_for_a_link_is_not_followed(space, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"secret")
    space.track({"README.md": b"hello", "swapped.txt": b"ok"})
    (space.project / "swapped.txt").unlink()
    try:
        (space.project / "swapped.txt").symlink_to(secret)
    except OSError:
        pytest.skip("this machine cannot make file symlinks")
    contents = await _download()
    assert f"{FOLDER}/swapped.txt" not in contents
    assert _report(contents)["link"] == ["swapped.txt"]


async def test_a_tracked_file_under_a_linked_folder_is_not_followed(space, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_bytes(b"leak")
    space.track({"README.md": b"hello", "sub/leak.txt": b"x"})
    (space.project / "sub" / "leak.txt").unlink()
    (space.project / "sub").rmdir()
    _make_dir_link(space.project / "sub", outside)
    contents = await _download()
    assert f"{FOLDER}/sub/leak.txt" not in contents
    assert _report(contents)["link"] == ["sub/leak.txt"]


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions and not root"
)
async def test_an_unreadable_tracked_file_is_reported(space):
    space.track({"README.md": b"hello", "locked.txt": b"x"})
    (space.project / "locked.txt").chmod(0)
    try:
        contents = await _download()
    finally:
        (space.project / "locked.txt").chmod(0o600)
    assert _report(contents)["unreadable"] == ["locked.txt"]


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions and not root"
)
async def test_a_file_whose_folder_cannot_be_searched_is_unreadable_not_missing(space):
    """資料夾讀得到名稱但不能進去（沒有 x）：裡面的檔案是讀不到，不是不見了."""
    space.track({"README.md": b"hello", "locked/a.txt": b"x"})
    (space.project / "locked").chmod(0o444)
    try:
        contents = await _download()
    finally:
        (space.project / "locked").chmod(0o755)
    assert _report(contents)["unreadable"] == ["locked/a.txt"]


async def test_no_report_when_nothing_was_skipped(space):
    space.track({"README.md": b"hello"})
    assert REPORT_NAME not in await _download()


async def test_old_and_far_future_times_still_zip(space):
    space.track({"old.txt": b"o", "future.txt": b"f"})
    os.utime(space.project / "old.txt", (0, 0))
    os.utime(space.project / "future.txt", (7_258_118_400, 7_258_118_400))  # 2200 年
    response = await ui_sessions.download_project(PROJECT_ID, OWNER)
    try:
        with zipfile.ZipFile(response.path) as zf:
            assert zf.getinfo(f"{FOLDER}/old.txt").date_time == (1980, 1, 1, 0, 0, 0)
            assert zf.getinfo(f"{FOLDER}/future.txt").date_time == (2107, 12, 31, 23, 59, 58)
    finally:
        resource_manager.remove_file(response.path)


# --- 上限 ---------------------------------------------------------------------------------


async def test_too_many_files_is_refused_before_packing(space, monkeypatch):
    space.track({"a.txt": b"a", "b.txt": b"b"})
    monkeypatch.setattr(project_download, "MAX_DOWNLOAD_FILES", 1)
    refused = await _refused()
    assert refused.status_code == 413 and refused.detail["reason"] == "too_many_files"
    assert _temp_zips(space) == []


async def test_too_many_folders_is_refused_before_packing(space, monkeypatch):
    space.track({"a/x.txt": b"a", "b/y.txt": b"b"})
    monkeypatch.setattr(project_download, "MAX_DOWNLOAD_DIRS", 1)
    refused = await _refused()
    assert refused.status_code == 413 and refused.detail["reason"] == "too_many_files"
    assert _temp_zips(space) == []


async def test_too_many_bytes_is_refused_and_the_partial_zip_removed(space, monkeypatch):
    space.track({"a.txt": b"a" * 8, "b.txt": b"b" * 8})
    monkeypatch.setattr(project_download, "MAX_DOWNLOAD_BYTES", 10)
    refused = await _refused()
    assert refused.status_code == 413 and refused.detail["reason"] == "too_large"
    assert _temp_zips(space) == []


async def test_untracked_folders_are_not_walked(space, monkeypatch):
    """沒追蹤的資料夾（例如 node_modules）不進去：走訪筆數上限只算到追蹤的資料夾."""
    space.track({"README.md": b"hello", "src/app.py": b"x"})
    junk = space.project / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    for i in range(50):
        (junk / f"f{i}.js").write_bytes(b"x")
    monkeypatch.setattr(project_download, "MAX_WALK_ENTRIES", 10)
    assert set(await _download()) == {f"{FOLDER}/README.md", f"{FOLDER}/src/app.py"}


async def test_a_walk_with_too_many_entries_is_refused(space, monkeypatch):
    space.track({"README.md": b"hello"})
    for i in range(20):
        (space.project / f"untracked{i}.txt").write_bytes(b"x")
    monkeypatch.setattr(project_download, "MAX_WALK_ENTRIES", 10)
    refused = await _refused()
    assert refused.status_code == 413 and refused.detail["reason"] == "walk_too_large"
    assert _temp_zips(space) == []


async def test_a_walk_that_takes_too_long_is_refused(space, monkeypatch):
    space.track({"a.txt": b"a", "b.txt": b"b"})
    clock = iter(range(0, 10**9, 10**8))
    monkeypatch.setattr(project_download, "get_monotonic_ms", lambda: next(clock))
    monkeypatch.setattr(project_download, "MAX_WALK_SECONDS", 1)
    refused = await _refused()
    assert refused.status_code == 413 and refused.detail["reason"] == "walk_too_large"


async def test_checking_the_list_counts_toward_the_time_limit(space, monkeypatch):
    """計時從讀名單開始：名單上的檔案都不在磁碟上（走訪沒有東西可算）也會超時."""
    space.track({}, {b"gone1.txt": REGULAR, b"gone2.txt": REGULAR})
    clock = iter(range(0, 10**9, 10**8))
    monkeypatch.setattr(project_download, "get_monotonic_ms", lambda: next(clock))
    monkeypatch.setattr(project_download, "MAX_WALK_SECONDS", 1)
    refused = await _refused()
    assert refused.status_code == 413 and refused.detail["reason"] == "walk_too_large"


async def test_a_zip_that_cannot_be_removed_does_not_hide_the_refusal(space, monkeypatch):
    """做一半的 zip 刪不掉（例如被別的程式開著）：照樣回原本的拒絕，只記 log."""
    space.track({"a.txt": b"a" * 8, "b.txt": b"b" * 8})
    monkeypatch.setattr(project_download, "MAX_DOWNLOAD_BYTES", 10)
    locked = Mock(side_effect=PermissionError("in use"))
    monkeypatch.setattr(resource_manager, "remove_file", locked)
    refused = await _refused()
    assert refused.status_code == 413 and refused.detail["reason"] == "too_large"
    assert locked.called


# --- 畫面說得出原因 -------------------------------------------------------------------------

_LOCALES = Path(__file__).resolve().parents[2] / "ToukaChat" / "src" / "locales"


@pytest.mark.parametrize("language", ["zh-TW", "en"])
def test_every_refusal_reason_has_wording_on_the_projects_page(language):
    """下載被拒的每個原因在專案頁都有說明（沒有就只剩一般的「下載失敗」）."""
    reasons = {
        project_download.NOT_DEDICATED_SPACE, project_download.TOO_MANY_FILES,
        project_download.TOO_LARGE, project_download.WALK_TOO_LARGE,
        git_index.NO_GIT, git_index.GIT_NOT_FOLDER, git_index.GIT_IS_LINK, git_index.NO_INDEX,
        git_index.INDEX_UNSUPPORTED, git_index.INDEX_INVALID, git_index.INDEX_TOO_LARGE,
        PROTECTED_LOCATION, SCAN_LIMIT, ui_sessions._OUTSIDE_WORKSPACE,
    }
    catalog = json.loads((_LOCALES / language / "translation.json").read_text(encoding="utf-8"))
    assert reasons <= set(catalog["projects"]["projects_view"]["download_reasons"])


# --- 同時只做一個、斷線時清掉 --------------------------------------------------------------


@pytest.fixture
def blocked(space, monkeypatch, tmp_path):
    """打包卡住直到放行；回（開始了, 放行, 做好的 zip）."""
    started, release = threading.Event(), threading.Event()
    made = tmp_path / "made.zip"

    def slow(folder: str, root: Path) -> project_download.ProjectArchive:
        started.set()
        release.wait(10)
        made.write_bytes(b"zip")
        return project_download.ProjectArchive(made, files=1, skipped=0, size=3)

    monkeypatch.setattr(project_download, "build_project_zip", slow)
    space.track({"README.md": b"hello"})
    return started, release, made


async def _settle(done) -> None:
    for _ in range(200):
        if done():
            return
        await asyncio.sleep(0.01)


async def test_one_download_at_a_time(blocked):
    started, release, made = blocked
    first = asyncio.create_task(ui_sessions.download_project(PROJECT_ID, OWNER))
    await asyncio.to_thread(started.wait, 10)
    busy = await asyncio.wait_for(_refused(), 5)  # 不是排隊等：馬上拒絕
    assert busy.status_code == 429
    assert busy.detail["code"] == ErrorCode.PROJECT_DOWNLOAD_BUSY.value
    release.set()
    response = await first
    assert Path(response.path) == made
    assert not project_download._DOWNLOAD_SLOT.locked()


async def test_a_cancelled_download_removes_its_zip_and_frees_the_slot(blocked):
    started, release, made = blocked
    task = asyncio.create_task(ui_sessions.download_project(PROJECT_ID, OWNER))
    await asyncio.to_thread(started.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await _settle(lambda: not project_download._DOWNLOAD_SLOT.locked() and not made.exists())
    assert not made.exists()
    assert not project_download._DOWNLOAD_SLOT.locked()


async def test_the_response_is_a_no_store_zip_that_removes_itself(space):
    space.track({"README.md": b"hello"})
    response = await ui_sessions.download_project(PROJECT_ID, OWNER)
    assert isinstance(response, TempFileResponse)
    assert response.media_type == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert FOLDER in response.headers["content-disposition"]
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request"}

    await response({"type": "http", "method": "GET", "headers": []}, receive, send)
    assert sent[0]["status"] == 200
    assert not Path(response.path).exists()


async def test_the_zip_is_removed_even_when_the_browser_hangs_up(space):
    space.track({"README.md": b"hello"})
    response = await ui_sessions.download_project(PROJECT_ID, OWNER)

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body":
            raise OSError  # 連線斷了

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    # Starlette 可能把 OSError 換成 ClientDisconnect
    with pytest.raises((OSError, ClientDisconnect)):
        await response({"type": "http", "method": "GET", "headers": []}, receive, send)
    assert not Path(response.path).exists()
