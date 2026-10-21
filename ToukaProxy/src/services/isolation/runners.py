"""SandboxRunner 合約（iso-c2）：runner 只改寫啟動參數，spawn 仍由呼叫端經 broker 執行.

``RunnerBroker.acquire`` 落實「降級要先經人核准」（C2-2 設計 §2.8）：
- 政策層級不是 none：只能用同一層級的 runner；層級掉了就拒絕，不靜默降級。政策是 executor 時
  這次還要在執行器範圍內（``ProfileSource.executor_issue``），否則拒絕、不退回 Landlock；
- 政策層級 none 且人核准過：在「種類不低於卡片（executor > landlock > direct）、features 包含
  卡片的」runner 裡取當下最強的；都不符合就拒絕；
- 政策層級 none 沒有同意：一律拒絕。
``acquire_maintenance`` 只給 Touka 固定的維護指令（``home_wipe``）：不看 grant，只用執行器、不降級。
profile 在選好位置之後才建（``ProfileSource``）：執行器位置的 HOME／TMP 在 READY 的 state_root。
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, Protocol

from src.sandbox_kernel.framing import MAX_START_BYTES
from src.sandbox_kernel.spec import (
    FAILURE_CODES,
    INTERPRETER_UNTRUSTED,
    MAX_SPEC_BYTES,
    RELAY_FAILURE_CODES,
    STATUS_MAX_BYTES,
    InterpreterUntrustedError,
    exec_env_reserved,
    exec_env_stripped,
    filter_executor_env,
    kernel_digest,
    parse_status,
    trusted_interpreter,
)
from src.services.isolation.executor_session import IO_MODES, MAX_SECONDS, MCP_KIND
from src.services.isolation.profiles import (
    EXECUTOR_KINDS,
    PYTEST_ADDOPTS,
    current_layout,
    launcher_spec_fields,
)
from src.services.isolation.status import LAUNCHER_NAME, isolation_monitor
from src.utils.isolation_policy import TCP_FEATURE

if sys.platform != "win32":
    import fcntl

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from src.services.isolation.consent import Consent, ExecutionGrant
    from src.services.isolation.executor_client import ExecutorReady
    from src.services.isolation.profiles import Location, SandboxLayout, SandboxProfile
    from src.services.isolation.status import (
        FallbackPlan,
        IsolationSnapshot,
        LevelDecision,
        LocalDecision,
    )
    from src.utils.isolation_policy import IsolationLevel

logger = logging.getLogger(__name__)

# Linux pipe 的預設容量；spec 比它大時先把 pipe 調大，寫入時不會卡住
PIPE_DEFAULT_CAPACITY = 64 * 1024
ISOLATION_UNAVAILABLE = "isolation_unavailable"
RELAY_NAME = "relay.py"
EXECUTOR_GRACE_SECONDS = 30  # 執行器端的 max_seconds＝本機逾時＋30 秒（本機先逾時、先收掉 relay）
EXECUTOR_KNOWN_CODES = FAILURE_CODES | RELAY_FAILURE_CODES
# 降級去處的強弱（卡片寫的是下限）
KIND_RANK = {"direct": 0, "landlock": 1, "executor": 2}


class IsolationUnavailableError(Exception):
    """拿不到可用的 runner；呼叫端回 ``upstream_error``（原因 ``isolation_unavailable``）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class IsolationPendingError(IsolationUnavailableError):
    """本機 Landlock 還在檢查、或只是暫時有問題：先不啟動，恢復時由 listener 啟動（MEDIUM-5）."""


@dataclass(frozen=True)
class ExecRequest:
    """要啟動的指令；cwd 一律是 profile 的 cwd。``preexec_fn`` 只有直接執行會用.

    ``io``：merged（stderr 併進 stdout）或 stdio（MCP 伺服器）；``max_seconds`` 給執行器（沒給時
    kind mcp 不限時，其他用上限）。``exec_env``：條目自己的環境變數，只放 spec（啟動器套完限制
    才交給目標，啟動器自己的環境沒有）；值不能寫進 log。
    """

    argv: tuple[str, ...]
    env: Mapping[str, str]
    profile: SandboxProfile
    io: Literal["merged", "stdio"] = "merged"
    max_seconds: int | None = None
    preexec_fn: Callable[[], None] | None = None
    exec_env: Mapping[str, str] = field(default_factory=dict, repr=False)


class StatusPipe:
    """狀態 pipe 的讀端（proxy 留著；寫端交給啟動器或 relay，只有它們寫得到）.

    ``read`` 只讀一次、讀完就關（之後一律回空的，不會讀到被重用的 fd 號碼）；不等待：寫端還開著
    （子行程還沒結束）就當作沒有代碼。
    """

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def read(self) -> bytes:
        """讀出代碼並關掉讀端。"""
        fd, self._fd = self._fd, None
        if fd is None:
            return b""
        try:
            os.set_blocking(fd, False)
            return os.read(fd, STATUS_MAX_BYTES + 1)
        except OSError:  # 寫端還開著（BlockingIOError）或讀不到：當作沒有代碼
            return b""
        finally:
            os.close(fd)


def status_pipe() -> tuple[StatusPipe, int]:
    """開一條狀態 pipe：回（proxy 留著的讀端、交給子行程 ``--status-fd`` 的寫端）。"""
    read_fd, write_fd = os.pipe()
    return StatusPipe(read_fd), write_fd


@dataclass(frozen=True)
class LaunchPlan:
    """呼叫端交給 ``spawn_exec``／``run_sync`` 的參數。"""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    preexec_fn: Callable[[], None] | None
    pass_fds: tuple[int, ...]
    level: IsolationLevel
    features: frozenset[str]
    owned_fds: tuple[int, ...] = ()
    status: StatusPipe | None = None

    def release(self) -> None:
        """在 spawn 之後（成功或失敗）關掉 runner 為這次交給子行程的 fd；可重複呼叫。"""
        for fd in self.owned_fds:
            with contextlib.suppress(OSError):
                os.close(fd)

    def read_status(self) -> bytes:
        """跑完（含逾時、取消、spawn 失敗）讀狀態 fd 並關掉讀端；沒有狀態 fd 或讀過了回空的。"""
        return self.status.read() if self.status is not None else b""


@dataclass(frozen=True)
class SandboxFailure:
    """啟動器回報的失敗（只會觸發重探，不直接改層級）。"""

    code: str


class SandboxRunner(Protocol):
    """三種 runner 共同的合約（DIP）。"""

    level: IsolationLevel

    def plan(self, request: ExecRequest) -> LaunchPlan:
        """改寫啟動參數。"""
        ...

    def failure(self, status: bytes) -> SandboxFailure | None:
        """辨認狀態 fd 裡的失敗代碼（``LaunchPlan.read_status`` 的內容）。"""
        ...


class NetRulesetProvider(Protocol):
    """共用網路規則集（依封鎖埠與 ABI 建一次）；回 None 時由啟動器自己建。

    實作必須用 ``landlock.build_net_ruleset`` 建，bind 與 connect 一起擋，和啟動器自建的相同。
    """

    def __call__(self, block_ports: tuple[int, ...], features: frozenset[str]) -> int | None:
        """回傳可交給啟動器的規則集 fd。"""
        ...


class MonitorView(Protocol):
    """runner 需要的 monitor 介面。"""

    @property
    def checked_kernel_digest(self) -> str | None:
        """最近一次健康檢查看到的 kernel 雜湊。"""
        ...

    def snapshot(self) -> IsolationSnapshot:
        """目前判定。"""
        ...

    def fallback_for(
        self, *, profile_complete: bool, executor_eligible: bool = False
    ) -> FallbackPlan:
        """降級去處（``executor_eligible``：這次在執行器範圍內）。"""
        ...

    def report_failure(self, code: str) -> None:
        """請求重探。"""
        ...

    def local_decision(self) -> LocalDecision:
        """本機判定（第三方 MCP 與 scaffold；不看層級）。"""
        ...

    def local_level(self) -> LevelDecision:
        """本機層級（scaffold 的試跑）。"""
        ...


class ProfileFactory(Protocol):
    """spawn 前才依位置建 profile：``acquire`` 選好位置才呼叫（執行器位置不在 proxy 建資料夾）。"""

    async def __call__(self, location: Location, executor_state: str | None) -> SandboxProfile:
        """``executor_state`` 是執行器 READY 的 state_root（proxy 位置是 None）。"""
        ...


@dataclass(frozen=True)
class FixedProfile:
    """位置無關的固定 profile（沒有隔離脈絡的指令）；只能在 proxy 位置用。"""

    profile: SandboxProfile

    async def __call__(self, location: Location, _executor_state: str | None) -> SandboxProfile:
        """執行器位置一律拒絕（沒有執行器的 profile）。"""
        if location != "proxy":
            raise IsolationUnavailableError(ISOLATION_UNAVAILABLE)
        return self.profile


@dataclass(frozen=True)
class ProfileSource:
    """``acquire`` 的 profile 來源.

    ``executor_issue``：這次不在執行器範圍的原因（None：在範圍內）。
    """

    build: ProfileFactory
    executor_issue: str | None

    @classmethod
    def fixed(cls, profile: SandboxProfile) -> ProfileSource:
        """固定 profile、不在執行器範圍。"""
        return cls(FixedProfile(profile), executor_issue=ISOLATION_UNAVAILABLE)


@dataclass(frozen=True)
class Acquired:
    """選好的 runner 與它那個位置的 profile。"""

    runner: SandboxRunner
    profile: SandboxProfile


@dataclass(frozen=True)
class ThirdPartyAcquired:
    """``acquire_third_party`` 的結果：包住（``acquired``），或照今天的方式未隔離（``reason``）."""

    acquired: Acquired | None
    reason: str | None = None


def checked_exec_env(exec_env: Mapping[str, str], *, label: str) -> dict[str, str]:
    """``exec_env`` 先濾掉 profile 管的名稱與禁令 19 的變數（kernel 的拒收、移除是第二道）.

    濾掉的記一次警告（``label`` 與變數名）；值絕不寫進 log。
    """
    dropped = sorted(n for n in exec_env if exec_env_reserved(n) or exec_env_stripped(n))
    if dropped:
        logger.warning("Ignored environment names for %s: %s", label, ", ".join(dropped))
    return {name: value for name, value in exec_env.items() if name not in dropped}


def spec_document(
    profile: SandboxProfile, *, net_strict: bool, exec_env: Mapping[str, str] | None = None
) -> dict:
    """啟動器 spec 的欄位（``launcher_spec_fields`` 加上 spawn 端的 pid 與 ``net_strict``）.

    ``exec_env`` 濾過之後有東西才帶這個鍵：``/opt`` 的舊啟動器不認得它（代理人指令不受影響）。
    """
    fields = {**launcher_spec_fields(profile), "parent_pid": os.getpid()}  # spawn 端就是本行程
    fields["net_strict"] = net_strict
    kept = checked_exec_env(exec_env or {}, label=profile.name)
    if kept:
        fields["exec_env"] = kept
    return fields


def launcher_spec(
    profile: SandboxProfile, *, net_strict: bool = True, exec_env: Mapping[str, str] | None = None
) -> str:
    """啟動器 spec 的 JSON 文字；欄位來自 ``spec_document``。

    ``net_strict`` 等於 runner 的 features 有沒有 tcp：有 tcp 時網路設定不安全就不執行；人核准、
    features 不含 tcp 的降級照常執行（卡片已照實標示埠擋不住）。
    """
    fields = spec_document(profile, net_strict=net_strict, exec_env=exec_env)
    text = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise IsolationUnavailableError("spec_too_large")
    return text


def executor_max_seconds(local_timeout: float | None) -> int:
    """執行器 START 的 ``max_seconds``：本機逾時＋30 秒，1～900；沒有本機逾時就用上限."""
    if local_timeout is None:
        return MAX_SECONDS
    return max(1, min(MAX_SECONDS, math.ceil(local_timeout) + EXECUTOR_GRACE_SECONDS))


def _start_seconds(kind: str, requested: int | None) -> int:
    """START 的 ``max_seconds``：有給就夾在 1～900；沒給時 kind ``mcp`` 是 0，其他用上限.

    0 是常駐 MCP 伺服器的「沒有時限」，伺服器只准它配 stdio。
    """
    if not requested:
        return 0 if kind == MCP_KIND else MAX_SECONDS
    return max(1, min(MAX_SECONDS, requested))


def executor_env(env: Mapping[str, str], profile: SandboxProfile) -> dict[str, str]:
    """START 的環境：先過允許清單（繼承來的 ``GIT_CONFIG_*`` 一律拿掉），再套 profile 的那組."""
    result = filter_executor_env(env)
    result.update(profile.env_set)
    inherited = env.get(PYTEST_ADDOPTS)
    if inherited and PYTEST_ADDOPTS in profile.env_set:
        result[PYTEST_ADDOPTS] = f"{profile.env_set[PYTEST_ADDOPTS]} {inherited}"
    return result


def spec_pipe(spec: bytes) -> int:
    """把 spec 寫進 pipe 並關掉寫端，回傳讀端給啟動器 ``--spec-fd``；寫不下就拒絕，不會卡住。"""
    read_fd, write_fd = os.pipe()
    try:
        if sys.platform != "win32" and len(spec) > PIPE_DEFAULT_CAPACITY:
            fcntl.fcntl(write_fd, fcntl.F_SETPIPE_SZ, len(spec))
        os.set_blocking(write_fd, False)
        view = memoryview(spec)
        while view:
            view = view[os.write(write_fd, view) :]
    except OSError as exc:
        os.close(read_fd)
        raise IsolationUnavailableError("spec_too_large") from exc
    finally:
        os.close(write_fd)
    return read_fd


class DirectRunner:
    """未隔離：只在卡片寫「未隔離」而且人核准後使用。"""

    level: IsolationLevel = "none"

    def plan(self, request: ExecRequest) -> LaunchPlan:
        """原樣啟動（保留呼叫端的 rlimit preexec）；沒有啟動器，``exec_env`` 直接併進環境."""
        return LaunchPlan(
            argv=tuple(request.argv),
            cwd=request.profile.cwd,
            env={**request.env, **request.exec_env},
            preexec_fn=request.preexec_fn,
            pass_fds=(),
            level="none",
            features=frozenset(),
        )

    def failure(self, _status: bytes) -> SandboxFailure | None:
        """直接執行沒有啟動器，也沒有狀態 fd。"""
        return None


class LandlockRunner:
    """proxy 內的 Landlock：``python -I -S <kernel>/launcher.py --spec-fd N … -- argv``。"""

    level: IsolationLevel = "landlock"

    def __init__(
        self,
        kernel_dir: Path,
        features: frozenset[str],
        net_rulesets: NetRulesetProvider | None = None,
        *,
        python: str,
    ) -> None:
        self._launcher = kernel_dir / LAUNCHER_NAME
        self._features = features
        self._net_rulesets = net_rulesets
        self._python = python  # 可信直譯器（``trusted_python``）：不用 venv 的 sys.executable

    @property
    def features(self) -> frozenset[str]:
        """這個 runner 生效的 features（MCP 的隔離狀態照它標示）."""
        return self._features

    def plan(self, request: ExecRequest) -> LaunchPlan:
        """前置啟動器；spec 經繼承的 pipe 傳（不放 argv），失敗代碼走另一條狀態 pipe.

        環境變數改寫是最後一步。
        """
        profile = request.profile
        fd = self._net_rulesets(profile.block_ports, self._features) if self._net_rulesets else None
        strict = TCP_FEATURE in self._features
        spec = launcher_spec(profile, net_strict=strict, exec_env=request.exec_env)
        spec_fd = spec_pipe(spec.encode("utf-8"))
        status, status_fd = status_pipe()
        argv = [self._python, "-I", "-S", str(self._launcher), "--spec-fd", str(spec_fd)]
        argv += ["--status-fd", str(status_fd)]
        if fd is not None:
            argv += ["--net-ruleset-fd", str(fd)]
        env = {k: v for k, v in request.env.items() if k not in profile.env_unset}
        env.update(profile.env_set)
        inherited = request.env.get(PYTEST_ADDOPTS)
        if inherited and PYTEST_ADDOPTS in profile.env_set:
            # profile 的選項在前、原本的接在後面（指令列的同名選項仍最後生效）
            env[PYTEST_ADDOPTS] = f"{profile.env_set[PYTEST_ADDOPTS]} {inherited}"
        return LaunchPlan(
            argv=(*argv, "--", *request.argv),
            cwd=profile.cwd,
            env=env,
            preexec_fn=None,
            pass_fds=(spec_fd, status_fd, *((fd,) if fd is not None else ())),
            level="landlock",
            features=self._features,
            owned_fds=(spec_fd, status_fd),
            status=status,
        )

    def failure(self, status: bytes) -> SandboxFailure | None:
        """只認狀態 fd 裡恰好一個 ``FAILURE_CODES`` 的代碼；指令輸出裡的標記不看（C2-2 §2.9）."""
        code = parse_status(status, FAILURE_CODES)
        return SandboxFailure(code) if code is not None else None


class ExecutorRunner:
    """執行器：本機 relay；指令、cwd、env 與 spec 都在請求的 START 裡（經 pipe 傳）.

    argv：``python -I -S <kernel>/relay.py --ipc-dir D --request-fd R --status-fd S --boot-id B``
    （沒有權杖與指令）。relay 是本機子行程，所以 killpg、逾時、背景停止沿用 C2-1；relay 斷線時
    執行器收掉遠端。
    """

    level: IsolationLevel = "executor"

    def __init__(
        self,
        kernel_dir: Path,
        ipc_dir: Path,
        boot_id: str,
        features: frozenset[str],
        *,
        python: str,
    ) -> None:
        self._relay = kernel_dir / RELAY_NAME
        self._ipc_dir = ipc_dir
        self._boot_id = boot_id  # 判定時的 READY；之後執行器重啟過，relay 就不送 START
        self._features = features
        self._python = python

    @property
    def boot_id(self) -> str:
        """判定時的 READY boot_id（常駐的 MCP 伺服器要比對執行器有沒有重啟過）."""
        return self._boot_id

    @property
    def features(self) -> frozenset[str]:
        """這個 runner 生效的 features（MCP 的隔離狀態照它標示）."""
        return self._features

    def plan(self, request: ExecRequest) -> LaunchPlan:
        """組 START 與請求 ``{parent_pid, start}``；relay 自己不帶環境（START 的 env 才給指令）."""
        profile = request.profile
        kind = EXECUTOR_KINDS.get(profile.name)
        if kind is None or profile.owner is None or request.io not in IO_MODES:
            raise IsolationUnavailableError(ISOLATION_UNAVAILABLE)
        start = {
            "argv": list(request.argv),
            "cwd": str(profile.cwd),
            "env": executor_env(request.env, profile),
            "spec": spec_document(
                profile, net_strict=TCP_FEATURE in self._features, exec_env=request.exec_env
            ),
            "kind": kind,
            "owner": profile.owner,
            "io": request.io,
            "max_seconds": _start_seconds(kind, request.max_seconds),
        }
        text = json.dumps(start, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_START_BYTES:
            raise IsolationUnavailableError("spec_too_large")
        body = json.dumps({"parent_pid": os.getpid(), "start": start}, ensure_ascii=False)
        request_fd = spec_pipe(body.encode("utf-8"))
        status, status_fd = status_pipe()
        argv = (
            self._python, "-I", "-S", str(self._relay), "--ipc-dir", str(self._ipc_dir),
            "--request-fd", str(request_fd), "--status-fd", str(status_fd),
            "--boot-id", self._boot_id,
        )
        # kind mcp 的 home 只在執行器容器的 state（proxy 沒掛）：relay 在本機從唯讀的
        # kernel 資料夾跑（依路徑載入、不用 cwd）；伺服器的 cwd 照 START，執行器強制是 home
        return LaunchPlan(
            argv=argv,
            cwd=self._relay.parent if kind == MCP_KIND else profile.cwd,
            env={},
            preexec_fn=None,
            pass_fds=(request_fd, status_fd),
            level="executor",
            features=self._features,
            owned_fds=(request_fd, status_fd),
            status=status,
        )

    def failure(self, status: bytes) -> SandboxFailure | None:
        """狀態 fd 只認 relay 的代碼與遠端啟動器的原代碼（``EXECUTOR_KNOWN_CODES``）."""
        code = parse_status(status, EXECUTOR_KNOWN_CODES)
        return SandboxFailure(code) if code is not None else None


def trusted_python(layout: SandboxLayout) -> str:
    """Runner 前置啟動器用的可信直譯器（容器裡整條擁有者鏈要 root）；不合格就拒絕、不換別的."""
    try:
        return trusted_interpreter(require_root_owned=layout.in_container)
    except InterpreterUntrustedError as exc:
        raise IsolationUnavailableError(INTERPRETER_UNTRUSTED) from exc


class RunnerBroker:
    """依 grant 與目前判定選 runner；一定在 spawn 前才呼叫。"""

    def __init__(
        self,
        monitor: MonitorView = isolation_monitor,
        layout_provider: Callable[[], SandboxLayout] = current_layout,
        net_rulesets: NetRulesetProvider | None = None,
        interpreter: Callable[[SandboxLayout], str] = trusted_python,
    ) -> None:
        self._monitor = monitor
        self._layout_provider = layout_provider
        self._net_rulesets = net_rulesets
        self._interpreter = interpreter

    async def acquire(self, grant: ExecutionGrant, source: ProfileSource) -> Acquired:
        """規則見模組說明；拿不到就拋 ``IsolationUnavailableError``。"""
        if grant.consent is not None:
            return await self._consented(grant.consent, source)
        snap = self._monitor.snapshot()
        if grant.policy_level == "none" or snap.level != grant.policy_level:
            raise IsolationUnavailableError(snap.reason or ISOLATION_UNAVAILABLE)
        if snap.level == "executor":
            # 不在執行器範圍、或 READY 不見了：拒絕，不退回 Landlock
            if source.executor_issue is not None or snap.executor is None:
                raise IsolationUnavailableError(source.executor_issue or ISOLATION_UNAVAILABLE)
            return await self._executor(snap.executor, snap.features, source)
        profile = await source.build("proxy", None)
        if profile.ungranted:
            raise IsolationUnavailableError(snap.reason or ISOLATION_UNAVAILABLE)
        return Acquired(self._landlock(snap.local.features), profile)

    async def _consented(self, consent: Consent, source: ProfileSource) -> Acquired:
        """人核准的降級：種類不低於卡片、features 包含卡片的，在符合的裡面取當下最強的."""
        floor, needed = KIND_RANK[consent.fallback], consent.features
        if source.executor_issue is None:
            plan = self._monitor.fallback_for(profile_complete=True, executor_eligible=True)
            ready = self._monitor.snapshot().executor
            if plan.kind == "executor" and ready is not None and needed <= plan.features:
                return await self._executor(ready, plan.features, source)
        if floor > KIND_RANK["landlock"]:
            raise IsolationUnavailableError(ISOLATION_UNAVAILABLE)
        profile = await source.build("proxy", None)
        plan = self._monitor.fallback_for(profile_complete=not profile.ungranted)
        if plan.kind == "landlock" and needed <= plan.features:
            return Acquired(self._landlock(plan.features), profile)
        if floor == KIND_RANK["direct"]:
            return Acquired(DirectRunner(), profile)
        raise IsolationUnavailableError(ISOLATION_UNAVAILABLE)

    async def acquire_maintenance(self, source: ProfileSource) -> Acquired:
        """系統維護（刪除靈魂前清它在執行器裡的 home／tmp，``home_wipe``）：不看 grant、不降級.

        固定指令、不是代理人的程式，所以不需要同意；只用可用的執行器（可能只差網路設定），
        不能用就拒絕、不退回 Landlock 或直接執行。
        """
        eligible = source.executor_issue is None
        plan = self._monitor.fallback_for(profile_complete=True, executor_eligible=eligible)
        ready = self._monitor.snapshot().executor
        if plan.kind != "executor" or ready is None:
            raise IsolationUnavailableError(source.executor_issue or ISOLATION_UNAVAILABLE)
        return await self._executor(ready, plan.features, source)

    async def acquire_local(self, grant: ExecutionGrant, source: ProfileSource) -> Acquired:
        """``scaffold_mcp`` 的試跑：只用本機 Landlock（政策是本機層級），不退回執行器.

        沒有同意時政策必須是 landlock、本機層級此刻也必須是 landlock；有同意時去處只有本機
        Landlock 或未隔離（執行器範圍固定不在）。
        """
        local = replace(source, executor_issue=ISOLATION_UNAVAILABLE)
        if grant.consent is not None:
            return await self._consented(grant.consent, local)
        decision = self._monitor.local_level()
        if grant.policy_level != "landlock" or decision.level != "landlock":
            raise IsolationUnavailableError(decision.reason or ISOLATION_UNAVAILABLE)
        profile = await local.build("proxy", None)
        if profile.ungranted:
            raise IsolationUnavailableError(ISOLATION_UNAVAILABLE)
        return Acquired(self._landlock(self._monitor.local_decision().features), profile)

    async def acquire_third_party(self, source: ProfileSource) -> ThirdPartyAcquired:
        """第三方與 scaffold 的正式啟動（不需同意，決定 5 甲）：只看本機判定，不看層級.

        可用 → 本機 Landlock（features 照本機；沒授權到的路徑照樣啟動）；還在檢查或暫時性原因 →
        ``IsolationPendingError``（不先未隔離啟動）；穩定原因 → 照今天的方式未隔離（不建 profile、
        不碰 ST，MEDIUM-7）。
        """
        decision = self._monitor.local_decision()
        if decision.status == "available":
            profile = await source.build("proxy", None)
            return ThirdPartyAcquired(Acquired(self._landlock(decision.features), profile))
        if not decision.stable:
            raise IsolationPendingError(decision.reason or ISOLATION_UNAVAILABLE)
        return ThirdPartyAcquired(None, decision.reason)

    async def _executor(
        self, ready: ExecutorReady, features: frozenset[str], source: ProfileSource
    ) -> Acquired:
        layout = self._layout_provider()
        kernel = self._kernel_dir(layout)
        if layout.executor_dir is None:
            raise IsolationUnavailableError(ISOLATION_UNAVAILABLE)
        profile = await source.build("executor", ready.state_root)
        runner = ExecutorRunner(
            kernel, layout.executor_dir, ready.boot_id, features, python=self._interpreter(layout)
        )
        return Acquired(runner, profile)

    def _landlock(self, features: frozenset[str]) -> LandlockRunner:
        layout = self._layout_provider()
        kernel = self._kernel_dir(layout)
        python = self._interpreter(layout)
        return LandlockRunner(kernel, features, self._net_rulesets, python=python)

    def _kernel_dir(self, layout: SandboxLayout) -> Path:
        """啟動器與 relay 所在的 kernel 目錄；原生時要和最近一次檢查的雜湊相同."""
        if layout.kernel_dir is None:
            raise IsolationUnavailableError("kernel_not_installed")
        if not layout.in_container and (
            kernel_digest(layout.kernel_dir) != self._monitor.checked_kernel_digest
        ):
            # 原生（沒有唯讀 /opt）時 kernel 就是 live src：檢查之後被改過就重探、這次拒絕
            self._monitor.report_failure("kernel_changed")
            raise IsolationUnavailableError("kernel_outdated")
        return layout.kernel_dir


runner_broker = RunnerBroker()
