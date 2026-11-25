"""讀 git 的 index（下載專案的名單，ADR-0332 PR B）.

``utils.git_index.read_index`` 是純函式：只拿 bytes，不碰檔案系統、不跑 git。支援 index v2～v4；
一般檔、連結、子模組分開，去重；``link``（split index）、``sdir``（sparse index）、SHA-256、
未知的小寫擴充段一律拒絕（``index_unsupported``）；大寫開頭的擴充段略過（git 的規則）；
長度、填充、名稱長度、檢查碼不符都是 ``index_invalid``；項目數超過上限是 ``index_too_large``。
大部分 index 用這裡的寫入器手工組（每個欄位看得見）；另有一組用真的 git 產生（沒有 git 就跳過）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
from typing import TYPE_CHECKING

import pytest
from src.services import execution_broker
from src.utils import git_index
from src.utils.git_index import GitIndexError, read_index

if TYPE_CHECKING:
    from pathlib import Path

REGULAR, EXECUTABLE, LINK, GITLINK = 0o100644, 0o100755, 0o120000, 0o160000
LIMIT = 1000


def _varint(value: int) -> bytes:
    """git 的 offset varint（``encode_varint``）."""
    out = [value & 0x7F]
    value >>= 7
    while value:
        value -= 1
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


def _entry(
    path: bytes, mode: int, *, version: int, prev: bytes, extended: int = 0, stage: int = 0,
    name_len: int | None = None,
) -> bytes:
    flags = (stage << 12) | min(len(path) if name_len is None else name_len, 0xFFF)
    if extended:
        flags |= 0x4000
    head = struct.pack(">10I", 0, 0, 0, 0, 0, 0, mode, 0, 0, 0) + bytes(20)
    head += struct.pack(">H", flags)
    if extended:
        head += struct.pack(">H", extended)
    if version == 4:
        common = len(os.path.commonprefix([prev, path]))
        return head + _varint(len(prev) - common) + path[common:] + b"\0"
    size = (len(head) + len(path) + 8) & ~7
    body = head + path
    return body + b"\0" * (size - len(body))


def _ext(signature: bytes, data: bytes = b"") -> bytes:
    return signature + struct.pack(">I", len(data)) + data


def _index(
    entries: list[tuple], *, version: int = 2, extensions: bytes = b"", trailer: str = "sha1",
    count: int | None = None,
) -> bytes:
    body = b"DIRC" + struct.pack(">II", version, len(entries) if count is None else count)
    prev = b""
    for path, mode, *rest in entries:
        options = rest[0] if rest else {}
        body += _entry(path, mode, version=version, prev=prev, **options)
        prev = path
    body += extensions
    if trailer == "sha1":
        return body + hashlib.sha1(body, usedforsecurity=False).digest()
    if trailer == "zero":
        return body + bytes(20)
    return body + hashlib.sha256(body).digest()


def _reason(data: bytes) -> str:
    with pytest.raises(GitIndexError) as raised:
        read_index(data, max_entries=LIMIT)
    return raised.value.reason


TREE = [
    (b"README.md", REGULAR),
    (b"src/app.py", EXECUTABLE),
    (b"src/lib/" + b"deep" * 70 + b".txt", REGULAR),  # 長名稱（v4 的前綴壓縮也要對）
    (b"src/lib/util.py", REGULAR),
]


@pytest.mark.parametrize("version", [2, 3, 4])
def test_each_supported_version_lists_the_tracked_files(version):
    found = read_index(_index(TREE, version=version), max_entries=LIMIT)
    assert found.files == tuple(path for path, _mode in TREE)
    assert found.links == () and found.submodules == ()


def test_links_and_submodules_are_kept_apart_and_conflicts_are_one_file():
    entries = [
        (b"a.txt", REGULAR, {"stage": 1}), (b"a.txt", REGULAR, {"stage": 2}),
        (b"a.txt", REGULAR, {"stage": 3}), (b"link", LINK), (b"vendor/lib", GITLINK),
    ]
    found = read_index(_index(entries), max_entries=LIMIT)
    assert found.files == (b"a.txt",)
    assert found.links == (b"link",) and found.submodules == (b"vendor/lib",)


def test_extended_flags_are_read_in_v3_and_refused_in_v2():
    entries = [(b"new.txt", REGULAR, {"extended": 0x2000})]  # intent-to-add
    assert read_index(_index(entries, version=3), max_entries=LIMIT).files == (b"new.txt",)
    assert _reason(_index(entries, version=2)) == git_index.INDEX_INVALID


def test_optional_extensions_are_skipped():
    extensions = _ext(b"TREE", b"\0" * 9) + _ext(b"EOIE", b"\0" * 24)
    found = read_index(_index(TREE, extensions=extensions), max_entries=LIMIT)
    assert len(found.files) == len(TREE)


@pytest.mark.parametrize("signature", [b"link", b"sdir", b"abcd"])
def test_required_extensions_are_refused(signature):
    """split index（``link``）、sparse index（``sdir``）與不認得的小寫擴充段：git 自己也要懂才讀."""
    assert _reason(_index(TREE, extensions=_ext(signature, b"\0" * 8))) == (
        git_index.INDEX_UNSUPPORTED
    )


def test_a_skipped_checksum_is_accepted():
    """``index.skipHash`` 寫出全零的檢查碼."""
    assert read_index(_index(TREE, trailer="zero"), max_entries=LIMIT).files[0] == b"README.md"


def test_a_sha256_repository_is_refused_as_unsupported():
    assert _reason(_index(TREE, trailer="sha256")) == git_index.INDEX_UNSUPPORTED


@pytest.mark.parametrize("version", [1, 5])
def test_other_versions_are_unsupported(version):
    assert _reason(_index(TREE, version=version)) == git_index.INDEX_UNSUPPORTED


def test_too_many_entries_is_refused_before_reading_them():
    assert _reason(_index(TREE, count=LIMIT + 1)) == git_index.INDEX_TOO_LARGE


def _corrupt_padding() -> bytes:
    data = bytearray(_index([(b"a.txt", REGULAR)]))
    data[12 + 62 + len(b"a.txt") + 1] = 0x41  # 填充應該是 NUL
    body = bytes(data[:-20])
    return body + hashlib.sha1(body, usedforsecurity=False).digest()


def _resign(data: bytes) -> bytes:
    body = data[:-20]
    return body + hashlib.sha1(body, usedforsecurity=False).digest()


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"DIRC", id="too-short"),
        pytest.param(b"XXXX" + _index(TREE)[4:], id="signature"),
        pytest.param(_index(TREE)[:-21] + b"\0", id="checksum"),
        pytest.param(_resign(_index(TREE)[:40]), id="truncated-entry"),
        pytest.param(_corrupt_padding(), id="padding"),
        pytest.param(_index([(b"a.txt", REGULAR, {"name_len": 9})]), id="name-length"),
        pytest.param(_index(TREE, count=len(TREE) + 1), id="count-over-the-data"),
        pytest.param(_index(TREE, extensions=b"TREE\0\0\1\0"), id="extension-overrun"),
        pytest.param(_index([(b"", REGULAR)]), id="empty-name"),
        pytest.param(_index([(b"a", 0o020000)]), id="unknown-mode"),
    ],
)
def test_broken_indexes_are_invalid(data):
    assert _reason(data) == git_index.INDEX_INVALID


def test_a_folder_entry_is_a_sparse_index():
    """sparse index 把整個資料夾記成一項（mode 040000）：讀不懂，不是壞掉."""
    assert _reason(_index([(b"dir/", 0o040000)])) == git_index.INDEX_UNSUPPORTED


def test_a_name_longer_than_git_allows_is_invalid():
    name = b"x" * (git_index.MAX_NAME_BYTES + 1)
    assert _reason(_index([(name, REGULAR)])) == git_index.INDEX_INVALID


def test_v4_names_that_expand_past_the_budget_are_too_large(monkeypatch):
    """v4 的前綴壓縮讓很小的檔案展開成很長的名稱：展開後的總長有上限."""
    monkeypatch.setattr(git_index, "MAX_TOTAL_NAME_BYTES", 600)
    entries = [(b"p" * 200 + bytes([ord("a") + i]), REGULAR) for i in range(5)]
    assert _reason(_index(entries, version=4)) == git_index.INDEX_TOO_LARGE


def test_a_v4_entry_that_strips_more_than_the_previous_name_is_invalid():
    head = b"DIRC" + struct.pack(">II", 4, 1)
    entry = struct.pack(">10I", 0, 0, 0, 0, 0, 0, REGULAR, 0, 0, 0) + bytes(20)
    entry += struct.pack(">H", 1) + _varint(3) + b"a\0"
    assert _reason(_resign(head + entry + bytes(20))) == git_index.INDEX_INVALID


# --- 真的 git 產生的 index ----------------------------------------------------------------

GIT = shutil.which("git")
_NEUTRAL_CONFIG = (
    "-c", "core.splitIndex=false", "-c", "core.fsmonitor=false", "-c", "index.version=2",
    "-c", "feature.manyFiles=false",
)


def _git(repo: Path, *args: str) -> None:
    """在暫存 repo 跑 git；關掉會改變 index 格式或跑程式的設定（不受本機全域設定影響）."""
    assert GIT is not None
    execution_broker.run_sync(
        [GIT, "-C", str(repo), *_NEUTRAL_CONFIG, *args], capture_output=True, check=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if GIT is None:
        pytest.skip("git is not installed here")
    repo = tmp_path / "repo"
    (repo / "src" / "lib").mkdir(parents=True)
    for rel in ("README.md", "src/app.py", "src/lib/util.py"):
        (repo / rel).write_text(rel, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    return repo


@pytest.mark.parametrize("version", ["2", "3", "4"])
def test_indexes_written_by_git_are_read(repo, version):
    (repo / "new.txt").write_text("n", encoding="utf-8")
    _git(repo, "add", "-N", "new.txt")  # intent-to-add：v3 以上有延伸旗標
    _git(repo, "update-index", "--index-version", version)
    found = read_index((repo / ".git" / "index").read_bytes(), max_entries=LIMIT)
    assert set(found.files) == {b"README.md", b"new.txt", b"src/app.py", b"src/lib/util.py"}


def test_a_split_index_written_by_git_is_refused(repo):
    _git(repo, "update-index", "--split-index")
    data = (repo / ".git" / "index").read_bytes()
    with pytest.raises(GitIndexError) as raised:
        read_index(data, max_entries=LIMIT)
    assert raised.value.reason == git_index.INDEX_UNSUPPORTED
