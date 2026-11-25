"""讀 git 的 index（``.git/index``）：下載專案時決定放哪些檔案（ADR-0332 PR B）.

純函式，只拿 bytes：不碰檔案系統、不跑 git（index 是代理人寫得到的，跑 git 會讓它的設定跑程式）。
格式見 git 的 ``Documentation/gitformat-index.txt``：

- 版本 2～4；v3 起可有延伸旗標；v4 的名稱以前一個名稱做前綴壓縮（git 的 offset varint）。
- 一般檔案、連結（120000）、子模組（160000）分開；衝突的多個 stage 算一個名稱，只要有一個
  stage 是子模組或連結就算那一種（偏嚴）。資料夾項目（040000）只有 sparse index 有。
- 擴充段：大寫開頭的可略過；小寫開頭的（``link`` 是 split index、``sdir`` 是 sparse index，
  其他不認得的也一樣）git 自己也要懂才讀，這裡不懂就拒絕。
- 檢查碼：SHA-1；全零是 ``index.skipHash``，接受；SHA-256 的 repository 拒絕。

讀不懂的是 ``index_unsupported``，壞掉的是 ``index_invalid``，太大的是 ``index_too_large``。
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

INDEX_INVALID = "index_invalid"
INDEX_UNSUPPORTED = "index_unsupported"
INDEX_TOO_LARGE = "index_too_large"
# 讀 index 之前（``ResourceManager.read_git_index`` 回的狀態）
INDEX_OK = "ok"
NO_GIT = "no_git"
GIT_IS_LINK = "git_is_link"
GIT_NOT_FOLDER = "git_not_folder"  # ``.git`` 是檔案（例如 worktree 的指標）
NO_INDEX = "no_index"

# 一個名稱最長多少位元組（Linux 的 PATH_MAX）；v4 展開後的名稱總長上限（防小檔案展開成巨量）
MAX_NAME_BYTES = 4096
MAX_TOTAL_NAME_BYTES = 64 * 1024 * 1024

_HEADER = struct.Struct(">4sII")
_ENTRY_FIXED = 62  # ctime、mtime、dev、ino、mode、uid、gid、size（各 4）＋物件編號 20＋旗標 2
_MODE_OFFSET = 24
_FLAGS_OFFSET = 60
_SHA1_BYTES = 20
_SHA256_BYTES = 32
_NAME_MASK = 0xFFF
_EXTENDED_FLAG = 0x4000
_TYPE_MASK = 0o170000
_REGULAR, _LINK, _GITLINK, _TREE = 0o100000, 0o120000, 0o160000, 0o040000
_KINDS = {_REGULAR: "file", _LINK: "link", _GITLINK: "submodule"}
_STRICTNESS = {"file": 0, "link": 1, "submodule": 2}
_SUPPORTED_VERSIONS = frozenset({2, 3, 4})


class GitIndexError(Exception):
    """index 讀不了；``reason`` 是代碼（``INDEX_*``）."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class IndexEntries:
    """index 裡的名稱（相對 repository 根、``/`` 分隔的 bytes），依 index 的順序."""

    files: tuple[bytes, ...]
    links: tuple[bytes, ...]
    submodules: tuple[bytes, ...]


def read_index(data: bytes, *, max_entries: int) -> IndexEntries:
    """解析 index；不合格丟 ``GitIndexError``."""
    if len(data) < _HEADER.size + _SHA1_BYTES:
        raise GitIndexError(INDEX_INVALID)
    signature, version, count = _HEADER.unpack_from(data)
    if signature != b"DIRC":
        raise GitIndexError(INDEX_INVALID)
    if version not in _SUPPORTED_VERSIONS:
        raise GitIndexError(INDEX_UNSUPPORTED)
    _check_trailer(data)
    if count > max_entries:
        raise GitIndexError(INDEX_TOO_LARGE)
    end = len(data) - _SHA1_BYTES
    kinds, pos, has_empty = _read_entries(data, version, count, end)
    _check_extensions(data, pos, end)  # 先看擴充段：split index 的項目名稱是空的
    if has_empty:
        raise GitIndexError(INDEX_INVALID)
    grouped: dict[str, list[bytes]] = {"file": [], "link": [], "submodule": []}
    for name, kind in kinds.items():
        grouped[kind].append(name)
    return IndexEntries(
        tuple(grouped["file"]), tuple(grouped["link"]), tuple(grouped["submodule"])
    )


def _check_trailer(data: bytes) -> None:
    body, trailer = data[:-_SHA1_BYTES], data[-_SHA1_BYTES:]
    if trailer == hashlib.sha1(body, usedforsecurity=False).digest():
        return
    if trailer == bytes(_SHA1_BYTES):
        return  # index.skipHash
    if len(data) >= _HEADER.size + _SHA256_BYTES:
        body256, trailer256 = data[:-_SHA256_BYTES], data[-_SHA256_BYTES:]
        if trailer256 == hashlib.sha256(body256).digest():
            raise GitIndexError(INDEX_UNSUPPORTED)
    raise GitIndexError(INDEX_INVALID)


def _read_entries(
    data: bytes, version: int, count: int, end: int
) -> tuple[dict[bytes, str], int, bool]:
    """依序讀 ``count`` 個項目；回（名稱 → 種類, 擴充段的起點, 有沒有空名稱）."""
    kinds: dict[bytes, str] = {}
    pos, previous, total, has_empty = _HEADER.size, b"", 0, False
    for _ in range(count):
        if pos + _ENTRY_FIXED > end:
            raise GitIndexError(INDEX_INVALID)
        mode = struct.unpack_from(">I", data, pos + _MODE_OFFSET)[0]
        flags = struct.unpack_from(">H", data, pos + _FLAGS_OFFSET)[0]
        name_start = pos + _ENTRY_FIXED
        if flags & _EXTENDED_FLAG:
            if version < 3:
                raise GitIndexError(INDEX_INVALID)
            name_start += 2
        if version == 4:
            name, pos = _v4_name(data, name_start, end, previous, flags & _NAME_MASK)
        else:
            name, pos = _padded_name(data, pos, name_start, end, flags & _NAME_MASK)
        total += len(name)
        if total > MAX_TOTAL_NAME_BYTES:
            raise GitIndexError(INDEX_TOO_LARGE)
        kind = _kind(mode)
        previous = name
        if not name:
            has_empty = True
            continue
        if _STRICTNESS[kind] >= _STRICTNESS[kinds.get(name, "file")]:
            kinds[name] = kind
    return kinds, pos, has_empty


def _kind(mode: int) -> str:
    kind = _KINDS.get(mode & _TYPE_MASK)
    if kind is not None:
        return kind
    if mode & _TYPE_MASK == _TREE:
        raise GitIndexError(INDEX_UNSUPPORTED)  # sparse index 的資料夾項目
    raise GitIndexError(INDEX_INVALID)


def _checked_name(name: bytes, declared: int) -> bytes:
    """不能有 NUL、不能太長、要和旗標的長度相符；空名稱由呼叫端看完擴充段後再判."""
    if b"\0" in name or len(name) > MAX_NAME_BYTES:
        raise GitIndexError(INDEX_INVALID)
    if declared < _NAME_MASK and declared != len(name):
        raise GitIndexError(INDEX_INVALID)
    return name


def _padded_name(
    data: bytes, entry_start: int, name_start: int, end: int, declared: int
) -> tuple[bytes, int]:
    """v2／v3：名稱之後補 1～8 個 NUL 到 8 的倍數；名稱長度 0xFFF 表示要自己找結尾."""
    if declared < _NAME_MASK:
        name_end = name_start + declared
    else:
        name_end = data.find(b"\0", name_start, end)
        if name_end < 0:
            raise GitIndexError(INDEX_INVALID)
    header = name_start - entry_start
    entry_end = entry_start + ((header + (name_end - name_start) + 8) & ~7)
    if entry_end > end or data[name_end:entry_end].strip(b"\0"):
        raise GitIndexError(INDEX_INVALID)
    return _checked_name(data[name_start:name_end], declared), entry_end


def _v4_name(
    data: bytes, start: int, end: int, previous: bytes, declared: int
) -> tuple[bytes, int]:
    """v4：去掉前一個名稱結尾的幾個位元組，接上這裡到 NUL 為止的部分（沒有填充）."""
    strip, pos = _varint(data, start, end)
    if strip > len(previous):
        raise GitIndexError(INDEX_INVALID)
    suffix_end = data.find(b"\0", pos, end)
    if suffix_end < 0:
        raise GitIndexError(INDEX_INVALID)
    name = previous[: len(previous) - strip] + data[pos:suffix_end]
    return _checked_name(name, declared), suffix_end + 1


def _varint(data: bytes, pos: int, end: int) -> tuple[int, int]:
    """讀 git 的 offset varint（``decode_varint``）；回（值, 下一個位置）."""
    if pos >= end:
        raise GitIndexError(INDEX_INVALID)
    byte = data[pos]
    value = byte & 0x7F
    pos += 1
    while byte & 0x80:
        if pos >= end or value > MAX_NAME_BYTES:
            raise GitIndexError(INDEX_INVALID)
        byte = data[pos]
        value = ((value + 1) << 7) | (byte & 0x7F)
        pos += 1
    return value, pos


def _check_extensions(data: bytes, pos: int, end: int) -> None:
    """擴充段：簽名 4 位元組＋長度 4 位元組＋內容；要剛好讀到檢查碼前面."""
    while pos < end:
        if pos + 8 > end:
            raise GitIndexError(INDEX_INVALID)
        signature = data[pos : pos + 4]
        size = struct.unpack_from(">I", data, pos + 4)[0]
        if pos + 8 + size > end:
            raise GitIndexError(INDEX_INVALID)
        if not 0x41 <= signature[0] <= 0x5A:  # 小寫開頭：git 要懂才讀
            raise GitIndexError(INDEX_UNSUPPORTED)
        pos += 8 + size
