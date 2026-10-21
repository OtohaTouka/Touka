"""推導路徑的 Touka 目錄判定（iso-c2，設計 §3.4）：往上檢查、往下掃描、快取與背景續掃.

``profiles.authorize_derived_path`` 先處理 G 與固定受保護集合 P，剩下的交給這裡：

- 往上檢查每次都做（只列幾個上層資料夾），在執行緒裡跑。
- 往下掃描依 resolve 後的專案根快取（TTL 10 分鐘）。根目錄 mtime 改變時比對第一層的真資料夾
  （名稱＋inode）：沒有新增或同名換掉就沿用、有就只補掃新增的那些（不延長 TTL）。第一次碰到
  某個根時在背景用大預算掃（30 秒、50,000 個資料夾、3 層），前景最多等 2 秒；等不到就這一次
  回 ``scan_limit``，背景掃完寫進快取給下一條指令用；背景也超過上限就快取 ``scan_limit``。
- 同一個根同時只掃一次（single-flight），前景與背景續掃共用同一個任務。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from src.utils.task_utils import spawn_logged
from src.utils.time_utils import get_monotonic_ms
from src.utils.touka_dirs import (
    PROTECTED_LOCATION,
    SCAN_LIMIT,
    ToukaAnchors,
    scan_below,
    top_level_dirs,
    upward_verdict,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from src.services.isolation.profiles import SandboxLayout
    from src.utils.touka_dirs import ScanOutcome

    _Incremental = tuple[frozenset[str], float]  # 補掃：只掃哪些第一層資料夾、TTL 起點

logger = logging.getLogger(__name__)

SCAN_MAX_DEPTH = 3
SCAN_MAX_DIRS = 50_000
SCAN_SECONDS = 30.0
FOREGROUND_WAIT_SECONDS = 2.0
SCAN_CACHE_TTL_SECONDS = 600.0
_VERDICTS: dict[str, str | None] = {"clean": None, "touka": PROTECTED_LOCATION}


class ScanFunction(Protocol):
    """往下掃描（``touka_dirs.scan_below`` 的形狀）。"""

    def __call__(
        self, root: Path, *, max_depth: int, max_dirs: int, seconds: float,
        only: frozenset[str] | None = None,
    ) -> ScanOutcome:
        """回傳 ``clean``／``touka``／``limit``／``error``；``only`` 限定第一層要進的資料夾。"""
        ...


@dataclass(frozen=True)
class ScanBudget:
    """往下掃描的範圍與上限。"""

    max_depth: int = SCAN_MAX_DEPTH
    max_dirs: int = SCAN_MAX_DIRS
    seconds: float = SCAN_SECONDS


@dataclass(frozen=True)
class _Cached:
    verdict: str | None
    mtime_ns: int
    at: float
    top: Mapping[str, int] | None = None  # 掃描前列的第一層真資料夾（名稱 → inode）


def _monotonic_seconds() -> float:
    return get_monotonic_ms() / 1000


def layout_anchors(layout: SandboxLayout) -> ToukaAnchors:
    """E、D、INSTALL_ROOT 一定是 Touka 目錄；E、D 整個封住；往上檢查停在鏡像根。"""
    data = Path(layout.data_root).resolve()
    env_dir = Path(layout.env_file).resolve().parent
    return ToukaAnchors(
        always=(Path(layout.install_root).resolve(),),
        sealed=(data, env_dir),
        stop_roots=tuple(Path(m).resolve() for m in layout.mirror_roots),
    )


def _probe(target: Path, anchors: ToukaAnchors) -> tuple[str | None, int, dict[str, int] | None]:
    """往上檢查，通過時讀根目錄 mtime 與第一層資料夾（快取的失效條件）。"""
    upward = upward_verdict(target, anchors)
    if upward:
        return upward, 0, None
    try:
        return None, target.stat().st_mtime_ns, top_level_dirs(target)
    except OSError:
        return SCAN_LIMIT, 0, None


class ProjectScanner:
    """Touka 目錄判定（DIP：掃描函式、時鐘、預算都可注入）。"""

    def __init__(
        self,
        *,
        budget: ScanBudget | None = None,
        foreground_wait: float = FOREGROUND_WAIT_SECONDS,
        ttl: float = SCAN_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = _monotonic_seconds,
        scan: ScanFunction = scan_below,
    ) -> None:
        self._budget = budget or ScanBudget()
        self._foreground_wait = foreground_wait
        self._ttl = ttl
        self._clock = clock
        self._scan_fn = scan
        self._cache: dict[Path, _Cached] = {}
        self._inflight: dict[Path, asyncio.Task[str | None]] = {}

    async def verdict(self, target: Path, layout: SandboxLayout) -> str | None:
        """``target`` 已 resolve；可授權回 None，否則回拒絕原因。"""
        try:
            upward, mtime_ns, top = await asyncio.wait_for(
                asyncio.to_thread(_probe, target, layout_anchors(layout)), self._foreground_wait
            )
        except TimeoutError:
            return SCAN_LIMIT
        if upward:
            return upward
        cached = self._cache.get(target)
        if cached and self._clock() - cached.at >= self._ttl:
            cached = None
        if cached and cached.mtime_ns == mtime_ns:
            return cached.verdict
        incremental = None
        if cached and cached.top is not None and top is not None:
            # 根目錄變了：第一層沒有新增（或同名換掉）的資料夾就沿用，有就只掃新增的那些
            added = frozenset(name for name, inode in top.items() if cached.top.get(name) != inode)
            if not added or cached.verdict is not None:
                self._cache[target] = replace(cached, mtime_ns=mtime_ns, top=top)
                return cached.verdict
            incremental = (added, cached.at)
        try:
            return await asyncio.wait_for(
                asyncio.shield(self._scan_task(target, mtime_ns, top, incremental)),
                self._foreground_wait,
            )
        except TimeoutError:
            return SCAN_LIMIT  # 背景繼續掃，結果寫進快取給下一條指令

    def _scan_task(
        self,
        root: Path,
        mtime_ns: int,
        top: Mapping[str, int] | None,
        incremental: _Incremental | None = None,
    ) -> asyncio.Task[str | None]:
        task = self._inflight.get(root)
        if task is None or task.done() or task.get_loop() is not asyncio.get_running_loop():
            scan = self._scan(root, mtime_ns, top, incremental)
            task = spawn_logged(scan, "isolation-project-scan")
            self._inflight[root] = task
        return task

    async def _scan(
        self,
        root: Path,
        mtime_ns: int,
        top: Mapping[str, int] | None,
        incremental: _Incremental | None,
    ) -> str | None:
        """``top`` 是掃描之前列的第一層：之後才放進的資料夾，下一次一定算新增。"""
        budget = self._budget
        only, since = incremental or (None, None)
        extra = {} if only is None else {"only": only}
        try:
            outcome = await asyncio.to_thread(
                self._scan_fn,
                root,
                max_depth=budget.max_depth,
                max_dirs=budget.max_dirs,
                seconds=budget.seconds,
                **extra,
            )
        except Exception:
            logger.exception("Touka 目錄掃描失敗，這次不授權")
            return SCAN_LIMIT
        finally:
            if self._inflight.get(root) is asyncio.current_task():
                del self._inflight[root]
        verdict = _VERDICTS.get(outcome, SCAN_LIMIT)
        at = since if since is not None else self._clock()  # 只補掃新增的不延長 TTL
        self._cache[root] = _Cached(verdict, mtime_ns, at, top)
        return verdict


project_scanner = ProjectScanner()
