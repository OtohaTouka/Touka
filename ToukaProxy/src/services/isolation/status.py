"""隔離層級判定（iso-c2）：``IsolationMonitor`` 是唯一判定來源.

兩個狀態分開：
- ``local_landlock``：用 kernel 目錄的啟動器做自測，``pending``／``available``／``unavailable``；
  沒設執行器時的 landlock 層級、人核准降級到本機 Landlock、第三方 MCP 與 scaffold 的本機包裝
  （``local_decision``）都依它判斷。
- ``level``（代理人指令）：部署預期、自測、ABI、kernel 雜湊、sandbox state（ST）。
  設了 ``TOUKA_EXECUTOR_DIR`` 時預期是執行器層：只在 Linux 做健康檢查（``executor_client``），
  READY 的版本、kernel、伺服器程式、封鎖埠、roots、自測、ABI 全部相符才判 ``executor``。
封鎖埠 B 也由這裡算（``block_ports``）：``connections.json`` 讀不到（還沒讀過也算）或 B 溢位時
判 ``none``，降級去處保留執行器或本機 Landlock、features 不含 tcp。
沒有結果就照實判 ``none``，絕不無限等待；背景重探只「請求」重做，不直接改層級（relay 經狀態 fd
回報的執行器代碼例外：先照那個原因判 none，直到下一次健康檢查）。
權限自癒（``repair``）：第一次 ``ensure_ready`` 安裝掛鉤（只在 Linux）；健康檢查約每分鐘修一次根，
ST 不可寫時先修 ``ST/proxy`` 再判斷；計數放在 snapshot 的 ``repairs``。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from src.config import is_container_runtime
from src.sandbox_kernel.framing import block_ports_digest
from src.sandbox_kernel.spec import (
    INTERPRETER_UNTRUSTED,
    MAX_BLOCK_PORTS,
    RELAY_FAILURE_CODES,
    InterpreterUntrustedError,
    SelfTestResult,
    kernel_digest,
    parse_selftest_output,
    trusted_interpreter,
)
from src.services.execution_broker import spawn_exec
from src.services.isolation.block_ports import (
    CONNECTIONS_FILE,
    MCP_REGISTRY_FILE,
    UNBLOCKABLE_PORTS,
    ConnectionsRead,
    core_block_ports,
    read_connection_ports,
    read_registry_ports,
    upstream_port,
)
from src.services.isolation.executor_client import (
    ExecutorExpectation,
    ExecutorHealth,
    ExecutorRoot,
    check_health,
    ready_issue,
)
from src.services.isolation.executor_server import executor_code_digest
from src.services.isolation.mcp_profiles import MCP_PROXY_STATE_KINDS
from src.services.isolation.profiles import (
    BLOCK_PORTS_OVERFLOW,
    SANDBOX_STATE_UNCONFIGURED,
    authorize_derived_path,
    current_layout,
)
from src.services.isolation.repair import PermissionRepairService
from src.services.isolation.repair import install as install_permission_repair
from src.services.process_registry import reap_cancelled_process
from src.services.resource_manager import resource_manager
from src.utils.isolation_policy import (
    EXECUTOR_LAYER_MIN_ABI,
    LANDLOCK_LAYER_MIN_ABI,
    TCP_FEATURE,
    effective_abi,
    landlock_features,
    level_rank,
)
from src.utils.task_utils import spawn_logged
from src.utils.time_utils import get_monotonic_ms

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from src.models.identity import AgentIdentity
    from src.services.isolation.executor_client import ExecutorReady
    from src.services.isolation.profiles import Location, SandboxLayout
    from src.utils.isolation_policy import IsolationLevel
    from src.utils.permission_repair import PermissionRepairHook

logger = logging.getLogger(__name__)
PROBE_PENDING = "probe_pending"

READY_SELFTEST_TIMEOUT_SECONDS = 5.0
READY_HEALTH_TIMEOUT_SECONDS = 2.0
HEALTH_CACHE_OK_SECONDS = 10.0
HEALTH_CACHE_FAIL_SECONDS = 1.0
BOOT_GRACE_SECONDS = 60.0
GRACE_INTERVAL_SECONDS = 2.0
BELOW_EXPECTED_INTERVAL_SECONDS = 30.0
AT_EXPECTED_RECHECK_SECONDS = 60.0
SELFTEST_RETRY_SECONDS = 600.0
MARKER_SELFTEST_MIN_GAP_SECONDS = 60.0
SELFTEST_TIMEOUT_SECONDS = 30.0
ROOT_REPAIR_INTERVAL_SECONDS = 60.0  # 權限自癒的定期檢查（W1、W7、W8、ST/proxy 本身）
LAUNCHER_NAME = "launcher.py"
SELFTEST_DIR_PREFIX = ".touka-selftest-"  # 每次自測加隨機尾碼；自測前清掉同前綴的殘留
LANDLOCK_UNAVAILABLE = "landlock_unavailable"  # 啟動器自測的代碼，也是 monitor 的原因
# 判定本身出錯（讀不到 kernel、monitor／專案／session 查詢拋例外）：照 none 處理（fail-closed）
CHECK_FAILED = "isolation_check_failed"
# 核心層的自測結論（和資料所在的檔案系統無關）：背景重試照舊在 ST 上做
_KERNEL_VERDICTS = frozenset(
    {"landlock_abi_too_old", LANDLOCK_UNAVAILABLE, "kernel_not_installed", INTERPRETER_UNTRUSTED}
)
NATIVE_PLATFORM = "native_platform"  # 不是 Linux（不自測）：預期是 none
STATE_UNSET = "unset"  # ST 沒設（容器沒設 ``TOUKA_SANDBOX_STATE_DIR``）；寫不進去是 not_writable
# 第三方 MCP 可以照今天的方式未隔離啟動的本機原因（平台、核心、映像、直譯器，不會自己好；C2-3
# MEDIUM-5）。其他（自測失敗、9p、ST 寫不進去、讀不到 kernel、看不懂的）都是暫時性：先等，恢復時
# 由 listener 啟動（ST 沒設另看細節，見 ``LocalDecision.stable``）
MCP_STABLE_LOCAL_REASONS = _KERNEL_VERDICTS | {NATIVE_PLATFORM, "kernel_outdated"}
EXECUTOR_UNREACHABLE = "executor_unreachable"
AGENT_DIR_NOT_IN_EXECUTOR = "agent_dir_not_in_executor"
PROJECT_NOT_IN_EXECUTOR = "project_not_in_executor"
BLOCK_PORTS_INCOMPLETE = "block_ports_incomplete"
# proxy 的 ST 版面 ``ST/proxy/<kind>/<owner>/{home,tmp}``；開機時把 tmp 改名成這個前綴再刪
# （MCP 的自建與第三方分開父目錄，名稱只有 ``mcp_profiles`` 一份）
STATE_KINDS = ("agents", "evolution", *MCP_PROXY_STATE_KINDS.values())
TMP_TRASH_PREFIX = "tmp.trash-"

LocalStatus = Literal["pending", "available", "unavailable"]
LocalDecisionStatus = Literal["available", "pending", "unavailable"]
# connections.json：還沒讀成功過（等同讀不到）／讀成功過（含確認不存在）／從沒讀成功、最近一次讀不到
ConnectionsState = Literal["unread", "read", "unreadable"]
SelfTestLocation = Literal["data", "state"]


def _monotonic_seconds() -> float:
    return get_monotonic_ms() / 1000


@dataclass(frozen=True)
class LandlockState:
    """proxy 本機 Landlock 的狀態；``net`` 是自測另外回報、TCP 規則擋不住的網路設定。"""

    status: LocalStatus
    abi: int = 0
    reason: str | None = None
    net: str | None = None

    @property
    def features(self) -> frozenset[str]:
        """可用時依 ABI 對照表生效的權利；網路設定不安全時扣掉 tcp（比照 ABI 3）。"""
        if self.status != "available":
            return frozenset()
        return landlock_features(self.abi) - ({TCP_FEATURE} if self.net else set())


@dataclass(frozen=True)
class IsolationSnapshot:
    """某一時刻的判定結果。"""

    level: IsolationLevel
    expected: IsolationLevel
    reason: str | None
    detail: str | None
    local: LandlockState
    features: frozenset[str]
    # proxy 本機 Landlock 讓降級只能「未隔離」的原因（還在等自測、不可用、kernel、ST）
    local_issue: str | None = None
    # 可以用的執行器（版本、kernel、roots、自測都相符；可能只差網路設定）的 READY
    executor: ExecutorReady | None = None
    # B 擋不全（讀不到設定或溢位）：判 none、降級後 features 不含 tcp
    block_ports_issue: str | None = None
    # 權限自癒的計數與旗標（``PermissionRepairService.snapshot``，不帶路徑）；沒有掛鉤時是 None
    repairs: dict[str, int | bool | float] | None = None


@dataclass(frozen=True)
class _DiskCheck:
    """refresh 在執行緒那一段的結果；執行緒只回傳，寫進 monitor 一律在事件迴圈上."""

    layout: SandboxLayout
    state_issue: str | None
    kernel_digest: str | None
    live_digest: str | None
    kernel_readable: bool
    server_digest: str | None = None
    connections: ConnectionsRead = ConnectionsRead(ok=False)
    registry: ConnectionsRead = ConnectionsRead(ok=True)


@dataclass(frozen=True)
class LevelDecision:
    """某個代理人、某個專案根的實際層級與原因。"""

    level: IsolationLevel
    reason: str | None = None


@dataclass(frozen=True)
class LocalDecision:
    """proxy 本機 Landlock 能不能包第三方 MCP 與 scaffold（``local_decision``；不看部署預期）.

    ``pending`` 只指 Linux 上還沒有自測結果；``features`` 只在可用時有。
    """

    status: LocalDecisionStatus
    reason: str | None = None
    detail: str | None = None
    features: frozenset[str] = frozenset()

    @property
    def stable(self) -> bool:
        """不可用的原因是穩定的：ST 只有沒設才算（寫不進去是暫時的）；看不懂的原因一律暫時."""
        if self.status != "unavailable":
            return False
        if self.reason == SANDBOX_STATE_UNCONFIGURED:
            return self.detail == STATE_UNSET
        return self.reason in MCP_STABLE_LOCAL_REASONS


@dataclass(frozen=True)
class FallbackPlan:
    """層級 none 經人核准後的降級去處（卡片照 features 選文案；不含 tcp 時 net_strict 是假）。"""

    kind: Literal["executor", "landlock", "direct"]
    features: frozenset[str]


class SelfTestProbe(Protocol):
    """在指定資料夾做啟動器自測。"""

    async def __call__(self, kernel_dir: Path, test_dir: Path) -> SelfTestResult:
        """執行一次自測。"""
        ...


class ExecutorHealthProbe(Protocol):
    """對執行器做一次健康檢查（``executor_client.check_health`` 的形狀）。"""

    async def __call__(self, ipc_dir: Path, block_ports: tuple[int, ...]) -> ExecutorHealth:
        """送這次要用的封鎖埠，回 READY 或原因。"""
        ...


class ExecutorDigests(Protocol):
    """預期的伺服器程式雜湊與封鎖埠雜湊；算法屬於執行器伺服器，接線時注入它的 SSOT。"""

    def server_digest(self, proxy_root: Path) -> str | None:
        """原始碼裡伺服器程式的雜湊（會讀檔：在執行緒裡呼叫）；算不出來回 None。"""
        ...

    def block_ports_digest(self, block_ports: tuple[int, ...]) -> str | None:
        """這份封鎖埠的雜湊；算不出來回 None。"""
        ...


class ExecutorServerDigests:
    """執行器伺服器的 SSOT：程式清單雜湊（``executor_code_digest``）與封鎖埠雜湊（framing）."""

    def server_digest(self, proxy_root: Path) -> str | None:
        """原始碼裡伺服器程式（``EXECUTOR_CODE_FILES``）的雜湊；讀不到就拋（呼叫端判版本不符）。"""
        return executor_code_digest(proxy_root)

    def block_ports_digest(self, block_ports: tuple[int, ...]) -> str | None:
        """這份封鎖埠的雜湊（排序、去重）。"""
        return block_ports_digest(block_ports)


def _no_upstream_urls() -> tuple[str, ...]:
    return ()


def expected_executor_roots(layout: SandboxLayout) -> frozenset[ExecutorRoot]:
    """執行器應該掛的三個 root：代理人資料夾與 MCP 資料夾可寫、skills 唯讀。"""
    data = layout.data_root
    return frozenset(
        {
            ExecutorRoot(str(data / "personal_drive"), "rw"),
            ExecutorRoot(str(data / "mcp_servers"), "rw"),
            ExecutorRoot(str(layout.config_dir / "skills"), "ro"),
        }
    )


async def launcher_selftest(kernel_dir: Path, test_dir: Path) -> SelfTestResult:
    """預設自測：``<可信直譯器> -I -S <kernel>/launcher.py --self-test --pin --dir <test_dir>``.

    直譯器同 runner（容器裡整條擁有者鏈要 root，不合格回 ``interpreter_untrusted``）。環境是空的：
    不繼承 proxy 的環境（黑名單過濾的 ``get_clean_env`` 不能用在隔離邊界），是執行器指令允許清單
    的子集，同執行器伺服器的自測。
    """
    try:
        python = trusted_interpreter(require_root_owned=is_container_runtime())
    except InterpreterUntrustedError:
        logger.warning("Isolation self-test interpreter is not trusted")
        return SelfTestResult(ok=False, error=INTERPRETER_UNTRUSTED)
    launcher = kernel_dir / LAUNCHER_NAME
    proc = await spawn_exec(
        python, "-I", "-S", str(launcher), "--self-test", "--pin", "--dir", str(test_dir),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={},
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), SELFTEST_TIMEOUT_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return SelfTestResult(ok=False, error="timeout")
    except asyncio.CancelledError:
        await reap_cancelled_process(proc, _kill_launcher)  # 先結束它，_probe 才清資料夾
        raise
    return parse_selftest_output(proc.returncode, stdout)


def _kill_launcher(proc: asyncio.subprocess.Process) -> None:
    """只殺自測的啟動器本身（它不是新行程群組，killpg 會殺到 proxy）。"""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


class IsolationMonitor:
    """隔離層級的唯一判定來源（DIP：版面、自測、時鐘都可注入）。"""

    def __init__(
        self,
        *,
        layout_provider: Callable[[], SandboxLayout] = current_layout,
        selftest: SelfTestProbe = launcher_selftest,
        clock: Callable[[], float] = _monotonic_seconds,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        platform: str = sys.platform,
        start_background: bool = True,
        ready_timeout: float = READY_SELFTEST_TIMEOUT_SECONDS,
        health_timeout: float = READY_HEALTH_TIMEOUT_SECONDS,
        executor_health: ExecutorHealthProbe = check_health,
        executor_digests: ExecutorDigests | None = None,
        upstream_urls: Callable[[], Iterable[str]] = _no_upstream_urls,
    ) -> None:
        self._layout_provider = layout_provider
        self._selftest = selftest
        self._clock = clock
        self._sleep = sleep
        self._platform = platform
        self._start_background = start_background
        self._ready_timeout = ready_timeout
        self._health_timeout = health_timeout
        self._executor_health = executor_health
        self._digests = executor_digests or ExecutorServerDigests()
        self._upstream_sources = [upstream_urls]  # 記憶體裡的上游來源（路由快照、registry）
        self._listeners: list[Callable[[IsolationSnapshot], Awaitable[None]]] = []
        self._reset()

    def _reset(self) -> None:
        self._local = LandlockState("pending")
        self._state_issue: str | None = None
        self._kernel_issue: str | None = None
        self._kernel_digest: str | None = None
        self._image_digest: tuple[Path, str] | None = None  # 映像裡唯讀的 kernel 只雜湊一次
        self._layout: SandboxLayout | None = None
        self._refreshing: asyncio.Task[None] | None = None
        self._data_verified = False  # 資料所在檔案系統（9p）的自測通過過
        self._executor: ExecutorHealth | None = None  # 最近一次健康檢查（或 relay 回報）
        self._executor_issue: str | None = None  # READY 和預期不符的原因
        self._boot_id: str | None = None
        self._boot_changed = False
        self._known_ports: set[int] = set()  # 行程存活期間只增不減
        self._connections: ConnectionsState = "unread"
        self._connections_error: str | None = None
        self._registry_error: str | None = None
        self._overflow = False
        self._warned_ports: set[int] = set()
        self._health_at: float | None = None
        self._health_ok = False
        self._started_at: float | None = None
        self._initial: asyncio.Task[None] | None = None
        self._background: asyncio.Task[None] | None = None
        self._selftest_lock = asyncio.Lock()
        self._last_selftest = 0.0
        self._marker_at: float | None = None
        self._last_seen: IsolationSnapshot | None = None
        self._repair: PermissionRepairService | None = None  # 第一次 ensure_ready 時安裝
        self._roots_checked_at = float("-inf")

    # --- 判定 ---------------------------------------------------------------------------

    @property
    def checked_kernel_digest(self) -> str | None:
        """最近一次健康檢查看到的 kernel 目錄雜湊（原生模式 spawn 前比對用）。"""
        return self._kernel_digest

    def _expected(self, layout: SandboxLayout) -> tuple[IsolationLevel, str | None]:
        linux = self._platform.startswith("linux")
        if layout.executor_dir is not None:
            # 只有 Linux 做健康檢查；其他平台照實判連不上
            return "executor", (None if linux else EXECUTOR_UNREACHABLE)
        if linux:
            return "landlock", None
        return "none", NATIVE_PLATFORM

    def _current_layout(self) -> SandboxLayout:
        """事件迴圈上用的版面：refresh 在執行緒裡算好的快取；還沒有時才當場算一次."""
        if self._layout is None:
            self._layout = self._layout_provider()
        return self._layout

    def snapshot(self) -> IsolationSnapshot:
        """目前（以快取資料）的判定；不做任何 I/O 探測。"""
        expected, blocked = self._expected(self._current_layout())
        level, reason, detail = self._judge(expected, blocked)
        issue = self._local_issue()
        ready = self._usable_ready() if expected == "executor" and not blocked else None
        features = self._executor_features(ready) if level == "executor" and ready else None
        return IsolationSnapshot(
            level, expected, reason, detail, self._local,
            self._local.features if features is None else features,
            local_issue=issue[0] if issue else None,
            executor=ready,
            block_ports_issue=self._block_ports_issue() if expected != "none" else None,
            repairs=self._repair.snapshot() if self._repair is not None else None,
        )

    def _usable_ready(self) -> ExecutorReady | None:
        """READY 和預期全部相符（可能只差網路設定）時回它，否則 None."""
        health = self._executor
        if health is None or health.ready is None or self._executor_issue:
            return None
        return health.ready

    def _executor_features(self, ready: ExecutorReady) -> frozenset[str]:
        """執行器那一層的 features：依它的 ABI；網路設定不安全或 B 擋不全時扣掉 tcp."""
        tcp_unsafe = ready.net or self._block_ports_issue()
        return landlock_features(ready.abi) - ({TCP_FEATURE} if tcp_unsafe else set())

    def _local_issue(self) -> tuple[str | None, str | None] | None:
        """讓降級只能「未隔離」的本機原因（原因、細節）；沒有回 None."""
        local = self._local
        if local.status == "pending":
            return PROBE_PENDING, None
        if local.status == "unavailable":
            return local.reason, None
        if self._kernel_issue:
            return self._kernel_issue, None
        if self._state_issue:
            return SANDBOX_STATE_UNCONFIGURED, self._state_issue
        return None

    def _judge(
        self, expected: IsolationLevel, blocked: str | None
    ) -> tuple[IsolationLevel, str | None, str | None]:
        """決定降級去處的原因（自測、kernel、ST）排在只影響 tcp 的原因（ABI、網路設定）之前."""
        if expected == "none" or blocked:
            return "none", blocked, None
        if expected == "executor":
            return self._judge_executor()
        return self._judge_local()

    def _judge_local(self) -> tuple[IsolationLevel, str | None, str | None]:
        """本機 Landlock 層（預期 landlock 的層級，也是 scaffold 用的本機層級）."""
        issue = self._local_issue()
        if issue:
            return "none", *issue
        local = self._local
        if effective_abi(local.abi) < LANDLOCK_LAYER_MIN_ABI:
            return "none", "landlock_abi_insufficient", None
        if local.net:  # 擋不住全部連線：不算 landlock 層，核准後只是檔案隔離
            return "none", local.net, None
        ports_issue = self._block_ports_issue()
        if ports_issue:
            return "none", ports_issue, None
        return "landlock", None, None

    def local_decision(self) -> LocalDecision:
        """第三方 MCP 與 scaffold 看的本機判定（C2-3 §2.4）：不看部署預期的層級.

        不是 Linux、或預期是 none：不可用、``native_platform``（不看永遠不會做的自測，HIGH-2）；
        只有 Linux 上還沒有自測結果才是 pending。可用時 features 照本機，網路設定不安全或 B 擋
        不全時不含 tcp（同降級，照樣包）。
        """
        expected, _ = self._expected(self._current_layout())
        if expected == "none" or not self._platform.startswith("linux"):
            return LocalDecision("unavailable", NATIVE_PLATFORM)
        issue = self._local_issue()
        if issue is not None:
            status: LocalDecisionStatus = "pending" if self._local.status == "pending" else (
                "unavailable"
            )
            return LocalDecision(status, *issue)
        tcp = {TCP_FEATURE} if self._block_ports_issue() else set()
        return LocalDecision("available", features=self._local.features - tcp)

    def local_level(self) -> LevelDecision:
        """本機層級（scaffold 的試跑）：本機可用、ABI≥4、網路設定安全、B 完整才是 landlock."""
        decision = self.local_decision()
        if decision.status != "available":
            return LevelDecision("none", decision.reason)
        level, reason, _detail = self._judge_local()
        return LevelDecision(level, reason)

    def _judge_executor(self) -> tuple[IsolationLevel, str | None, str | None]:
        """執行器層：健康檢查通過、READY 全部相符；ABI≥4 時網路設定還要安全（ABI 3 照樣算）."""
        health = self._executor
        if health is None:
            return "none", PROBE_PENDING, None
        if health.ready is None:
            return "none", health.reason, None
        if self._executor_issue:
            return "none", self._executor_issue, None
        ready = health.ready
        if effective_abi(ready.abi) >= LANDLOCK_LAYER_MIN_ABI and ready.net:
            return "none", ready.net, None  # 加一行覆寫就修得好：要人核准，去處仍是執行器
        ports_issue = self._block_ports_issue()
        if ports_issue:
            return "none", ports_issue, None
        return "executor", None, None

    # --- 封鎖埠 B -----------------------------------------------------------------------

    def block_ports(self, location: Location) -> tuple[int, ...]:
        """這次實際要用的 B（profile 與健康檢查共用）：溢位時只用核心 B，否則核心 ∪ 已知的上游埠.

        記憶體裡的上游來源在這裡併進已知集合（事件迴圈上）；超過上限就改層級並通知（不只在 spawn
        時失敗）。
        """
        ports = (upstream_port(url) for source in self._upstream_sources for url in source())
        self._learn(port for port in ports if port is not None)
        core = core_block_ports(location, self._warn_unblockable)
        if self._check_overflow():
            return core
        return tuple(sorted({*core, *self._known_ports}))

    def _block_ports_issue(self) -> str | None:
        if self._overflow:
            return BLOCK_PORTS_OVERFLOW
        return None if self._connections == "read" else BLOCK_PORTS_INCOMPLETE

    def _learn(self, ports: Iterable[int]) -> None:
        for port in ports:
            if port in UNBLOCKABLE_PORTS:
                self._warn_unblockable(port)
            else:
                self._known_ports.add(port)

    def _warn_unblockable(self, port: int) -> None:
        if port not in self._warned_ports:
            self._warned_ports.add(port)
            logger.warning("Upstream port %d cannot be blocked (web needs it); left open", port)

    def _check_overflow(self) -> bool:
        """完整 B（以 proxy 位置算，它包含執行器的）超過上限：判溢位（只增不減，重新啟動才會好）."""
        if not self._overflow:
            full = {*core_block_ports("proxy", self._warn_unblockable), *self._known_ports}
            if len(full) > MAX_BLOCK_PORTS:
                self._overflow = True
                logger.warning("Too many ports to block (%d); only the core ports stay", len(full))
                self._notify(self.snapshot())
        return self._overflow

    def _apply_connections(self, result: ConnectionsRead) -> None:
        """讀成功一次之後再讀不到：沿用已知的埠、層級不變；從沒讀成功過就是讀不到（只記型別名稱）."""
        if result.ok:
            self._connections, self._connections_error = "read", None
            self._learn(result.ports)
            return
        if result.error != self._connections_error:
            keeping = "keeping the known upstream ports" if self._connections == "read" else (
                "upstream ports are incomplete"
            )
            logger.warning("connections.json could not be read (%s); %s", result.error, keeping)
        self._connections_error = result.error
        if self._connections != "read":
            self._connections = "unreadable"

    def _apply_registry(self, result: ConnectionsRead) -> None:
        """MCP registry 是額外來源：讀到的埠併進已知集合；讀不到只記型別名稱，層級不變."""
        if result.ok:
            self._registry_error = None
            self._learn(result.ports)
        elif result.error != self._registry_error:
            self._registry_error = result.error
            logger.warning("MCP registry unreadable (%s); its ports are not added", result.error)

    async def level_for(
        self, identity: AgentIdentity, project_root: Path | None
    ) -> LevelDecision:
        """這次執行的層級：專案放不進隔離、或不在執行器範圍（``executor_scope_issue``）是 none."""
        snap = self.snapshot()
        if snap.level == "none":
            issue = self.executor_scope_issue(identity, project_root) if (
                snap.expected == "executor"
            ) else None
            # 原因排序（§2.8）：不在執行器範圍時，決定降級去處的是範圍（本機也壞了就是本機的原因），
            # 排在執行器與網路原因前面
            return LevelDecision("none", (snap.local_issue or issue) if issue else snap.reason)
        if snap.level == "executor":
            issue = self.executor_scope_issue(identity, project_root)
            return LevelDecision("none", issue) if issue else LevelDecision("executor")
        if project_root is not None:
            layout = self._current_layout()
            verdict = await authorize_derived_path(project_root, layout, identity.agent_id)
            if verdict:
                return LevelDecision("none", verdict)
        return LevelDecision(snap.level)

    def executor_scope_issue(
        self, identity: AgentIdentity, project_root: Path | None
    ) -> str | None:
        """這次能不能在執行器裡跑：代理人資料夾要在可寫的 root 裡，也不能綁專案.

        這批不掛工作區，所以綁了專案的一律不在執行器範圍。
        """
        drive = resource_manager.get_vault_path("personal", identity)
        writable = [
            Path(root.path)
            for root in expected_executor_roots(self._current_layout())
            if root.access == "rw"
        ]
        if drive is None or not any(Path(drive).resolve().is_relative_to(r) for r in writable):
            return AGENT_DIR_NOT_IN_EXECUTOR
        return PROJECT_NOT_IN_EXECUTOR if project_root is not None else None

    def fallback_for(
        self, *, profile_complete: bool, executor_eligible: bool = False
    ) -> FallbackPlan:
        """層級 none 經人核准後的去處，依序取當下最強的.

        1. ``executor_eligible``（這次在執行器範圍內，見 ``executor_scope_issue``）而且執行器可用、
           只差網路設定 → 執行器；features 不含 tcp。
        2. 本機 Landlock 可用、ST 可用、需要的路徑都能授權 → landlock。kernel 和原始碼不一致或
           讀不到（``_kernel_issue``）時一律未隔離：``/opt`` 的舊啟動器會拒絕新的 spec 鍵
           （``spec_invalid``），寫「隔離降級」的卡核准後必定不執行。
        3. 其他 → 未隔離。
        """
        expected, blocked = self._expected(self._current_layout())
        ready = self._usable_ready() if expected == "executor" and not blocked else None
        if executor_eligible and ready is not None:
            return FallbackPlan("executor", self._executor_features(ready))
        local = self._local
        usable = local.status == "available" and effective_abi(local.abi) >= EXECUTOR_LAYER_MIN_ABI
        if usable and not self._state_issue and not self._kernel_issue and profile_complete:
            tcp = {TCP_FEATURE} if self._block_ports_issue() else set()
            return FallbackPlan("landlock", local.features - tcp)
        return FallbackPlan("direct", frozenset())

    # --- 探測 ---------------------------------------------------------------------------

    async def ensure_ready(self) -> IsolationSnapshot:
        """第一次等自測結果（上限 5 秒）與健康檢查（上限 2 秒）；逾時照實回 none。"""
        expected, _ = self._expected(self._current_layout())
        if expected == "none" or not self._platform.startswith("linux"):
            return self.snapshot()
        if self._initial is None:
            # 權限自癒的掛鉤：只在 Linux 安裝，白名單用 monitor 快取的版面
            self._repair = install_permission_repair(PermissionRepairService(self._current_layout))
            self._started_at = self._clock()
            self._initial = spawn_logged(self._boot(), "isolation-selftest-boot")
            if self._start_background:
                self._background = spawn_logged(self._background_loop(), "isolation-reprobe")
        if not self._initial.done():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._initial), self._ready_timeout)
        if self._health_stale():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.refresh(), self._health_timeout)
        return self.snapshot()

    def _health_stale(self) -> bool:
        if self._health_at is None:
            return True
        ttl = HEALTH_CACHE_OK_SECONDS if self._health_ok else HEALTH_CACHE_FAIL_SECONDS
        return self._clock() - self._health_at >= ttl

    async def refresh(self) -> IsolationSnapshot:
        """健康檢查（同時只跑一次）：ST 可寫、kernel 雜湊；結果依成功／失敗快取.

        呼叫端逾時只放棄等待，這一輪照樣跑完、結果照樣寫進來（磁碟慢時不會重複開執行緒）。
        """
        task = self._refreshing
        if task is None or task.done() or task.get_loop() is not asyncio.get_running_loop():
            task = self._refreshing = spawn_logged(self._refresh_once(), "isolation-refresh")
        await asyncio.shield(task)
        return self.snapshot()

    async def _refresh_once(self) -> None:
        """兩段：磁碟 I/O（版面、kernel 雜湊、ST）在執行緒裡，合併與判定在事件迴圈上."""
        roots_due = self._clock() - self._roots_checked_at >= ROOT_REPAIR_INTERVAL_SECONDS
        if roots_due:
            self._roots_checked_at = self._clock()
        disk = await asyncio.to_thread(self._check_disk, self._image_digest, roots_due)
        self._apply_disk(disk)
        self.block_ports("proxy")  # 併入記憶體裡的上游來源、檢查溢位
        await self._check_executor(disk)
        snap = self.snapshot()
        self._health_ok = snap.level == snap.expected
        self._health_at = self._clock()
        self._notify(snap)

    def _check_disk(self, image: tuple[Path, str] | None, roots_due: bool = False) -> _DiskCheck:
        """執行緒裡跑：不寫 monitor 的狀態（權限自癒只補擁有者位元，計數在服務自己的鎖裡）."""
        layout = self._layout_provider()
        repair = self._repair
        if repair is not None and roots_due:
            try:
                repair.check_roots()  # 根本身被鎖時補回；沒有改動不記錄
            except Exception as exc:  # 自癒出錯不影響健康檢查
                logger.warning("Periodic permission repair failed (%s)", type(exc).__name__)
        state_issue = _state_issue(layout)
        if state_issue == "not_writable" and repair is not None:
            _repair_state_dir(repair, layout)  # 先修一次再判斷
            state_issue = _state_issue(layout)
        connections = read_connection_ports(layout.config_dir / CONNECTIONS_FILE)
        registry = read_registry_ports(layout.config_dir / MCP_REGISTRY_FILE)
        live_dir = layout.proxy_root / "src" / "sandbox_kernel"
        try:
            if layout.in_container and image is not None and image[0] == layout.kernel_dir:
                kernel = image[1]  # 映像裡的 kernel 唯讀，算過就不再算
            else:
                kernel = kernel_digest(layout.kernel_dir)
            live = kernel if layout.kernel_dir == live_dir else kernel_digest(live_dir)
        except OSError:
            logger.warning("Isolation kernel could not be read", exc_info=True)
            return _DiskCheck(layout, state_issue, None, None, False, None, connections, registry)
        server = self._server_digest(layout) if layout.executor_dir is not None else None
        return _DiskCheck(layout, state_issue, kernel, live, True, server, connections, registry)

    def _server_digest(self, layout: SandboxLayout) -> str | None:
        try:
            return self._digests.server_digest(layout.proxy_root)
        except Exception as exc:  # 算不出來就是版本不符（fail-closed）
            logger.warning("Executor server digest failed (%s)", type(exc).__name__)
            return None

    def _apply_disk(self, disk: _DiskCheck) -> None:
        layout = self._layout = disk.layout
        self._state_issue = disk.state_issue
        self._apply_connections(disk.connections)
        self._apply_registry(disk.registry)
        if not disk.kernel_readable:
            self._kernel_digest, self._kernel_issue = None, CHECK_FAILED
            return
        if layout.in_container and layout.kernel_dir and disk.kernel_digest:
            self._image_digest = (layout.kernel_dir, disk.kernel_digest)
        self._kernel_digest = disk.kernel_digest
        outdated = disk.kernel_digest != disk.live_digest
        self._kernel_issue = "kernel_outdated" if outdated else None

    async def _check_executor(self, disk: _DiskCheck) -> None:
        """預期是執行器層時做健康檢查，再把 READY 和預期逐項核對（寫入都在事件迴圈上）."""
        layout = disk.layout
        expected, blocked = self._expected(layout)
        if expected != "executor" or blocked or layout.executor_dir is None:
            self._executor = self._executor_issue = None
            return
        ports = self.block_ports("executor")  # 這次指令實際會用的那份（溢位時是核心 B）
        try:
            health = await self._executor_health(layout.executor_dir, ports)
        except Exception as exc:  # 健康檢查本身出錯：照連不上判（fail-closed），不往外丟
            logger.warning("Executor health check failed (%s)", type(exc).__name__)
            health = ExecutorHealth(None, EXECUTOR_UNREACHABLE)
        issue = None
        if health.ready is not None:
            expectation = ExecutorExpectation(
                kernel_digest=disk.live_digest,
                server_digest=disk.server_digest,
                block_ports_digest=self._digests.block_ports_digest(ports),
                roots=expected_executor_roots(layout),
            )
            issue = ready_issue(health.ready, expectation)
            if not disk.kernel_readable:
                issue = CHECK_FAILED
            self._note_boot(health.ready.boot_id)
        self._executor, self._executor_issue = health, issue

    def _note_boot(self, boot_id: str) -> None:
        """執行器重啟過（boot_id 變了）：下一次通知一定送出."""
        if self._boot_id is not None and boot_id != self._boot_id:
            self._boot_changed = True
        self._boot_id = boot_id

    async def _boot(self) -> None:
        """先清 proxy 的 tmp（在任何自測與指令之前改名，刪除放背景），再自測、健康檢查."""
        try:
            trash = await asyncio.to_thread(_rename_proxy_tmp, self._current_layout().state_root)
        except Exception:  # 清不掉只影響磁碟空間：照常開機
            logger.exception("Could not clear the proxy sandbox tmp folders")
            trash = []
        if trash:
            spawn_logged(asyncio.to_thread(_remove_trash, trash), "isolation-proxy-tmp-cleanup")
        await self.run_selftest("data", notify=False)
        await self.refresh()

    async def run_selftest(self, location: SelfTestLocation, *, notify: bool = True) -> None:
        """在資料所在檔案系統（9p）或 ST 上做一次自測，更新 ``local_landlock``。"""
        async with self._selftest_lock:
            layout = self._current_layout()
            result = await self._probe(layout, location)
            self._last_selftest = self._clock()
            if location == "data" and result.status == "available":
                self._data_verified = True
            current = self._local
            recovering_9p = current.reason == "fs_9p" and result.status == "available"
            if not (recovering_9p and location == "state"):
                self._local = result
        if notify:
            self._notify(self.snapshot())

    async def _probe(self, layout: SandboxLayout, location: SelfTestLocation) -> LandlockState:
        kernel = layout.kernel_dir
        if kernel is None or not resource_manager.exists(kernel / LAUNCHER_NAME):
            return LandlockState("unavailable", reason="kernel_not_installed")
        state_base = layout.state_root / "proxy" if layout.state_root else None
        base = layout.data_root / "mcp_servers" if location == "data" else state_base
        if base is None:
            return LandlockState("unavailable", reason="selftest_failed")
        test_dir = base / f"{SELFTEST_DIR_PREFIX}{secrets.token_hex(8)}"
        try:
            resource_manager.make_dir(base)
            _clear_stale_selftests(base)
            result = await self._selftest(kernel, test_dir)
        except Exception:  # 自測丟出任何例外都算失敗（取消照常往外傳）
            logger.exception("Isolation self-test could not run (%s)", location)
            result = SelfTestResult(ok=False, error="selftest_error")
        finally:
            with contextlib.suppress(OSError):
                resource_manager.remove_dir(test_dir)
        if result.ok and result.abi >= EXECUTOR_LAYER_MIN_ABI:
            return LandlockState("available", abi=result.abi, net=result.net)
        if 0 < result.abi < EXECUTOR_LAYER_MIN_ABI or result.error == "abi_too_old":
            return LandlockState("unavailable", abi=result.abi, reason="landlock_abi_too_old")
        if result.error in {LANDLOCK_UNAVAILABLE, INTERPRETER_UNTRUSTED}:
            # 核心沒有 Landlock、直譯器不可信：和「自測失敗」分開，卡片才能照實說明
            return LandlockState("unavailable", reason=result.error)
        reason = "fs_9p" if location == "data" and result.abi else "selftest_failed"
        logger.warning("Isolation self-test failed on %s: %s", location, result.error)
        return LandlockState("unavailable", abi=result.abi, reason=reason)

    def report_failure(self, code: str) -> None:
        """執行時看到失敗標記：只請求重探（不分原因，60 秒內最多一次），不直接改層級.

        relay 的代碼（``RELAY_FAILURE_CODES``）只會經狀態 fd 來，指令自己寫不進去：執行器先照這個
        原因判 none（只會更嚴），下一次健康檢查再判；本機 Landlock 和它無關，不重做自測。
        """
        if code in RELAY_FAILURE_CODES:
            logger.warning("Executor failure reported (%s); re-checking its health", code)
            self._executor, self._executor_issue = ExecutorHealth(None, code), None
            self._health_ok, self._health_at = False, None
            self._notify(self.snapshot())
            return
        now = self._clock()
        last = self._marker_at
        if last is not None and now - last < MARKER_SELFTEST_MIN_GAP_SECONDS:
            return
        self._marker_at = now
        logger.warning("Isolation failure reported (%s); re-probing", code)
        self._health_at = None
        spawn_logged(self.run_selftest("data"), "isolation-selftest-marker")

    # --- 背景重探 -----------------------------------------------------------------------

    def next_interval(self) -> float:
        """下一次健康檢查的間隔：寬限期 2 秒、低於預期 30 秒、達到預期 60 秒。"""
        snap = self.snapshot()
        started = self._started_at if self._started_at is not None else self._clock()
        if self._clock() - started < BOOT_GRACE_SECONDS:
            return GRACE_INTERVAL_SECONDS
        if level_rank(snap.level) < level_rank(snap.expected):
            return BELOW_EXPECTED_INTERVAL_SECONDS
        return AT_EXPECTED_RECHECK_SECONDS

    async def run_due_checks(self) -> None:
        """背景一輪：到期的健康檢查；本機 Landlock 不可用或網路設定不安全時每 10 分鐘重做自測。"""
        if self._health_stale():
            await self.refresh()
        local = self._local
        initial_done = self._initial is not None and self._initial.done()
        retry_due = self._clock() - self._last_selftest >= SELFTEST_RETRY_SECONDS
        if (local.status != "available" or local.net) and initial_done and retry_due:
            await self.run_selftest("data" if self._needs_data_selftest() else "state")

    def _needs_data_selftest(self) -> bool:
        """9p 失敗過、或還沒驗證過（例如開機自測丟出例外）：只認資料所在檔案系統的自測."""
        reason = self._local.reason
        return reason == "fs_9p" or (not self._data_verified and reason not in _KERNEL_VERDICTS)

    async def _background_loop(self) -> None:
        """一輪出錯只記 log、下一輪照常（取消照常往外傳）；出錯後照「低於預期」的間隔。"""
        interval = GRACE_INTERVAL_SECONDS
        while True:
            await self._sleep(interval)
            try:
                await self.run_due_checks()
                interval = self.next_interval()
            except Exception:
                logger.exception("Isolation re-probe round failed; retrying next round")
                interval = BELOW_EXPECTED_INTERVAL_SECONDS

    def stop(self) -> None:
        """停止背景重探（關機或測試）。"""
        if self._background is not None:
            self._background.cancel()

    def add_upstream_source(self, source: Callable[[], Iterable[str]]) -> None:
        """多接一個記憶體裡的上游 URL 來源（main 接模型路由，避開循環匯入）；每次組 B 都重讀."""
        self._upstream_sources.append(source)

    def on_level_change(self, callback: Callable[[IsolationSnapshot], Awaitable[None]]) -> None:
        """層級改變、本機 Landlock 變成可用、本機原因改變、或執行器重啟過（boot_id 變了）時呼叫.

        第一次在層級不是 none、或本機自測已有結果時通知（C2-3：等本機判定的第三方 MCP 靠它啟動）；
        本機原因（``local_issue``）改變也算：執行器部署下 ST 恢復時層級不會變（MEDIUM-R3-2）。
        """
        self._listeners.append(callback)

    def _notify(self, snap: IsolationSnapshot) -> None:
        previous, self._last_seen = self._last_seen, snap
        restarted, self._boot_changed = self._boot_changed, False
        if previous is None:
            changed = snap.level != "none" or snap.local.status != "pending"
        else:
            became_available = (
                previous.local.status != "available" and snap.local.status == "available"
            )
            local_changed = snap.local_issue != previous.local_issue  # 含 pending → 任何結果
            changed = snap.level != previous.level or became_available or restarted or local_changed
        if not changed:
            return
        logger.warning("Isolation level is %s (%s)", snap.level, snap.reason)
        for callback in self._listeners:
            spawn_logged(callback(snap), "isolation-level-change")


def _clear_stale_selftests(base: Path) -> None:
    """清掉上次被打斷留下的自測資料夾：只看 base 底下本前綴、不跟隨連結；清不掉就留著。"""
    root = base.resolve()
    for stale in base.glob(f"{SELFTEST_DIR_PREFIX}*"):
        if stale.is_symlink() or stale.is_junction() or not stale.resolve().is_relative_to(root):
            continue
        with contextlib.suppress(OSError):
            resource_manager.remove_dir(stale)


def _real_dirs(parent: Path) -> list[Path]:
    """``parent`` 底下真的資料夾（lstat：連結不算、不跟過去）；列不出來回空的."""
    try:
        entries = list(parent.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        try:
            if stat.S_ISDIR(os.lstat(entry).st_mode):
                found.append(entry)
        except OSError:
            continue
    return found


def _rename_proxy_tmp(state_root: Path | None) -> list[Path]:
    """``ST/proxy/<kind>/<owner>/tmp`` 原子改名成同一層的 ``tmp.trash-<隨機>``；回傳要刪的.

    只看這個形狀、不跟連結、不碰別的位置；上次沒刪完的 ``tmp.trash-*`` 一併刪。
    """
    proxy = state_root / "proxy" if state_root is not None else None
    if proxy is None or proxy not in _real_dirs(state_root):
        return []
    trash: list[Path] = []
    kinds = [d for d in _real_dirs(proxy) if d.name in STATE_KINDS]
    for owner in (o for kind in kinds for o in _real_dirs(kind)):
        for entry in _real_dirs(owner):
            if entry.name.startswith(TMP_TRASH_PREFIX):
                trash.append(entry)
            elif entry.name == "tmp":
                target = owner / f"{TMP_TRASH_PREFIX}{secrets.token_hex(8)}"
                with contextlib.suppress(OSError):
                    resource_manager.move_file(entry, target)
                    trash.append(target)
    return trash


def _remove_trash(paths: list[Path]) -> None:
    """背景刪除改名後的 tmp（rmtree 不跟連結）；刪不掉就留著，下次開機再試."""
    for path in paths:
        with contextlib.suppress(OSError):
            resource_manager.remove_dir(path)


def _repair_state_dir(repair: PermissionRepairHook, layout: SandboxLayout) -> None:
    """``ST/proxy`` 不可寫：先叫權限自癒修一次（根的保留額度）；出錯照實判 not_writable."""
    if layout.state_root is not None:
        with contextlib.suppress(Exception):
            repair.repair("state_issue", (str(layout.state_root / "proxy"),), retry=False)


def _state_issue(layout: SandboxLayout) -> str | None:
    """ST 未設定 → unset；建不出 proxy 子資料夾或不可寫 → not_writable（不拋例外）。"""
    if layout.state_root is None:
        return STATE_UNSET
    proxy_dir = layout.state_root / "proxy"
    try:
        resource_manager.make_dir(proxy_dir)
    except OSError:
        return "not_writable"
    return None if os.access(proxy_dir, os.W_OK | os.X_OK) else "not_writable"


isolation_monitor = IsolationMonitor()
