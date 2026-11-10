"""本機小幫手的測試工具：在記憶體裡模擬 Windows 檔案系統（ADR-0311）。

CI 只有 Linux，所以規則都用這個假的檔案系統測，真 Windows 的部分另外用 skipif。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath

import pytest
from touka_local.fsops import EntryInfo


@dataclass
class _Node:
    is_dir: bool
    data: bytes = b""
    mtime_ns: int = 1
    reparse: bool = False
    hidden: bool = False


def _key(path: str) -> str:
    return str(PureWindowsPath(path)).casefold()


class FakeFs:
    """Windows 語意：不分大小寫、rename 到已存在的目的地會失敗。"""

    def __init__(self) -> None:
        self.nodes: dict[str, _Node] = {}
        self.names: dict[str, str] = {}
        self.renames: list[tuple[str, str]] = []
        self.fail_rename_for: set[str] = set()
        self.tick = 100

    # 測試佈置
    def add_dir(self, path: str, *, reparse: bool = False, hidden: bool = False) -> None:
        pure = PureWindowsPath(path)
        for parent in reversed(pure.parents):
            if _key(str(parent)) not in self.nodes:
                self.nodes[_key(str(parent))] = _Node(is_dir=True)
                self.names[_key(str(parent))] = str(parent)
        self.nodes[_key(path)] = _Node(is_dir=True, reparse=reparse, hidden=hidden)
        self.names[_key(path)] = str(pure)

    def add_file(
        self,
        path: str,
        data: bytes = b"x",
        *,
        reparse: bool = False,
        hidden: bool = False,
        mtime_ns: int = 1,
    ) -> None:
        parent = str(PureWindowsPath(path).parent)
        if _key(parent) not in self.nodes:
            self.add_dir(parent)
        self.nodes[_key(path)] = _Node(
            is_dir=False, data=data, reparse=reparse, hidden=hidden, mtime_ns=mtime_ns
        )
        self.names[_key(path)] = str(PureWindowsPath(path))

    def touch(self, path: str) -> None:
        self.nodes[_key(path)].mtime_ns += 1

    def exists(self, path: str) -> bool:
        return _key(path) in self.nodes

    def data(self, path: str) -> bytes:
        return self.nodes[_key(path)].data

    # Fs 合約
    def lstat(self, path: str) -> EntryInfo | None:
        node = self.nodes.get(_key(path))
        if node is None:
            return None
        return EntryInfo(
            is_dir=node.is_dir,
            size=len(node.data),
            mtime_ns=node.mtime_ns,
            is_reparse=node.reparse,
            is_hidden=node.hidden,
        )

    def children(self, path: str) -> list[str]:
        prefix = _key(path).rstrip("\\") + "\\"
        return sorted(
            PureWindowsPath(self.names[key]).name
            for key in self.nodes
            if key.startswith(prefix) and "\\" not in key[len(prefix) :]
        )

    def mkdir(self, path: str) -> None:
        if _key(path) in self.nodes:
            raise FileExistsError(path)
        self.add_dir(path)

    def create_new(self, path: str, data: bytes) -> None:
        if _key(path) in self.nodes:
            raise FileExistsError(path)
        self.add_file(path, data, mtime_ns=self.tick)

    def read(self, path: str, limit: int) -> bytes:
        return self.nodes[_key(path)].data[:limit]

    def rename(self, source: str, destination: str) -> None:
        if _key(source) in self.fail_rename_for:
            raise PermissionError(source)
        if _key(destination) in self.nodes:
            raise FileExistsError(destination)
        prefix = _key(source).rstrip("\\") + "\\"
        moved = [key for key in self.nodes if key == _key(source) or key.startswith(prefix)]
        for key in moved:
            suffix = self.names[key][len(source) :]
            new_name = destination + suffix
            self.nodes[_key(new_name)] = self.nodes.pop(key)
            self.names[_key(new_name)] = new_name
            del self.names[key]
        self.renames.append((source, destination))

    def remove_tree_for_test(self, path: str) -> None:
        prefix = _key(path).rstrip("\\") + "\\"
        for key in [k for k in self.nodes if k == _key(path) or k.startswith(prefix)]:
            del self.nodes[key]
            del self.names[key]


class FakeTrash:
    """回收桶的假實作：成功、失敗、被中止。"""

    def __init__(self, fs: FakeFs) -> None:
        self.fs = fs
        self.sent: list[str] = []
        self.outcomes: dict[str, str] = {}

    def send(self, path: str) -> str:
        outcome = self.outcomes.get(path, "ok")
        if outcome == "ok":
            self.sent.append(path)
            self.fs.remove_tree_for_test(path)
        return outcome


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fs() -> FakeFs:
    return FakeFs()


@pytest.fixture
def trash(fs: FakeFs) -> FakeTrash:
    return FakeTrash(fs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
