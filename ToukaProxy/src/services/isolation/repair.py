"""proxy 端的權限自癒（iso-c2 U2n）：``utils/permission_repair`` 裝飾器的掛鉤.

只在白名單內用 kernel ``owner_repair.repair_chain`` 補自己擁有的 inode 的擁有者位元：W1 掛載根
（``personal_drive``、``mcp_servers``、``mcp_servers/managed``）、W7 ``state_root``、``ST/proxy`` 與
W8 ``custom`` 只修那一層；W2 ``personal_drive/<id>/**``、W3 ``mcp_servers/managed/<skill>/**``、
W4 ``custom/<id>/drive/**``、W5 ``ST/proxy/**`` 修整條鏈；W9 ``custom/<id>`` 只在走向 W4 的路上修。
判斷順序：白名單（原路徑與 resolve 後都要落在同一條）→ ``is_protected_path`` →
頻率限制 → kernel；Touka 自己的狀態一律不修。計數與記錄都不帶路徑與代理人 ID。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from src.sandbox_kernel import owner_repair
from src.services.isolation.profiles import current_layout
from src.services.resource_manager import resource_manager
from src.utils.isolation_policy import is_safe_segment
from src.utils.permission_repair import set_permission_repair
from src.utils.time_utils import get_current_epoch, get_monotonic_ms

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.services.isolation.profiles import SandboxLayout

logger = logging.getLogger(__name__)

TARGET_COOLDOWN_SECONDS = 10.0
WINDOW_SECONDS = 60.0
WINDOW_LIMIT = 120
ROOT_RESERVE = 30  # 根（reserved_roots）每個時間窗另外保留，不受全域上限影響
BURST_WARN_GAP_SECONDS = 600.0
REFUSED_BURST, NOT_OWNER_BURST, UNCHANGED_BURST = 50, 20, 10
WARNINGS_PER_WINDOW = 3  # 修好、重試、被限流：每窗每種最多幾則 WARNING，其餘 DEBUG
PERIODIC_CHECK = "periodic_check"
COUNTERS = (
    "attempted", "repaired", "refused_outside", "refused_protected", "refused_not_owner",
    "nothing_changed", "throttled", "retried", "retry_failed",
)
# 結果 → (單次記錄層級, 彙總告警的組, 門檻)；同一組在同一個時間窗合計達門檻記一則 WARNING
_GROUPS = {
    "refused_outside": (logging.DEBUG, "refused", REFUSED_BURST),
    "refused_protected": (logging.DEBUG, "refused", REFUSED_BURST),
    "refused_not_owner": (logging.INFO, "not_owner", NOT_OWNER_BURST),
    "nothing_changed": (logging.INFO, "unchanged", UNCHANGED_BURST),
}
_AGENT, _STATE = "agent_area", "state_area"


@dataclass(frozen=True)
class WhitelistMatch:
    """``root`` 走訪起點；``scope`` 同一條白名單（含代理人或技能那段）；``reserved`` 根本身。"""

    root: Path
    scope: Path
    kind: str
    reserved: bool


def reserved_roots(layout: SandboxLayout) -> frozenset[Path]:
    """只修那一層、有保留額度、定期檢查的根：W1、W7（``state_root``）、``ST/proxy`` 與 W8。"""
    data = layout.data_root
    roots = {data / "personal_drive", data / "mcp_servers", data / "mcp_servers" / "managed"}
    roots.add(data / "custom")  # W8：W4 的上層，被鎖時定期檢查也修回來
    if layout.state_root is not None:
        roots |= {layout.state_root, layout.state_root / "proxy"}
    return frozenset(roots)


def match_whitelist(target: Path, layout: SandboxLayout) -> WhitelistMatch | None:
    """``target`` 落在哪一條白名單；不在回 None（``*`` 段要通過 ``is_safe_segment``）。"""
    data, state = layout.data_root, layout.state_root
    families = [
        (data / "personal_drive", ("*",), _AGENT),
        (data / "mcp_servers", ("managed", "*"), _AGENT),
        (data / "custom", ("*", "drive"), _AGENT),
        *([(state, ("proxy",), _STATE)] if state is not None else []),
    ]
    for base, pattern, kind in families:
        if not target.is_relative_to(base):
            continue
        head = target.relative_to(base).parts[: len(pattern)]
        pairs = zip(pattern, head, strict=False)
        if any(want not in ("*", got) or not is_safe_segment(got) for want, got in pairs):
            return None
        reserved = target in reserved_roots(layout)
        if not reserved and len(head) < len(pattern):
            return None
        kind = "mount_root" if reserved and kind == _AGENT else kind
        return WhitelistMatch(base, base.joinpath(*head), kind, reserved)
    return None


def _absolute(raw: object) -> Path | None:
    try:
        path = Path(os.path.normpath(os.fsdecode(raw)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return path if path.is_absolute() else None


def _area(target: Path | None, layout: SandboxLayout) -> str:
    """被拒路徑的種類（只分區，不記路徑）。"""
    if target is not None and target.is_relative_to(layout.data_root):
        return "data_root"
    if target is not None and target.is_relative_to(layout.env_file.parent):
        return "env_dir"
    return "other"


def _monotonic_seconds() -> float:
    return get_monotonic_ms() / 1000


class PermissionRepairService:
    """自癒掛鉤：白名單 → 受保護路徑 → 頻率限制 → kernel；計數與時間窗上鎖（多執行緒呼叫）。"""

    def __init__(
        self,
        layout_provider: Callable[[], SandboxLayout] = current_layout,
        *,
        is_protected: Callable[[Path], bool] = resource_manager.is_protected_path,
        repair_chain: Callable[[str, str], owner_repair.RepairOutcome] = owner_repair.repair_chain,
        clock: Callable[[], float] = _monotonic_seconds,
    ) -> None:
        self._layout, self._is_protected = layout_provider, is_protected
        self._repair_chain, self._clock = repair_chain, clock
        self._lock = threading.Lock()
        self._totals: Counter[str] = Counter()
        self._last_repair: dict[Path, float] = {}
        self._last_burst: dict[str, float] = {}
        self._window_start = float("-inf")
        self._roll(clock())

    def _roll(self, now: float) -> None:
        if now - self._window_start < WINDOW_SECONDS:
            return
        self._window_start, self._used, self._reserve_used = now, 0, 0
        self._relock = self._burst = False
        self._window: Counter[str] = Counter()
        self._details: Counter[tuple[str, str, str]] = Counter()
        cutoff = now - TARGET_COOLDOWN_SECONDS
        self._last_repair = {path: at for path, at in self._last_repair.items() if at > cutoff}

    def repair(self, method: str, paths: tuple[object, ...], *, retry: bool) -> bool:
        """裝飾器的掛鉤：每個有值的 filename 都試；任何一個真的有改就回 True。"""
        results = [self._repair_one(method, raw, retry=retry) for raw in paths if raw]
        return any(results)

    def note_retry(self, method: str, succeeded: bool) -> None:
        """記錄修完之後那一次重試的結果。"""
        self._record("retried" if succeeded else "retry_failed", method)

    def check_roots(self) -> int:
        """定期檢查：只修 ``reserved_roots`` 本身，沒改動不記錄；回真的有修的個數。"""
        roots = sorted(reserved_roots(self._layout()))
        return sum(self._repair_one(PERIODIC_CHECK, r, retry=False, quiet=True) for r in roots)

    def snapshot(self) -> dict[str, int | bool | float]:
        """``GET /isolation`` 的 ``repairs``：只有計數與旗標。"""
        with self._lock:
            now = self._clock()
            self._roll(now)
            resets_in = max(0.0, self._window_start + WINDOW_SECONDS - now)
            return {
                **{name: self._totals[name] for name in COUNTERS},
                "permission_relock": self._relock,
                "denied_burst": self._burst,
                "window_resets_at": get_current_epoch() + resets_in,
            }

    def _repair_one(self, method: str, raw: object, *, retry: bool, quiet: bool = False) -> bool:
        layout = self._layout()
        target = _absolute(raw)
        match = None if target is None else match_whitelist(target, layout)
        try:
            resolved = None if match is None else target.resolve()
        except (OSError, RuntimeError):
            resolved = None
        if match is None or resolved is None or match_whitelist(resolved, layout) != match:
            return self._record("refused_outside", method, _area(target, layout))
        if self._is_protected(target) or self._is_protected(resolved):
            return self._record("refused_protected", method, _area(target, layout))
        if not self._admit(target, reserved=match.reserved):
            return False if quiet else self._record("throttled", method, match.kind)
        outcome = self._repair_chain(str(match.root), str(target))
        if outcome.repaired or not quiet:
            name = "refused_not_owner" if outcome.stopped == owner_repair.NOT_OWNER else None
            name = "repaired" if outcome.repaired else name or "nothing_changed"
            self._record(name, method, match.kind, retry=retry)
        return outcome.repaired

    def _admit(self, target: Path, *, reserved: bool) -> bool:
        with self._lock:
            now = self._clock()
            self._roll(now)
            last = self._last_repair.get(target)
            if last is not None and now - last < TARGET_COOLDOWN_SECONDS:
                return False
            if self._used < WINDOW_LIMIT:
                self._used += 1
            elif reserved and self._reserve_used < ROOT_RESERVE:
                self._reserve_used += 1
            else:
                self._relock = True
                return False
            self._last_repair[target] = now
            return True

    def _record(self, name: str, method: str, area: str = "", *, retry: bool | None = None) -> bool:
        """記一筆結果（路徑以外的掛鉤呼叫也算 ``attempted``）；一律回 False 方便 return。"""
        level, group, threshold = _GROUPS.get(name, (logging.WARNING, "", 0))
        message = None
        with self._lock:
            now = self._clock()
            self._roll(now)
            self._totals[name] += 1
            self._window[name] += 1
            if name not in ("retried", "retry_failed"):
                self._totals["attempted"] += 1
            if group:
                self._window[group] += 1
                self._details[group, "method", method] += 1
                self._details[group, "area", area] += 1
                message = self._burst_message(group, threshold, now)
            elif self._window[name] > WARNINGS_PER_WINDOW:
                level = logging.DEBUG
        text = "Permission repair %s: method=%s kind=%s retry=%s"
        logger.log(level, text, name, method, area, retry)
        if message:
            logger.warning(message)
        return False

    def _burst_message(self, group: str, threshold: int, now: float) -> str | None:
        last = self._last_burst.get(group)
        total = self._window[group]
        if total < threshold or (last is not None and now - last < BURST_WARN_GAP_SECONDS):
            return None
        self._last_burst[group], self._burst = now, True
        mine = [(kind, val, n) for (g, kind, val), n in self._details.most_common() if g == group]
        top = [value for kind, value, _ in mine if kind == "method"]
        areas = {value: n for kind, value, n in mine if kind == "area"}
        return f"Permission repair {group} burst: {total} in 60s; methods={top[:3]} areas={areas}"


def install(service: PermissionRepairService | None = None) -> PermissionRepairService | None:
    """只在 Linux 安裝全域掛鉤（第二段由 monitor 第一次 ``ensure_ready`` 呼叫）；否則回 None。"""
    if sys.platform != "linux":
        return None
    service = service or PermissionRepairService()
    set_permission_repair(service)
    return service
