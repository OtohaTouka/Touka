"""往下掃描 Touka 目錄（iso-c2 U1g）：範圍、上限、不跟連結、快取與背景續掃.

``scan_below`` 從專案根往下最多 3 層，找到 Touka 目錄（標記或資料根形狀）就回 ``touka``；
超過資料夾數或時間上限回 ``limit``；讀不到回 ``error``（兩者呼叫端都當 ``scan_limit``）。
時間上限用注入的時鐘驗證：每看一個資料夾時鐘就往前走，等於一個很慢的檔案系統。

``ProjectScanner`` 的前景最多等一小段時間；等不到就這一次回 ``scan_limit``，背景照樣掃完並
寫進快取（依專案根，TTL 與根目錄 mtime）；同一個根同時只掃一次。這裡用可以卡住的假掃描
（``_GatedScan``）控制背景掃描什麼時候結束。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import threading
from typing import TYPE_CHECKING

import pytest
from src.services.isolation.profiles import SandboxLayout
from src.services.isolation.project_scan import (
    SCAN_CACHE_TTL_SECONDS,
    ProjectScanner,
    ScanBudget,
)
from src.utils import touka_dirs
from src.utils.touka_dirs import scan_below

if TYPE_CHECKING:
    from pathlib import Path

_winapi = importlib.import_module("_winapi") if os.name == "nt" else None


def _scan(root: Path, **overrides: object) -> str:
    budget: dict = {"max_depth": 3, "max_dirs": 1000, "seconds": 30.0}
    budget.update(overrides)
    return scan_below(root, **budget)


def _tree(root: Path, *relatives: str) -> Path:
    for relative in relatives:
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


def test_clean_project_is_clean(tmp_path):
    root = _tree(tmp_path / "proj", "src/pkg/mod", "tests", "docs/a/b/c/d")
    (root / ".env").write_text("OWN=1\n", encoding="utf-8")
    assert _scan(root) == "clean"


@pytest.mark.parametrize(
    "marker",
    ["ToukaProxy", "TOUKAMEMORY", "toukaheartbeat", "ToukaDispatch", "heartbeat-config"],
)
def test_marker_directories_are_found_with_casefold(tmp_path, marker):
    root = _tree(tmp_path / "proj", f"a/b/clone/{marker}")
    assert _scan(root) == "touka"


def test_ghcr_compose_file_is_a_marker(tmp_path):
    root = _tree(tmp_path / "proj", "deploy")
    (root / "deploy" / "Docker-Compose.GHCR.yml").write_text("", encoding="utf-8")
    assert _scan(root) == "touka"


@pytest.mark.parametrize("name", ["soul", "SOUL", "Personal_Drive", "ui_sessions.db"])
def test_any_data_root_name_marks_a_data_root(tmp_path, name):
    """資料根形狀不靠用到才建的 legacy：soul、personal_drive 或啟動就建的 ui_sessions.db 任一個。"""
    root = _tree(tmp_path / "proj", "x")
    (root / "x" / name).mkdir() if "." not in name else (root / "x" / name).write_bytes(b"")
    assert _scan(root) == "touka"


def test_legacy_alone_is_not_a_data_root(tmp_path):
    """legacy 是很常見的資料夾名稱；資料根一定還有其他名稱，單獨出現不算。"""
    assert _scan(_tree(tmp_path / "proj", "x/legacy", "y/Legacy/old")) == "clean"


def test_project_root_itself_is_checked(tmp_path):
    root = _tree(tmp_path / "proj", "ToukaProxy")
    assert _scan(root) == "touka"


def test_depth_three_is_the_last_level_looked_into(tmp_path):
    found = _tree(tmp_path / "found", "a/b/c/ToukaProxy")
    beyond = _tree(tmp_path / "beyond", "a/b/c/d/ToukaProxy")
    assert _scan(found) == "touka"
    assert _scan(beyond) == "clean"  # 已知限制：只到 3 層


def test_more_directories_than_the_budget_is_limit(tmp_path):
    root = _tree(tmp_path / "big", *(f"d{i}/sub" for i in range(10)))
    assert _scan(root, max_dirs=21) == "clean"  # 根＋10＋10
    assert _scan(root, max_dirs=20) == "limit"


def test_slow_filesystem_hits_the_time_limit(tmp_path):
    """每看一個資料夾就過 1 秒：時間上限 5 秒，6 個資料夾以上就停。"""
    root = _tree(tmp_path / "slow", *(f"d{i}" for i in range(10)))
    ticks = iter(range(10_000))

    def _slow_clock() -> float:
        return float(next(ticks))

    assert scan_below(root, max_depth=3, max_dirs=1000, seconds=5.0, clock=_slow_clock) == "limit"
    fast = scan_below(root, max_depth=3, max_dirs=1000, seconds=30.0, clock=_slow_clock)
    assert fast == "clean"


def test_skipped_directories_are_neither_counted_nor_entered(tmp_path):
    names = sorted(touka_dirs.SCAN_SKIP_NAMES)
    root = _tree(tmp_path / "proj", *(f"{name}/x/y" for name in names))
    assert _scan(root, max_dirs=1) == "clean"


def _dir_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if _winapi is None:
            pytest.skip("cannot create directory links here")
        _winapi.CreateJunction(str(target), str(link))


def test_links_are_not_followed(tmp_path):
    touka = _tree(tmp_path / "touka", "ToukaProxy")
    root = _tree(tmp_path / "proj", "src")
    _dir_link(root / "src" / "link", touka)
    _dir_link(root / "loop", root)
    assert _scan(root, max_dirs=3) == "clean"


def test_scan_only_enters_the_named_top_folders(tmp_path):
    """補掃只進指定的第一層資料夾；根目錄本身的內容照樣檢查。"""
    root = _tree(tmp_path / "proj", "old/ToukaProxy", "new/a")
    assert _scan(root, only=frozenset({"new"})) == "clean"
    assert _scan(root, only=frozenset({"old"})) == "touka"
    (root / "docker-compose.ghcr.yml").write_text("", encoding="utf-8")
    assert _scan(root, only=frozenset()) == "touka"


def test_missing_root_is_an_error(tmp_path):
    """讀不到的資料夾（POSIX 權限的案例在 Linux 模組）一律是 error，不是 clean。"""
    assert _scan(tmp_path / "gone") == "error"


# --- ProjectScanner：快取、single-flight、背景續掃 ---------------------------------------

PROTECTED = "protected_location"
SCAN_LIMIT = "scan_limit"


class _GatedScan:
    """假掃描：在閘門打開前一直卡住（像很慢的檔案系統），記錄被呼叫幾次。"""

    def __init__(self, outcome: str = "clean", *, open_gate: bool = True) -> None:
        self.outcome = outcome
        self.calls = 0
        self.gate = threading.Event()
        if open_gate:
            self.gate.set()

    def __call__(self, _root: Path, *, max_depth: int, max_dirs: int, seconds: float) -> str:
        assert (max_depth, max_dirs, seconds) == (3, 50_000, 30.0)  # 背景用大預算
        self.calls += 1
        self.gate.wait(10)
        return self.outcome


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _workspace_layout(tmp_path: Path) -> SandboxLayout:
    """工作區（鏡像根）是 Touka checkout，專案是使用者自建的頂層資料夾。"""
    workspace = tmp_path / "workspace"
    (workspace / "ToukaProxy").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    return SandboxLayout(
        data_root=tmp_path / "data",
        config_dir=tmp_path / "config",
        install_root=tmp_path / "install",
        proxy_root=tmp_path / "install" / "ToukaProxy",
        env_file=tmp_path / "host_root" / ".env",
        state_root=tmp_path / "st",
        kernel_dir=None,
        executor_dir=None,
        mirror_roots=(workspace.resolve(),),
        in_container=False,
    )


def _project(layout: SandboxLayout, name: str = "proj") -> Path:
    root = layout.mirror_roots[0] / name
    (root / "src").mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _bump_mtime(path: Path) -> None:
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))


def _scanner(scan: _GatedScan, clock: _Clock | None = None, wait: float = 5.0) -> ProjectScanner:
    return ProjectScanner(scan=scan, clock=clock or _Clock(), foreground_wait=wait)


async def _until(condition, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition not reached")


async def test_result_is_cached_per_root_until_the_ttl(tmp_path):
    layout = _workspace_layout(tmp_path)
    project = _project(layout)
    scan, clock = _GatedScan(), _Clock()
    scanner = _scanner(scan, clock)
    assert await scanner.verdict(project, layout) is None
    clock.now += SCAN_CACHE_TTL_SECONDS - 1
    assert await scanner.verdict(project, layout) is None
    assert scan.calls == 1
    clock.now += 2
    assert await scanner.verdict(project, layout) is None
    assert scan.calls == 2


async def test_root_mtime_change_invalidates_the_cache(tmp_path):
    """真的掃描：快取之後在第 1 層放進另一份 Touka checkout，根目錄 mtime 變了就重掃。"""
    layout = _workspace_layout(tmp_path)
    project = _project(layout)
    scanner = ProjectScanner()
    assert await scanner.verdict(project, layout) is None
    (project / "clone" / "ToukaProxy").mkdir(parents=True)
    _bump_mtime(project)
    assert await scanner.verdict(project, layout) == PROTECTED


@pytest.mark.parametrize("change", ["new_file", "removed_folder", "replaced_file"])
async def test_root_change_without_new_top_folders_keeps_the_cached_verdict(tmp_path, change):
    """根目錄 mtime 變了但第一層沒有新增（或換掉）資料夾：沿用舊判定，不重掃（9p 上大專案）。"""
    layout = _workspace_layout(tmp_path)
    project = _project(layout)
    (project / "old").mkdir()
    (project / "a.txt").write_text("", encoding="utf-8")
    scan = _GatedScan()
    scanner = _scanner(scan)
    assert await scanner.verdict(project, layout) is None
    if change == "new_file":
        (project / ".coverage").write_text("", encoding="utf-8")
    elif change == "removed_folder":
        (project / "old").rmdir()
    else:
        (project / "a.tmp").write_text("x", encoding="utf-8")
        os.replace(project / "a.tmp", project / "a.txt")  # 寫入動詞的原子置換
    _bump_mtime(project)
    assert await scanner.verdict(project, layout) is None
    assert scan.calls == 1


class _RecordingScan:
    """真的掃描，記下每次只掃哪些第一層資料夾（None＝整個專案根）。"""

    def __init__(self) -> None:
        self.only: list[frozenset[str] | None] = []

    def __call__(self, root: Path, *, max_depth: int, max_dirs: int, seconds: float, **kw) -> str:
        self.only.append(kw.get("only"))
        return scan_below(root, max_depth=max_depth, max_dirs=max_dirs, seconds=seconds, **kw)


async def test_new_top_folders_are_scanned_alone_and_a_touka_one_is_caught(tmp_path):
    """第一層有新增的資料夾：只掃新增的（3 層、略過清單照舊）；新放進的 Touka 形狀照樣抓到。"""
    layout = _workspace_layout(tmp_path)
    project = _project(layout)
    scan, clock = _RecordingScan(), _Clock()
    scanner = ProjectScanner(scan=scan, clock=clock)
    assert await scanner.verdict(project, layout) is None
    (project / "docs" / "a" / "b").mkdir(parents=True)
    _bump_mtime(project)
    clock.now += SCAN_CACHE_TTL_SECONDS - 10
    assert await scanner.verdict(project, layout) is None
    (project / "vendor" / "x" / "ToukaProxy").mkdir(parents=True)  # 略過清單照舊不進去
    (project / "clone" / "a" / "b" / "ToukaMemory").mkdir(parents=True)  # 第 3 層
    _bump_mtime(project)
    _bump_mtime(project)  # 和上一次的 mtime 一定不同
    assert await scanner.verdict(project, layout) == PROTECTED
    assert scan.only == [None, frozenset({"docs"}), frozenset({"vendor", "clone"})]
    clock.now += 20  # 只掃新增的不延長 TTL：從第一次完整掃描算起
    await scanner.verdict(project, layout)
    assert scan.only[-1] is None


@pytest.mark.parametrize("refusal", ["touka", "limit"])
async def test_new_clean_folder_does_not_reopen_a_refused_project(tmp_path, refusal):
    """已經拒絕（找到 Touka 或掃不完）的專案，多一個乾淨的新資料夾也不會變成可授權。"""
    layout = _workspace_layout(tmp_path)
    project = _project(layout)
    if refusal == "touka":
        (project / "clone" / "ToukaProxy").mkdir(parents=True)
    else:
        _tree(project, *(f"d{i}" for i in range(5)))
    scanner = ProjectScanner(scan=_RecordingScan(), clock=_Clock(), budget=ScanBudget(max_dirs=3))
    expected = PROTECTED if refusal == "touka" else SCAN_LIMIT
    assert await scanner.verdict(project, layout) == expected
    (project / "fresh").mkdir()  # 單獨掃它是乾淨的
    _bump_mtime(project)
    assert await scanner.verdict(project, layout) == expected


async def test_top_folder_swapped_under_the_same_name_is_rescanned(tmp_path):
    """同名資料夾被換掉（例如刪掉後 clone 一份 Touka）也算新增。"""
    layout = _workspace_layout(tmp_path)
    project = _project(layout)
    scanner = ProjectScanner(scan=_RecordingScan(), clock=_Clock())
    assert await scanner.verdict(project, layout) is None
    os.replace(project / "src", tmp_path / "moved_src")  # 舊的還在別處：不會重用同一個 inode
    (project / "src" / "ToukaDispatch").mkdir(parents=True)
    _bump_mtime(project)
    assert await scanner.verdict(project, layout) == PROTECTED


async def test_concurrent_requests_for_one_root_share_a_single_scan(tmp_path):
    layout = _workspace_layout(tmp_path)
    project = _project(layout)
    scan = _GatedScan(open_gate=False)
    scanner = _scanner(scan)
    waiters = [asyncio.ensure_future(scanner.verdict(project, layout)) for _ in range(3)]
    try:
        await _until(lambda: scan.calls >= 1)
        await asyncio.sleep(0.1)  # 其他請求都已經在等
    finally:
        scan.gate.set()
    assert await asyncio.gather(*waiters) == [None, None, None]
    assert scan.calls == 1


async def test_foreground_gives_up_but_the_background_scan_fills_the_cache(tmp_path):
    layout = _workspace_layout(tmp_path)
    project = _project(layout)
    scan = _GatedScan("touka", open_gate=False)
    scanner = _scanner(scan, wait=0.05)
    try:
        assert await scanner.verdict(project, layout) == SCAN_LIMIT  # 前景沒等到
    finally:
        scan.gate.set()
    results = []

    async def _settled() -> bool:
        results.append(await scanner.verdict(project, layout))
        return results[-1] != SCAN_LIMIT

    for _ in range(100):
        if await _settled():
            break
    assert results[-1] == PROTECTED  # 下一條指令用背景的結果
    assert scan.calls == 1  # 用快取，沒有再掃一次


async def test_foreground_wait_bounds_a_hung_scan(tmp_path):
    layout = _workspace_layout(tmp_path)
    project = _project(layout)
    scan = _GatedScan(open_gate=False)
    scanner = _scanner(scan, wait=0.05)
    try:
        verdict = await asyncio.wait_for(scanner.verdict(project, layout), 2.0)
    finally:
        scan.gate.set()
    assert verdict == SCAN_LIMIT


@pytest.mark.parametrize("outcome", ["limit", "error"])
async def test_background_overrun_is_cached_as_scan_limit(tmp_path, outcome):
    layout = _workspace_layout(tmp_path)
    project = _project(layout)
    scan = _GatedScan(outcome)
    scanner = _scanner(scan)
    assert await scanner.verdict(project, layout) == SCAN_LIMIT
    assert await scanner.verdict(project, layout) == SCAN_LIMIT
    assert scan.calls == 1


async def test_touka_directory_itself_is_refused_before_any_scan(tmp_path):
    """往上檢查自己就擋下 Touka 目錄本身（不靠往下掃描，也不受快取影響）。"""
    workspace = tmp_path / "plain"  # 普通的工作區：上層不是 Touka 目錄
    layout = SandboxLayout(
        **{**_workspace_layout(tmp_path).__dict__, "mirror_roots": (workspace.resolve(),)}
    )
    clone = _project(layout, "clone")
    (clone / "ToukaDispatch").mkdir()
    scan = _GatedScan("clean")
    assert await _scanner(scan).verdict(clone, layout) == PROTECTED
    assert scan.calls == 0


async def test_unexpected_scan_failure_is_not_granted_and_not_cached(tmp_path):
    layout = _workspace_layout(tmp_path)
    project = _project(layout)
    calls = []

    def _broken(_root: Path, **_budget: object) -> str:
        calls.append(1)
        raise RuntimeError

    scanner = ProjectScanner(scan=_broken)
    assert await scanner.verdict(project, layout) == SCAN_LIMIT
    assert await scanner.verdict(project, layout) == SCAN_LIMIT
    assert len(calls) == 2  # 下一條指令重試，不把意外記成結果


async def test_upward_check_runs_every_time_even_with_a_cached_scan(tmp_path):
    """快取只存往下掃描；上層變成 Touka 目錄時，下一條指令就拒絕（不等 TTL）。"""
    workspace = tmp_path / "plain"
    layout = SandboxLayout(
        **{**_workspace_layout(tmp_path).__dict__, "mirror_roots": (workspace.resolve(),)}
    )
    (workspace / "data" / "src").mkdir(parents=True)  # 使用者在普通資料夾裡建的「data」專案
    project = (workspace / "data").resolve()
    scan = _GatedScan()
    scanner = _scanner(scan)
    assert await scanner.verdict(project, layout) is None
    (workspace / "ToukaMemory").mkdir()  # 工作區變成 Touka checkout，data 成了受保護名稱
    assert await scanner.verdict(project, layout) == PROTECTED
    assert scan.calls == 1


async def test_scanner_budget_defaults_to_the_background_budget():
    assert ScanBudget() == ScanBudget(max_depth=3, max_dirs=50_000, seconds=30.0)
