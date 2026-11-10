"""開放名單（ADR-0311）：使用者在這台電腦上開放了哪些資料夾，每次使用都重新核對。"""

from __future__ import annotations

import json
import ntpath
import threading
from dataclasses import asdict, dataclass
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Final

from touka_local.errors import Refused
from touka_local.fsops import durable_write_text, read_durable_text
from touka_local.paths import (
    anchor_of,
    check_segment_syntax,
    is_plain_absolute,
    join,
    normalize_folder,
    parts_of,
    related,
    same_path,
)
from touka_local.rules import GIT_FOLDER, is_protected_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from touka_local.fsops import Fs

_SYSTEM_FOLDERS: Final = frozenset({"$recycle.bin", "system volume information"})
_ONEDRIVE_PREFIX: Final = "onedrive"
_DRIVE_FIXED: Final = "fixed"


@dataclass(frozen=True)
class Environment:
    """這台電腦上不能開放的位置。"""

    user_profile: str
    windows_dir: str
    program_files: tuple[str, ...]
    program_data: str
    app_data: str
    local_app_data: str
    helper_dir: str
    touka_folder: str


@dataclass(frozen=True)
class Grant:
    """一個開放的資料夾。代理人只看得到 ``name``。"""

    name: str
    path: str
    writable: bool


def resolve_mount_root(value: str, touka_folder: str) -> str | None:
    """把 Touka 回報的掛載來源換成主機路徑；相對路徑以 Touka 資料夾為準。"""
    text = value.strip()
    if not text:
        return None
    pure = PureWindowsPath(text)
    if pure.anchor:
        return ntpath.normpath(text) if is_plain_absolute(text) else None
    return ntpath.normpath(ntpath.join(touka_folder, text))


def _load(file: Path) -> list[Grant]:
    try:
        payload = json.loads(read_durable_text(file))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("grants"), list):
        return []
    grants: list[Grant] = []
    for entry in payload["grants"]:
        grant = _parse_entry(entry)
        if grant is None:
            return []
        grants.append(grant)
    return grants


def _parse_entry(entry: object) -> Grant | None:
    if not isinstance(entry, dict) or set(entry) != {"name", "path", "writable"}:
        return None
    name, path, writable = entry["name"], entry["path"], entry["writable"]
    if not (isinstance(name, str) and isinstance(path, str) and isinstance(writable, bool)):
        return None
    try:
        check_segment_syntax(name)
    except Refused:
        return None
    return Grant(name=name, path=path, writable=writable)


class GrantBook:
    """開放名單。檔案壞掉就當作什麼都沒開放。"""

    def __init__(
        self,
        file: Path,
        *,
        fs: Fs,
        env: Environment,
        drive_type: Callable[[str], str],
    ) -> None:
        self._file = file
        self._fs = fs
        self._env = env
        self._drive_type = drive_type
        self._lock = threading.RLock()
        self._mounts: tuple[str, ...] = ()
        self._grants = _load(file)

    def grants(self) -> list[Grant]:
        """名單上的全部，包含暫停的。"""
        with self._lock:
            return list(self._grants)

    def usable(self) -> list[Grant]:
        """現在可以用的。"""
        return [grant for grant in self.grants() if self._is_usable(grant)]

    def paused_names(self) -> list[str]:
        """名單上有、但現在不合規則的。"""
        return [grant.name for grant in self.grants() if not self._is_usable(grant)]

    def find(self, name: str) -> Grant | None:
        """用名稱找開放的資料夾，不分大小寫；不合規則的當作沒有。"""
        key = name.casefold()
        for grant in self.grants():
            if grant.name.casefold() == key:
                return grant if self._is_usable(grant) else None
        return None

    def add(self, path: str, *, writable: bool) -> Grant:
        """開放一個資料夾。"""
        normalized = normalize_folder(path)
        with self._lock:
            self._check_folder(normalized)
            self._check_overlap(normalized, self._grants)
            grant = Grant(
                name=self._unique_name(PureWindowsPath(normalized).name),
                path=normalized,
                writable=writable,
            )
            self._commit([*self._grants, grant])
            return grant

    def remove(self, name: str) -> None:
        """從名單拿掉。"""
        key = name.casefold()
        with self._lock:
            self._commit([g for g in self._grants if g.name.casefold() != key])

    def set_writable(self, name: str, *, writable: bool) -> None:
        """切換唯讀或可以改。"""
        key = name.casefold()
        with self._lock:
            self._commit(
                [
                    Grant(g.name, g.path, writable) if g.name.casefold() == key else g
                    for g in self._grants
                ]
            )

    def set_mount_roots(self, values: Iterable[str]) -> None:
        """Touka 回報的掛載來源，只用來多擋，不會讓任何資料夾變得能開放。"""
        resolved = (resolve_mount_root(v, self._env.touka_folder) for v in values)
        with self._lock:
            self._mounts = tuple(r for r in resolved if r is not None)

    def needs_onedrive_hint(self, grant: Grant) -> bool:
        """OneDrive 接管的資料夾，裡面的雲端佔位檔都會被擋。"""
        return any(p.casefold().startswith(_ONEDRIVE_PREFIX) for p in parts_of(grant.path)[1:])

    def _commit(self, grants: list[Grant]) -> None:
        """先存檔，存成功才算數；視窗上看到的永遠是檔案裡的內容。"""
        payload = {"grants": [asdict(grant) for grant in grants]}
        durable_write_text(self._file, json.dumps(payload, ensure_ascii=False, indent=2))
        self._grants = grants

    def _unique_name(self, base: str) -> str:
        taken = {grant.name.casefold() for grant in self._grants}
        if base.casefold() not in taken:
            return base
        number = 2
        while f"{base} {number}".casefold() in taken:
            number += 1
        return f"{base} {number}"

    def _is_usable(self, grant: Grant) -> bool:
        try:
            normalize_folder(grant.path)
            self._check_folder(grant.path)
            with self._lock:
                others = [g for g in self._grants if g.name != grant.name]
            self._check_overlap(grant.path, others)
        except (Refused, OSError):
            return False
        return True

    def _blocked_folders(self) -> list[str]:
        """這些資料夾本身、裡面和上層都不能開放；使用者資料夾本身是 AppData 的上層。"""
        env = self._env
        with self._lock:
            mounts = self._mounts
        return [
            env.windows_dir,
            *env.program_files,
            env.program_data,
            env.app_data,
            env.local_app_data,
            join(env.user_profile, "AppData"),
            join(env.user_profile, ".ssh"),
            join(env.user_profile, ".aws"),
            join(env.user_profile, ".gnupg"),
            env.helper_dir,
            env.touka_folder,
            *mounts,
        ]

    def _check_folder(self, path: str) -> None:
        for part in parts_of(path)[1:]:
            if is_protected_name(part):
                raise Refused("protected")
            if part.casefold() in _SYSTEM_FOLDERS:
                raise Refused("system_folder")
        if same_path(path, anchor_of(path)):
            raise Refused("blocked")
        if any(related(path, blocked) for blocked in self._blocked_folders()):
            raise Refused("blocked")
        if self._drive_type(anchor_of(path)) != _DRIVE_FIXED:
            raise Refused("drive_type")
        self._check_chain(path)

    def _check_chain(self, path: str) -> None:
        parts = parts_of(path)
        info = None
        for depth in range(1, len(parts) + 1):
            level = join(*parts[:depth])
            info = self._fs.lstat(level)
            if info is None:
                raise Refused("not_found")
            if info.is_reparse:
                raise Refused("link")
            if self._fs.lstat(join(level, GIT_FOLDER)) is not None:
                raise Refused("git_project")
        if info is None or not info.is_dir:
            raise Refused("not_a_folder")

    @staticmethod
    def _check_overlap(path: str, others: Iterable[Grant]) -> None:
        if any(related(path, other.path) for other in others):
            raise Refused("overlap")
