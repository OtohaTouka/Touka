"""Bounded metadata-only permissions for the four reviewed deployment mounts.

Importing this module performs no account, kernel, target or permission operation.
Targets must remain quiescent throughout the invocation; FD checks are not a lock
against a concurrent host writer. The caller retains the outer hard deadline.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

if sys.platform == "linux":
    import grp
    import pwd
else:
    grp = None
    pwd = None

PROFILE = "local-permissions-v1"
LAYOUT = ("data", "hf_cache", "proxy_config", "heartbeat_config")
# Agent workspaces stay as strict as the roots themselves; below them, rebuildable tool
# directories are left untouched. The names mirror SCAN_SKIP_NAMES (pinned by a test, ADR-0279).
WORKSPACE_ROOTS = (("data", "personal_drive"), ("data", "mcp_servers"))
TOOL_DIR_NAMES = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__", ".git", "dist", "build", ".tox"}
    | {".mypy_cache", ".next", "target", "vendor", ".cache", "coverage", "out", ".gradle"}
    | {".terraform"}
)
SKIP_KINDS = ("symlink", "fifo", "socket", "device", "hardlink", "setid_file", "xattr", "tool_dir")
_CLOSED_REASONS = frozenset(
    {
        "ACCOUNT_MISMATCH",
        "ACCOUNT_OR_CAPABILITY_MISMATCH",
        "CAPABILITY_METADATA",
        "CLEANUP_FAILED",
        "ENTRY_IDENTITY",
        "ENTRY_NAME",
        "FD_IDENTITY",
        "INODE_ALIAS",
        "KERNEL_METADATA",
        "LIMIT_CONFIGURATION",
        "LIMIT_EXCEEDED",
        "METADATA_DRIFT",
        "METADATA_POSTCONDITION",
        "MOUNT_ALIAS",
        "MOUNT_DRIFT",
        "MOUNT_METADATA",
        "NESTED_MOUNT",
        "ROOT_IDENTITY",
        "ROOT_LAYOUT",
        "ROOT_MOUNT",
        "UNSAFE_METADATA",
        "UNSUPPORTED_API",
        "UNSUPPORTED_METADATA",
        "UNSUPPORTED_PLATFORM",
    }
)


@dataclass(frozen=True, slots=True)
class Limits:
    """Closed production maxima; tests may only reduce them."""

    nodes: int = 100_000
    depth: int = 64
    manifest_bytes: int = 32 * 1024 * 1024
    mount_bytes: int = 1024 * 1024
    seconds: float = 60.0
    fds: int = 69


class StatView(Protocol):
    """Metadata fields consumed without reading target file content."""

    st_dev: int
    st_ino: int
    st_mode: int
    st_nlink: int
    st_uid: int
    st_gid: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int


class Metadata(Protocol):
    """Injectable permission syscall boundary with no content operations."""

    platform: str

    def account(self) -> tuple[int, int, int, int]:
        """Return effective UID, resolved appuser UID/GID and effective capabilities."""
        ...

    def monotonic(self) -> float:
        """Return the shared-budget clock."""
        ...

    def mount_points(self) -> dict[str, str]:
        """Project the four relative roles from the trusted helper location."""
        ...

    def mountinfo(self, limit: int) -> bytes:
        """Read only bounded kernel mount metadata."""
        ...

    def mount_id(self, fd: int) -> int:
        """Return the kernel mount ID of a held descriptor."""
        ...

    def device(self, value: int) -> tuple[int, int]:
        """Return the kernel major/minor pair for an observed device."""
        ...

    def open_root(self, role: str) -> int:
        """Hold one no-follow directory descriptor for a reviewed role."""
        ...

    def open_child(self, parent: int, name: str, directory: bool) -> int:
        """Open a relative no-follow descriptor without consuming file content."""
        ...

    def fstat(self, fd: int) -> StatView:
        """Read metadata of a held descriptor."""
        ...

    def lstat(self, parent: int, name: str) -> StatView:
        """Read relative metadata without following a symbolic link."""
        ...

    def names(self, fd: int) -> Iterable[str]:
        """Enumerate directory entry names using metadata only."""
        ...

    def xattrs(self, fd: int) -> list[str]:
        """List extended attribute names without reading their values."""
        ...

    def fchown(self, fd: int, uid: int, gid: int) -> None:
        """Change ownership through an already revalidated descriptor."""
        ...

    def fchmod(self, fd: int, mode: int) -> None:
        """Change mode through an already revalidated descriptor."""
        ...

    def close(self, fd: int) -> None:
        """Close exactly one owned descriptor without retry."""
        ...


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One immutable inode observation retained for later revalidation."""

    dev: int
    inode: int
    mode: int
    links: int
    uid: int
    gid: int
    size: int
    mtime: int
    ctime: int


class PermissionFailure(Exception):
    """Only fixed internal reason codes may reach the public result."""


T = TypeVar("T")


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise PermissionFailure(reason)


def _snapshot(value: StatView) -> Snapshot:
    fields = (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    _require(all(type(item) is int for item in fields), "UNSAFE_METADATA")
    _require(all(item >= 0 for item in fields[:7]), "UNSAFE_METADATA")
    result = Snapshot(*fields)
    _require(result.links >= 1, "UNSAFE_METADATA")
    return result


def _skip_kind(value: Snapshot) -> str | None:
    """Classify from no-follow metadata; skipped entries are never opened or changed."""
    mode = value.mode
    if stat.S_ISDIR(mode):
        # Linux keeps S_ISGID on directories across chown; the postcondition still checks it.
        return None
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    _require(stat.S_ISREG(mode), "UNSAFE_METADATA")
    if value.links != 1:
        return "hardlink"
    if mode & (stat.S_ISUID | stat.S_ISGID):
        return "setid_file"
    return None


def _strict(value: Snapshot) -> Snapshot:
    """Mount roots and agent workspace roots keep the original closed checks."""
    _require(stat.S_ISDIR(value.mode), "UNSAFE_METADATA")
    _require(not value.mode & (stat.S_ISUID | stat.S_ISGID), "UNSUPPORTED_METADATA")
    return value


def _target_mode(value: Snapshot) -> int:
    old = stat.S_IMODE(value.mode)
    extra = 0o700 if stat.S_ISDIR(value.mode) else 0o600 | (0o100 if old & 0o111 else 0)
    return old | extra


def _mount_path(value: str) -> PurePosixPath:
    escapes = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
    _require(re.search(r"\\(?!040|011|012|134)", value) is None, "MOUNT_METADATA")
    decoded = re.sub(r"\\(040|011|012|134)", lambda match: escapes[match.group(1)], value)
    path = PurePosixPath(decoded)
    _require(
        path.is_absolute() and str(path) == decoded and ".." not in path.parts, "MOUNT_METADATA"
    )
    return path


class NativeMetadata:
    """Linux syscalls; target descriptors are never used to read file contents."""

    platform = sys.platform

    def __init__(self) -> None:
        self.anchor = Path(__file__).resolve().parent

    def account(self) -> tuple[int, int, int, int]:
        """Return effective UID, resolved appuser UID/GID and effective capabilities."""
        _require(
            self.platform == "linux" and pwd is not None and grp is not None, "UNSUPPORTED_PLATFORM"
        )
        _require(
            all(
                hasattr(os, name)
                for name in (
                    "O_DIRECTORY",
                    "O_NOFOLLOW",
                    "O_CLOEXEC",
                    "O_NOATIME",
                    "O_NONBLOCK",
                    "O_PATH",
                    "fchown",
                    "fchmod",
                    "listxattr",
                )
            ),
            "UNSUPPORTED_API",
        )
        _require(os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd, "UNSUPPORTED_API")
        account = pwd.getpwnam("appuser")
        group = grp.getgrnam("appuser")
        _require(account.pw_gid == group.gr_gid, "ACCOUNT_MISMATCH")
        rows = [
            line.split(b":", 1)[1].strip()
            for line in self._kernel_bytes("status", 64 * 1024).splitlines()
            if line.startswith(b"CapEff:")
        ]
        _require(
            len(rows) == 1 and re.fullmatch(rb"[0-9a-fA-F]{1,16}", rows[0]) is not None,
            "CAPABILITY_METADATA",
        )
        return os.geteuid(), account.pw_uid, group.gr_gid, int(rows[0], 16)

    def _kernel_bytes(self, name: str, limit: int, fd: int | None = None) -> bytes:
        _require(name in {"status", "mountinfo", "fdinfo"}, "KERNEL_METADATA")
        _require(type(limit) is int and 0 < limit <= 1024 * 1024, "KERNEL_METADATA")
        _require(
            (name == "fdinfo" and type(fd) is int and fd >= 0) or (name != "fdinfo" and fd is None),
            "KERNEL_METADATA",
        )
        # The operating-system root and current PID locate the standard proc ABI;
        # application mount targets always come from the trusted image anchor.
        path = Path(os.sep) / "proc" / str(os.getpid()) / name
        if fd is not None:
            path /= str(fd)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            result = bytearray()
            while True:
                chunk = os.read(descriptor, limit + 1 - len(result))
                if not chunk:
                    return bytes(result)
                result.extend(chunk)
                _require(len(result) <= limit, "LIMIT_EXCEEDED")
        finally:
            os.close(descriptor)

    def monotonic(self) -> float:
        """Return the shared-budget clock."""
        return time.monotonic()

    def mount_points(self) -> dict[str, str]:
        """Project the four relative roles from the trusted helper location."""
        return {role: str(self.anchor / role) for role in LAYOUT}

    def mountinfo(self, limit: int) -> bytes:
        """Read only bounded kernel mount metadata."""
        return self._kernel_bytes("mountinfo", limit)

    def mount_id(self, fd: int) -> int:
        """Return the kernel mount ID of a held descriptor."""
        rows = [
            line.split(b":", 1)[1].strip()
            for line in self._kernel_bytes("fdinfo", 64 * 1024, fd).splitlines()
            if line.startswith(b"mnt_id:")
        ]
        _require(
            len(rows) == 1 and re.fullmatch(rb"[0-9]{1,20}", rows[0]) is not None,
            "MOUNT_METADATA",
        )
        number = int(rows[0])
        _require(number > 0, "MOUNT_METADATA")
        return number

    def device(self, value: int) -> tuple[int, int]:
        """Return the kernel major/minor pair for an observed device."""
        return os.major(value), os.minor(value)

    def open_root(self, role: str) -> int:
        """Hold one no-follow directory descriptor for a reviewed role."""
        _require(role in LAYOUT, "ROOT_LAYOUT")
        return os.open(
            self.anchor / role,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOATIME,
        )

    def open_child(self, parent: int, name: str, directory: bool) -> int:
        """Open a relative no-follow descriptor without consuming file content."""
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOATIME | os.O_NONBLOCK
        if directory:
            return os.open(name, flags | os.O_DIRECTORY, dir_fd=parent)
        probe: int | None = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        readable: int | None = None
        keep = False
        try:
            before = os.fstat(probe)
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "UNSAFE_METADATA")
            # Follow only our held kernel FD reference, never a target-tree link.
            path = Path(os.sep) / "proc" / str(os.getpid()) / "fd" / str(probe)
            readable = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOATIME | os.O_NONBLOCK)
            after = os.fstat(readable)
            _require(
                (after.st_dev, after.st_ino, after.st_mode, after.st_nlink)
                == (before.st_dev, before.st_ino, before.st_mode, before.st_nlink),
                "FD_IDENTITY",
            )
            owned = probe
            probe = None  # A close with uncertain effects must never be retried.
            os.close(owned)
            keep = True
            return readable
        finally:
            try:
                if probe is not None:
                    os.close(probe)
            finally:
                if not keep and readable is not None:
                    os.close(readable)

    def fstat(self, fd: int) -> StatView:
        """Read metadata of a held descriptor."""
        return os.fstat(fd)

    def lstat(self, parent: int, name: str) -> StatView:
        """Read relative metadata without following a symbolic link."""
        return os.stat(name, dir_fd=parent, follow_symlinks=False)

    def names(self, fd: int) -> Iterable[str]:
        """Enumerate directory entry names using metadata only."""
        with os.scandir(fd) as entries:
            for entry in entries:
                yield entry.name

    def xattrs(self, fd: int) -> list[str]:
        """List extended attribute names without reading their values."""
        return os.listxattr(fd)

    def fchown(self, fd: int, uid: int, gid: int) -> None:
        """Change ownership through an already revalidated descriptor."""
        os.fchown(fd, uid, gid)

    def fchmod(self, fd: int, mode: int) -> None:
        """Change mode through an already revalidated descriptor."""
        os.fchmod(fd, mode)

    def close(self, fd: int) -> None:
        """Close exactly one owned descriptor without retry."""
        os.close(fd)


class _Engine:
    def __init__(self, backend: Metadata, limits: Limits) -> None:
        self.backend = backend
        self.limits = limits
        self.started = backend.monotonic()
        self.stage = "preflight"
        self.handles: set[int] = set()
        self.roots: dict[str, int] = {}
        self.inventory: dict[tuple[str, ...], Snapshot] = {}
        self.skipped: dict[tuple[str, ...], tuple[str, Snapshot]] = {}
        self.points: dict[str, str] = {}
        self.mount_raw = b""
        self.chown_count = 0
        self.chmod_count = 0
        self.mutation_attempted = False
        self.cleanup_failed = False

    def budget(self) -> None:
        elapsed = self.backend.monotonic() - self.started
        _require(0 <= elapsed < self.limits.seconds, "LIMIT_EXCEEDED")

    def call(self, function: Callable[..., T], *args: object) -> T:
        self.budget()
        result = function(*args)
        self.budget()
        return result

    def spare_fd(self) -> None:
        _require(len(self.handles) < self.limits.fds, "LIMIT_EXCEEDED")

    def opened(self, function: Callable[..., int], *args: object, reserve: int = 1) -> int:
        self.budget()
        _require(len(self.handles) + reserve <= self.limits.fds, "LIMIT_EXCEEDED")
        fd = function(*args)
        _require(type(fd) is int and fd >= 0 and fd not in self.handles, "FD_IDENTITY")
        self.handles.add(fd)
        self.budget()
        return fd

    def close(self, fd: int) -> None:
        self.handles.remove(fd)
        try:
            self.backend.close(fd)
        except Exception:
            self.cleanup_failed = True
            raise PermissionFailure("CLEANUP_FAILED") from None

    def close_all(self) -> None:
        for fd in tuple(self.handles):
            try:
                self.close(fd)
            except PermissionFailure:
                self.cleanup_failed = True

    def observe(self, fd: int) -> tuple[Snapshot, bool]:
        value = _snapshot(self.call(self.backend.fstat, fd))
        return value, self.call(self.backend.xattrs, fd) != []

    def inspect(self, fd: int) -> Snapshot:
        value, labelled = self.observe(fd)
        # Unknown xattr semantics cannot be certified as preserved by chown.
        _require(not labelled, "UNSUPPORTED_METADATA")
        return value

    def mount_snapshot(self) -> bytes:
        self.spare_fd()
        raw = self.call(self.backend.mountinfo, self.limits.mount_bytes)
        _require(type(raw) is bytes and 0 < len(raw) <= self.limits.mount_bytes, "MOUNT_METADATA")
        rows = []
        identifiers: set[int] = set()
        for line in raw.decode("utf-8").splitlines():
            self.budget()
            fields = line.split()
            _require(fields.count("-") == 1 and len(fields) >= 10, "MOUNT_METADATA")
            separator = fields.index("-")
            _require(separator >= 6 and len(fields) == separator + 4, "MOUNT_METADATA")
            _require(fields[0].isdigit() and fields[1].isdigit(), "MOUNT_METADATA")
            number = int(fields[0])
            _require(number not in identifiers, "MOUNT_METADATA")
            identifiers.add(number)
            device = fields[2].split(":")
            _require(
                len(device) == 2 and all(value.isdigit() for value in device), "MOUNT_METADATA"
            )
            flags = fields[5].split(",")
            _require(("rw" in flags) != ("ro" in flags), "MOUNT_METADATA")
            rows.append(
                (
                    number,
                    tuple(map(int, device)),
                    _mount_path(fields[3]),
                    _mount_path(fields[4]),
                    "rw" in flags,
                )
            )
        selected = {}
        for role, point in self.points.items():
            matches = [row for row in rows if str(row[3]) == point]
            _require(len(matches) == 1 and matches[0][4], "ROOT_MOUNT")
            row = matches[0]
            _require(
                row[0] == self.call(self.backend.mount_id, self.roots[role])
                and row[1] == self.call(self.backend.device, self.inspect(self.roots[role]).dev),
                "ROOT_MOUNT",
            )
            selected[role] = row
        for row in selected.values():
            for other in rows:
                if other[0] == row[0]:
                    continue
                _require(not other[3].is_relative_to(row[3]), "NESTED_MOUNT")
                same_backing = other[1] == row[1] and (
                    other[2].is_relative_to(row[2]) or row[2].is_relative_to(other[2])
                )
                _require(not (same_backing and other[4]), "MOUNT_ALIAS")
        return raw

    def names(self, fd: int, available: int, manifest_bytes: int) -> list[str]:
        self.spare_fd()  # scandir may hold one additional descriptor.
        iterator = iter(self.call(self.backend.names, fd))
        result = []
        used = 0
        try:
            while True:
                self.budget()
                try:
                    name = next(iterator)
                except StopIteration:
                    break
                self.budget()
                _require(
                    type(name) is str
                    and name not in {"", ".", ".."}
                    and not any(value in name for value in ("/", "\\", "\x00")),
                    "ENTRY_NAME",
                )
                used += len(name.encode("utf-8")) + 128
                _require(
                    len(result) < available and used + manifest_bytes <= self.limits.manifest_bytes,
                    "LIMIT_EXCEEDED",
                )
                result.append(name)
            _require(len(result) == len(set(result)), "ENTRY_IDENTITY")
            return result
        finally:
            close_iterator = getattr(iterator, "close", None)
            if close_iterator is not None:
                self.call(close_iterator)

    def inventory_scan(
        self,
    ) -> tuple[dict[tuple[str, ...], Snapshot], dict[tuple[str, ...], tuple[str, Snapshot]]]:
        result: dict[tuple[str, ...], Snapshot] = {}
        skipped: dict[tuple[str, ...], tuple[str, Snapshot]] = {}
        identities: set[tuple[int, int]] = set()
        manifest_bytes = 0

        def count(key: tuple[str, ...]) -> None:
            nonlocal manifest_bytes
            self.budget()
            _require(
                len(key) - 1 <= self.limits.depth
                and len(result) + len(skipped) < self.limits.nodes,
                "LIMIT_EXCEEDED",
            )
            manifest_bytes += len("/".join(key).encode("utf-8")) + 128
            _require(manifest_bytes <= self.limits.manifest_bytes, "LIMIT_EXCEEDED")

        def visit(fd: int, key: tuple[str, ...], value: Snapshot) -> None:
            _require((value.dev, value.inode) not in identities, "INODE_ALIAS")
            identities.add((value.dev, value.inode))
            result[key] = value
            if not stat.S_ISDIR(value.mode):
                return
            available = self.limits.nodes - len(result) - len(skipped)
            for name in self.names(fd, available, manifest_bytes):
                child_key = (*key, name)
                count(child_key)
                expected = _snapshot(self.call(self.backend.lstat, fd, name))
                strict = child_key in WORKSPACE_ROOTS
                if strict:
                    _strict(expected)
                    kind = None
                else:
                    kind = _skip_kind(expected)
                    if (
                        kind is None
                        and stat.S_ISDIR(expected.mode)
                        and child_key[:2] in WORKSPACE_ROOTS
                        and name in TOOL_DIR_NAMES
                    ):
                        kind = "tool_dir"
                if kind is not None:
                    skipped[child_key] = (kind, expected)
                    continue
                directory = stat.S_ISDIR(expected.mode)
                child = self.opened(
                    self.backend.open_child, fd, name, directory, reserve=1 if directory else 2
                )
                try:
                    observed, labelled = self.observe(child)
                    _require(observed == expected, "METADATA_DRIFT")
                    if labelled:
                        # Opened only to list xattr names; such entries are never changed.
                        _require(not strict, "UNSUPPORTED_METADATA")
                        skipped[child_key] = ("xattr", observed)
                    else:
                        visit(child, child_key, observed)
                finally:
                    self.close(child)
            _require(self.inspect(fd) == value, "METADATA_DRIFT")

        for role in LAYOUT:
            count((role,))
            visit(self.roots[role], (role,), _strict(self.inspect(self.roots[role])))
        return result, skipped

    def apply_one(self, key: tuple[str, ...]) -> None:
        fd = self.roots[key[0]]
        opened = []
        try:
            _require(self.inspect(fd) == self.inventory[(key[0],)], "METADATA_DRIFT")
            for index, name in enumerate(key[1:], 1):
                expected = self.inventory[key[: index + 1]]
                _require(
                    _snapshot(self.call(self.backend.lstat, fd, name)) == expected, "METADATA_DRIFT"
                )
                directory = stat.S_ISDIR(expected.mode)
                fd = self.opened(
                    self.backend.open_child, fd, name, directory, reserve=1 if directory else 2
                )
                opened.append(fd)
                _require(self.inspect(fd) == expected, "METADATA_DRIFT")
            original = self.inventory[key]
            current = self.inspect(fd)
            _require(current == original, "METADATA_DRIFT")
            target_mode = _target_mode(original)
            if (current.uid, current.gid) != (1001, 1001):
                self.budget()
                self.mutation_attempted = True
                self.call(self.backend.fchown, fd, 1001, 1001)
                self.chown_count += 1
                changed = self.inspect(fd)
                expected = Snapshot(
                    current.dev,
                    current.inode,
                    current.mode,
                    current.links,
                    1001,
                    1001,
                    current.size,
                    current.mtime,
                    changed.ctime,
                )
                _require(changed == expected, "METADATA_POSTCONDITION")
                current = changed
            if stat.S_IMODE(current.mode) != target_mode:
                _require(self.inspect(fd) == current, "METADATA_DRIFT")
                self.mutation_attempted = True
                self.call(self.backend.fchmod, fd, target_mode)
                self.chmod_count += 1
                changed = self.inspect(fd)
                expected = Snapshot(
                    current.dev,
                    current.inode,
                    stat.S_IFMT(current.mode) | target_mode,
                    current.links,
                    current.uid,
                    current.gid,
                    current.size,
                    current.mtime,
                    changed.ctime,
                )
                _require(changed == expected, "METADATA_POSTCONDITION")
                current = changed
            _require(self.inspect(fd) == current, "METADATA_DRIFT")
            self.inventory[key] = current
        finally:
            for descriptor in reversed(opened):
                if descriptor in self.handles:
                    self.close(descriptor)

    def run(self) -> None:
        maxima = Limits()
        for name in ("nodes", "depth", "manifest_bytes", "mount_bytes", "fds"):
            value = getattr(self.limits, name)
            _require(
                type(value) is int and 0 <= value <= getattr(maxima, name), "LIMIT_CONFIGURATION"
            )
        _require(
            type(self.limits.seconds) in {float, int}
            and 0 <= self.limits.seconds <= maxima.seconds,
            "LIMIT_CONFIGURATION",
        )
        _require(self.backend.platform == "linux", "UNSUPPORTED_PLATFORM")
        credentials = self.call(self.backend.account)
        _require(
            type(credentials) is tuple
            and len(credentials) == 4
            and all(type(value) is int for value in credentials),
            "ACCOUNT_MISMATCH",
        )
        _require(credentials == (0, 1001, 1001, 11), "ACCOUNT_OR_CAPABILITY_MISMATCH")
        self.points = self.call(self.backend.mount_points)
        _require(type(self.points) is dict and set(self.points) == set(LAYOUT), "ROOT_LAYOUT")
        paths = [_mount_path(self.points[role]) for role in LAYOUT]
        _require(
            all(
                not left.is_relative_to(right)
                for index, left in enumerate(paths)
                for other, right in enumerate(paths)
                if index != other
            ),
            "ROOT_LAYOUT",
        )
        root_ids = set()
        for role in LAYOUT:
            fd = self.opened(self.backend.open_root, role)
            self.roots[role] = fd
            value = _strict(self.inspect(fd))
            _require(
                stat.S_ISDIR(value.mode) and (value.dev, value.inode) not in root_ids,
                "ROOT_IDENTITY",
            )
            root_ids.add((value.dev, value.inode))
        self.mount_raw = self.mount_snapshot()
        self.inventory, self.skipped = self.inventory_scan()
        self.stage = "apply"
        _require(self.mount_snapshot() == self.mount_raw, "MOUNT_DRIFT")
        for role in LAYOUT:
            _require(self.inspect(self.roots[role]) == self.inventory[(role,)], "METADATA_DRIFT")
        for key in tuple(self.inventory):
            self.apply_one(key)
        self.stage = "verify"
        _require(self.mount_snapshot() == self.mount_raw, "MOUNT_DRIFT")
        _require(self.inventory_scan() == (self.inventory, self.skipped), "METADATA_DRIFT")
        self.budget()


def run_permission_init(backend: Metadata, *, limits: Limits | None = None) -> dict[str, object]:
    """Preflight, minimally modify and verify four quiescent mount trees."""
    engine: _Engine | None = None
    reason = "PRECONDITION_FAILED"
    success = False
    try:
        engine = _Engine(backend, limits if limits is not None else Limits())
        engine.run()
        success = True
    except PermissionFailure as error:
        value = error.args[0] if len(error.args) == 1 else None
        reason = (
            value
            if type(value) is str and value in _CLOSED_REASONS
            else "METADATA_OPERATION_FAILED"
        )
    except Exception:
        reason = "METADATA_OPERATION_FAILED"
    finally:
        if engine is not None:
            engine.close_all()
            if engine.cleanup_failed:
                success = False
                reason = "CLEANUP_FAILED"
    partial = engine is not None and engine.mutation_attempted and not success
    if partial and reason != "CLEANUP_FAILED":
        reason = "PARTIAL_PERMISSION_FAILURE"
    kinds = [kind for kind, _ in engine.skipped.values()] if engine is not None else []
    return {
        "profile": PROFILE,
        "status": "PASS" if success else "FAIL",
        "stage": "complete" if success else engine.stage if engine is not None else "preflight",
        "reason": "COMPLETE" if success else reason,
        "roles": list(LAYOUT),
        "nodes": len(engine.inventory) if engine is not None else 0,
        "chown_completed": engine.chown_count if engine is not None else 0,
        "chmod_completed": engine.chmod_count if engine is not None else 0,
        "skipped": {kind: kinds.count(kind) for kind in SKIP_KINDS},
        "mutation_uncertain": partial,
        "cleanup": "FAIL" if engine is not None and engine.cleanup_failed else "PASS",
    }


def _emit(payload: str) -> None:
    sys.stdout.write(payload + "\n")


def main(argv: list[str] | None = None) -> int:
    """Run the closed CLI and emit only reviewed result fields."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments != ["--profile", PROFILE, "--expected-uid", "1001", "--expected-gid", "1001"]:
        _emit(
            json.dumps(
                {
                    "profile": PROFILE,
                    "status": "FAIL",
                    "stage": "preflight",
                    "reason": "ARGUMENT_CONTRACT",
                }
            )
        )
        return 1
    try:
        backend = NativeMetadata()
    except Exception:
        _emit(
            json.dumps(
                {
                    "profile": PROFILE,
                    "status": "FAIL",
                    "stage": "preflight",
                    "reason": "CAPABILITY_GAP",
                }
            )
        )
        return 1
    result = run_permission_init(backend)
    _emit(json.dumps(result))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
