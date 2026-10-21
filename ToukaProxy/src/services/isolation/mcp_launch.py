"""MCP 伺服器怎麼啟動（iso-c2 C2-3 §2.1、§2.4、§2.7；判定的唯一來源）.

每台 stdio 伺服器連線前由這裡決定：在執行器裡、在主容器內以本機 Landlock 包住、照今天的方式
未隔離、或先不啟動（等隔離）。
- 分類：``utils.mcp_origin.classify``；紀錄由呼叫端注入的 reader 讀（隔離設定檔，``bound`` 在
  那一層核對）；與部署凍結共用 Proxy cwd、自建與第三方隔離 profile 的 state home。
- 自建：層級 executor／landlock → ``RunnerBroker.acquire``；層級 none 不自動啟動
  （``waiting_isolation``）；人在設定頁確認降級去處後帶同意啟動一次（``McpManualStart``）。
- 第三方與 scaffold：``RunnerBroker.acquire_third_party``，看本機判定、不看層級；還在檢查或
  暫時有問題時等（``waiting_probe``），穩定原因才照今天的方式未隔離啟動（決定 5 甲）。
- 環境：條目自己的 env 一律放 ``exec_env``（先濾掉 profile 管的名稱；值不進 log、``repr``、
  帳本或 API）；啟動器自己的環境由 protocols 層組好傳進來（隔離邊界不碰父行程的完整環境）。
判定結果記在 ``mcp_launch_book``（只有代碼），給 ``/mcp/list``、``/isolation`` 與重連用。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from src.services.isolation.agent_command import profile_unavailable, unbound_or_refuse
from src.services.isolation.consent import ExecutionGrant, consent_subject
from src.services.isolation.mcp_profiles import (
    build_mcp_self_profile,
    build_mcp_third_party_profile,
    mcp_classification_bases,
)
from src.services.isolation.profiles import ProfileError, current_layout
from src.services.isolation.runners import (
    ISOLATION_UNAVAILABLE,
    DirectRunner,
    ExecRequest,
    ExecutorRunner,
    IsolationUnavailableError,
    LandlockRunner,
    ProfileSource,
    checked_exec_env,
    runner_broker,
)
from src.services.isolation.status import PROBE_PENDING, isolation_monitor
from src.services.mcp_isolation_store import bound_of
from src.utils.isolation_policy import MCP_SCAFFOLD_ORIGIN
from src.utils.mcp_origin import (
    NO_RECORD,
    UNREADABLE_RECORD,
    OriginRecord,
    classify,
    proxy_base_dir,
)
from src.utils.mcp_sentinels import resolve_args_sentinels, resolve_env_sentinels
from src.utils.time_utils import get_monotonic_ms

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from src.models.mcp_isolation import McpRecordState, SandboxPathGrant
    from src.services.isolation.consent import Consent
    from src.services.isolation.mcp_profiles import McpThirdPartyLaunch
    from src.services.isolation.profiles import Location, SandboxLayout, SandboxProfile
    from src.services.isolation.runners import Acquired, RunnerBroker, SandboxRunner
    from src.services.isolation.status import (
        FallbackPlan,
        IsolationSnapshot,
        LocalDecision,
    )
    from src.services.mcp_isolation_store import RecordView
    from src.utils.isolation_policy import IsolationLevel, McpClass, McpOrigin
    from src.utils.mcp_origin import McpClassification, McpEntry

logger = logging.getLogger(__name__)

McpLaunchState = Literal["launched", "waiting_isolation", "waiting_probe"]
WAITING_ISOLATION: McpLaunchState = "waiting_isolation"  # 自建：層級 none，等隔離或人同意
WAITING_PROBE: McpLaunchState = "waiting_probe"  # 第三方：本機還在檢查或暫時有問題
TOOL_BASE_URL = "TOOL_BASE_URL"  # 正式啟動時 Touka 給伺服器的工具網址（同今天）
MANUAL_START_ACTION = "mcp_manual_start"  # 手動啟動同意的 subject 種類
THIRD_PARTY_CLASSES: frozenset[str] = frozenset({"third_party", "scaffold"})
MCP_SUSPEND_NOTICE_SECONDS = 660


class McpLaunchWaiting(Exception):
    """這台先不啟動（等隔離）：呼叫端不連線、不進重試，listener 之後重新判定."""

    def __init__(self, state: McpLaunchState, reason: str | None) -> None:
        super().__init__(state)
        self.state = state
        self.reason = reason


@dataclass(frozen=True)
class McpLaunchInfo:
    """一台伺服器最近一次的啟動判定；只有代碼（沒有路徑、參數或 env）."""

    mcp_class: McpClass
    origin: McpOrigin | None
    record_state: McpRecordState
    state: McpLaunchState
    level: IsolationLevel
    features: frozenset[str] = frozenset()
    reason: str | None = None
    boot_id: str | None = None  # 在執行器裡跑：判定當下 READY 的 boot_id（重連比對用，不對外）
    # 人同意、以未隔離啟動的（只記這個事實，不留同意本身；重連比對用，不對外）
    consented_direct: bool = False

    def public(self, *, connected: bool, suspended: bool = False) -> dict[str, object]:
        """``/mcp/list`` 每台的 ``isolation``；啟動了的依連線分成 running／not_started."""
        state = ("running" if connected else "not_started") if self.state == "launched" else (
            self.state
        )
        return {
            "class": self.mcp_class,
            "origin": self.origin,
            "record_state": self.record_state,
            "state": "suspended" if suspended else state,
            "level": self.level,
            "features": sorted(self.features),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class McpLaunch:
    """一次判定的結果.

    ``acquired`` 是 None：照今天的方式啟動（SDK 的 ``stdio_client``、今天的環境與 cwd，不用 ST）
    ——穩定原因的第三方、人同意的未隔離自建、遠端。否則以 ``request`` 交給 runner 的 ``plan``。
    """

    info: McpLaunchInfo
    acquired: Acquired | None = None
    argv: tuple[str, ...] = ()
    exec_env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def request(self, launcher_env: Mapping[str, str]) -> ExecRequest:
        """包住時給 ``runner.plan`` 的請求；``launcher_env`` 是 protocols 層組好的啟動器環境."""
        if self.acquired is None:
            raise IsolationUnavailableError(ISOLATION_UNAVAILABLE)
        return ExecRequest(
            argv=self.argv,
            env=launcher_env,
            profile=self.acquired.profile,
            io="stdio",
            exec_env=self.exec_env,
        )


@dataclass(frozen=True)
class McpLaunchCounts:
    """``/isolation`` 的 MCP 計數（只有台數）."""

    unisolated: int  # 照今天的方式未隔離啟動的第三方與 scaffold
    waiting: int
    suspended: int


class McpLaunchBook:
    """每台 stdio 伺服器最近一次的判定（模組單例 ``mcp_launch_book``；只在事件迴圈上用）."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._entries: dict[str, McpLaunchInfo] = {}
        self._waiting_since: dict[str, float] = {}
        self._clock = clock or (lambda: get_monotonic_ms() / 1000)

    def record(self, name: str, info: McpLaunchInfo) -> None:
        """記下這台最新的判定."""
        self._entries[name] = info
        if info.state == WAITING_PROBE and info.reason != PROBE_PENDING:
            self._waiting_since.setdefault(name, self._clock())
        else:
            self._waiting_since.pop(name, None)

    def get(self, name: str) -> McpLaunchInfo | None:
        """這台最近一次的判定；沒判定過是 None."""
        return self._entries.get(name)

    def retain(self, names: Iterable[str]) -> None:
        """只留下目前設定裡還有的名稱（重載設定後呼叫）."""
        keep = set(names)
        self._entries = {name: info for name, info in self._entries.items() if name in keep}
        self._waiting_since = {
            name: since for name, since in self._waiting_since.items() if name in keep
        }

    def suspended(self, name: str) -> bool:
        """暫時性故障等滿門檻才顯示暫停；探測尚未開始不計時。"""
        since = self._waiting_since.get(name)
        return since is not None and self._clock() - since >= MCP_SUSPEND_NOTICE_SECONDS

    def public(
        self, name: str, *, connected: bool, manual_start: dict[str, object] | None = None
    ) -> dict[str, object] | None:
        """公開狀態與同一次判定的手動啟動資訊；沒有判定就沒有隔離欄位。"""
        info = self.get(name)
        if info is None:
            return None
        result = info.public(connected=connected, suspended=self.suspended(name))
        if manual_start is not None:
            result["manual_start"] = manual_start
        return result

    def counts(self) -> McpLaunchCounts:
        """未隔離的第三方與 scaffold、等隔離的台數."""
        infos = list(self._entries.values())
        unisolated = sum(
            1
            for info in infos
            if info.state == "launched" and info.level == "none"
            and info.mcp_class in THIRD_PARTY_CLASSES
        )
        suspended = sum(self.suspended(name) for name in self._entries)
        waiting = sum(1 for info in infos if info.state != "launched") - suspended
        return McpLaunchCounts(unisolated, waiting, suspended)

    def relaunch_targets(self, snap: IsolationSnapshot, local: LocalDecision) -> tuple[str, ...]:
        """隔離狀態改變時要重新判定、重連的伺服器（§2.7）.

        等隔離的；照今天的方式未隔離啟動、而本機 Landlock 現在可用的第三方與 scaffold；在執行器
        裡跑、而執行器重啟過（READY 的 boot_id 和判定時不同）的；人同意以未隔離啟動、而現在層級
        不是 none 的（重連不帶同意，重新判定就會走隔離）。
        """
        ready = snap.executor

        def due(info: McpLaunchInfo) -> bool:
            if info.consented_direct:
                return snap.level != "none"
            if info.state != "launched":
                return True
            if info.level == "none" and info.mcp_class in THIRD_PARTY_CLASSES:
                return local.status == "available"
            return info.level == "executor" and ready is not None and ready.boot_id != info.boot_id

        return tuple(name for name, info in self._entries.items() if due(info))


mcp_launch_book = McpLaunchBook()


class IsolationRecordReader(Protocol):
    """讀一台伺服器的隔離紀錄（``McpIsolationStore`` 實作；``bound`` 在那一層核對）."""

    def record_for(self, name: str, entry: Mapping[str, Any]) -> RecordView:
        """對照目前條目（command、args、url）的紀錄."""
        ...


class McpLaunchMonitor(Protocol):
    """判定需要的 monitor 介面."""

    def snapshot(self) -> IsolationSnapshot:
        """目前判定."""
        ...

    def block_ports(self, location: Location) -> tuple[int, ...]:
        """這個位置要擋的埠."""
        ...

    def fallback_for(
        self, *, profile_complete: bool, executor_eligible: bool = False
    ) -> FallbackPlan:
        """層級 none 經人核准後的去處."""
        ...


def entry_identity(entry: McpEntry) -> dict[str, Any]:
    """條目的程式身分（``bound`` 的輸入：command、args、url；不含 env）."""
    return {"command": entry.command, "args": list(entry.args), "url": entry.url}


def origin_record(origin: str | None, *, records_unreadable: bool) -> OriginRecord:
    """紀錄交給 ``classify`` 的形狀（路由的分類也用這一份）.

    讀不到全部當自建；沒有可用紀錄（``effective_origin`` 是 None）是未標示。
    """
    if records_unreadable:
        return UNREADABLE_RECORD
    return NO_RECORD if origin is None else OriginRecord("present", origin)


def manual_start_subject(name: str, generation: int, bound: str) -> str:
    """手動啟動同意的 subject：綁伺服器名、installation generation 與條目的 ``bound``."""
    return consent_subject(
        MANUAL_START_ACTION, {"name": name, "generation": generation, "bound": bound}
    )


def _runner_info(runner: SandboxRunner) -> tuple[frozenset[str], str | None]:
    """包住時記下的 features 與（執行器的）boot_id."""
    if isinstance(runner, ExecutorRunner):
        return runner.features, runner.boot_id
    if isinstance(runner, LandlockRunner):
        return runner.features, None
    return frozenset(), None


@dataclass(frozen=True)
class _Classified:
    """執行緒裡讀完紀錄、分類完的結果."""

    classification: McpClassification
    record_state: McpRecordState
    sandbox_paths: tuple[SandboxPathGrant, ...]
    layout: SandboxLayout
    base_dir: Path

    def info(
        self,
        state: McpLaunchState,
        level: IsolationLevel,
        *,
        features: frozenset[str] = frozenset(),
        reason: str | None = None,
        boot_id: str | None = None,
        consented_direct: bool = False,
    ) -> McpLaunchInfo:
        found = self.classification
        return McpLaunchInfo(
            found.kind, found.origin, self.record_state, state, level, features, reason, boot_id,
            consented_direct,
        )  # fmt: skip


@dataclass(frozen=True)
class _SelfProfile:
    """自建的 profile 來源：位置選好才建；組不出來時只有「未隔離」的同意改用不需要 ST 的."""

    name: str
    found: _Classified
    monitor: McpLaunchMonitor
    grant: ExecutionGrant

    async def __call__(self, location: Location, executor_state: str | None) -> SandboxProfile:
        try:
            return build_mcp_self_profile(
                self.name,
                self.found.classification,
                layout=self.found.layout,
                location=location,
                block_ports=self.monitor.block_ports(location),
                executor_state=executor_state,
            )
        except (ProfileError, OSError) as exc:
            error = profile_unavailable(exc)
            if location != "proxy":
                raise error from exc
            return unbound_or_refuse(self.grant, self.found.base_dir, error)


class _ThirdPartyProfile:
    """第三方與 scaffold 的 profile 來源（只在 proxy 位置）；建好的參數與 env 留給啟動用."""

    def __init__(
        self,
        name: str,
        entry: McpEntry,
        found: _Classified,
        monitor: McpLaunchMonitor,
        grant: ExecutionGrant | None = None,
    ) -> None:
        self._name, self._entry, self._found = name, entry, found
        self._monitor, self._grant = monitor, grant
        self.built: McpThirdPartyLaunch | None = None

    async def __call__(self, location: Location, _executor_state: str | None) -> SandboxProfile:
        if location != "proxy":
            raise IsolationUnavailableError(ISOLATION_UNAVAILABLE)
        try:
            self.built = await build_mcp_third_party_profile(
                self._name,
                self._entry,
                self._found.classification,
                layout=self._found.layout,
                sandbox_paths=self._found.sandbox_paths,
                block_ports=self._monitor.block_ports("proxy"),
            )
        except (ProfileError, OSError) as exc:
            error = profile_unavailable(exc)
            if self._grant is None:
                raise error from exc
            return unbound_or_refuse(self._grant, self._found.base_dir, error)
        return self.built.profile


class McpLauncher:
    """MCP 啟動判定（DIP：monitor、broker、版面、base_dir、帳本都可注入）."""

    def __init__(
        self,
        *,
        monitor: McpLaunchMonitor = isolation_monitor,
        broker: RunnerBroker = runner_broker,
        layout_provider: Callable[[], SandboxLayout] = current_layout,
        base_dir: Callable[[], Path] = proxy_base_dir,
        book: McpLaunchBook = mcp_launch_book,
    ) -> None:
        self._monitor = monitor
        self._broker = broker
        self._layout_provider = layout_provider
        self._base_dir = base_dir
        self._book = book

    async def plan_server_launch(
        self,
        name: str,
        entry: McpEntry,
        reader: IsolationRecordReader,
        *,
        tool_base_url: str,
        consent: Consent | None = None,
    ) -> McpLaunch:
        """已安裝伺服器的正式啟動（開機、熱插拔、重連、第一次用到）；結果記進帳本.

        先不啟動時拋 ``McpLaunchWaiting``。``consent`` 只給人在設定頁確認過的那一次（自建）。
        """
        found = await asyncio.to_thread(self._classify, name, entry, reader)
        kind = found.classification.kind
        try:
            if kind == "remote":
                launch = McpLaunch(found.info("launched", "none"))
            elif kind == "self":
                launch = await self._self(name, entry, found, tool_base_url, consent)
            else:
                launch = await self._third_party(name, entry, found, tool_base_url)
        except McpLaunchWaiting as waiting:
            self._book.record(name, found.info(waiting.state, "none", reason=waiting.reason))
            raise
        self._book.record(name, launch.info)
        return launch

    async def candidate_launch(
        self,
        grant: ExecutionGrant,
        name: str,
        entry: McpEntry,
        *,
        kind: Literal["self", "scaffold"],
    ) -> McpLaunch:
        """``create_mcp``／``scaffold_mcp`` 的試跑：和正式啟動同一個 profile；拿不到就拋.

        環境照 ``clean`` 的語意（spec 的 env 原樣，不解析哨兵、不加 ``TOOL_BASE_URL``，SP7），只濾掉
        profile 管的名稱。create 用政策層級（``acquire``），scaffold 用本機層級
        （``acquire_local``）。不記進帳本。
        """
        origin = "creation" if kind == "self" else MCP_SCAFFOLD_ORIGIN
        record = OriginRecord("present", origin)
        found = await asyncio.to_thread(self._classify_with, name, entry, record, "ok", ())
        if found.classification.kind == "remote":
            return McpLaunch(found.info("launched", "none"))
        acquire = self._broker.acquire if kind == "self" else self._broker.acquire_local
        args: Sequence[str] = resolve_args_sentinels(list(entry.args))
        if found.classification.kind == "self":
            source = ProfileSource(_SelfProfile(name, found, self._monitor, grant), None)
            acquired = await acquire(grant, source)
        else:
            third = _ThirdPartyProfile(name, entry, found, self._monitor, grant)
            acquired = await acquire(grant, ProfileSource(third, ISOLATION_UNAVAILABLE))
            args = third.built.args if third.built is not None else args
        exec_env = checked_exec_env(dict(entry.env or {}), label=name)
        return self._launch(found, acquired, (entry.command or "", *args), exec_env)

    def _classify(self, name: str, entry: McpEntry, reader: IsolationRecordReader) -> _Classified:
        """在執行緒裡：讀紀錄（``bound`` 由 reader 核對）、分類（會讀檔案系統中繼資料）."""
        view = reader.record_for(name, entry_identity(entry))
        record = origin_record(view.effective_origin, records_unreadable=view.records_unreadable)
        return self._classify_with(name, entry, record, view.state, view.usable_sandbox_paths)

    def _classify_with(
        self,
        name: str,
        entry: McpEntry,
        record: OriginRecord,
        record_state: McpRecordState,
        sandbox_paths: Sequence[SandboxPathGrant],
    ) -> _Classified:
        layout, base_dir = self._layout_provider(), self._base_dir()
        found = classify(name, entry, record, layout, base_dir=base_dir)
        if found.kind != "remote":
            for home in mcp_classification_bases(name, layout=layout, base_dir=base_dir)[1:]:
                actual = classify(name, entry, record, layout, base_dir=home)
                skills = found.managed_skills | actual.managed_skills
                found = replace(actual if actual.kind == "self" else found, managed_skills=skills)
        paths = tuple(sandbox_paths) if found.kind in THIRD_PARTY_CLASSES else ()
        return _Classified(found, record_state, paths, layout, base_dir)

    async def _self(
        self,
        name: str,
        entry: McpEntry,
        found: _Classified,
        tool_base_url: str,
        consent: Consent | None,
    ) -> McpLaunch:
        """自建：層級 none 不自動啟動；人同意時走 broker 的降級規則（不弱於卡片上的去處）."""
        snap = self._monitor.snapshot()
        if consent is None and snap.level == "none":
            raise McpLaunchWaiting(WAITING_ISOLATION, snap.reason)
        grant = ExecutionGrant(snap.level if consent is None else "none", consent)
        source = ProfileSource(_SelfProfile(name, found, self._monitor, grant), None)
        try:
            acquired = await self._broker.acquire(grant, source)
        except IsolationUnavailableError as exc:  # 判定之後層級掉了、profile 組不出來
            raise McpLaunchWaiting(WAITING_ISOLATION, exc.reason) from exc
        argv = (entry.command or "", *resolve_args_sentinels(list(entry.args)))
        exec_env = self._exec_env(name, resolve_env_sentinels(dict(entry.env or {})), tool_base_url)
        return self._launch(
            found, acquired, argv, exec_env, reason=snap.reason, consented=consent is not None
        )

    async def _third_party(
        self, name: str, entry: McpEntry, found: _Classified, tool_base_url: str
    ) -> McpLaunch:
        """第三方與 scaffold：看本機判定；穩定原因照今天的方式（不建 profile）."""
        third = _ThirdPartyProfile(name, entry, found, self._monitor)
        try:
            got = await self._broker.acquire_third_party(
                ProfileSource(third, ISOLATION_UNAVAILABLE)
            )
        except IsolationUnavailableError as exc:  # 含 IsolationPendingError
            raise McpLaunchWaiting(WAITING_PROBE, exc.reason) from exc
        if got.acquired is None or third.built is None:
            return McpLaunch(found.info("launched", "none", reason=got.reason))
        built = third.built
        exec_env = self._exec_env(name, built.env, tool_base_url)
        return self._launch(found, got.acquired, (built.command or "", *built.args), exec_env)

    @staticmethod
    def _exec_env(name: str, env: Mapping[str, str], tool_base_url: str) -> dict[str, str]:
        """條目的 env 加 Touka 的工具網址（同今天，Touka 的值優先），再濾掉 profile 管的名稱."""
        return checked_exec_env({**env, TOOL_BASE_URL: tool_base_url}, label=name)

    @staticmethod
    def _launch(
        found: _Classified,
        acquired: Acquired,
        argv: tuple[str, ...],
        exec_env: Mapping[str, str],
        *,
        reason: str | None = None,
        consented: bool = False,
    ) -> McpLaunch:
        """包住的照 runner 記下層級；人同意的未隔離（DirectRunner）照今天的方式啟動並記下來."""
        runner = acquired.runner
        if isinstance(runner, DirectRunner):
            info = found.info("launched", "none", reason=reason, consented_direct=consented)
            return McpLaunch(info)
        features, boot_id = _runner_info(runner)
        info = found.info("launched", runner.level, features=features, boot_id=boot_id)
        return McpLaunch(info, acquired, argv, exec_env)


mcp_launcher = McpLauncher()


class McpRelaunchHost(Protocol):
    """手動啟動需要 manager 提供的三件事（U3e 接上）."""

    def server_entry(self, name: str) -> McpEntry | None:
        """設定裡這台目前的條目；沒有是 None."""
        ...

    def get_installation_generation(self, name: str) -> int:
        """這台目前的 installation generation."""
        ...

    async def relaunch_with_consent(self, name: str, consent: Consent) -> bool:
        """帶這次同意重新判定、連線一次（``plan_server_launch(consent=...)``）."""
        ...


class McpManualStart:
    """``routers/mcp.McpManualStarter`` 的實作（U3e 放進 lifespan context）.

    只對「自建、在等隔離」的伺服器；去處由 monitor 當下算；同意要和現在的去處、generation、
    ``bound`` 都相符，只用這一次啟動（不留著給之後的重連）；同一台同時只能有一個手動啟動。
    """

    def __init__(
        self,
        host: McpRelaunchHost,
        *,
        monitor: McpLaunchMonitor = isolation_monitor,
        book: McpLaunchBook = mcp_launch_book,
    ) -> None:
        self._host = host
        self._monitor = monitor
        self._book = book
        self._starting: set[str] = set()

    def manual_start_plan(self, name: str) -> FallbackPlan | None:
        """這台是自建而且在等隔離時，回伺服器當下算的降級去處；否則 None."""
        info = self._book.get(name)
        if info is None or info.mcp_class != "self" or info.state != WAITING_ISOLATION:
            return None
        return self._monitor.fallback_for(profile_complete=True, executor_eligible=True)

    async def start_with_consent(self, name: str, consent: Consent) -> bool:
        """去處（ack）、generation、``bound`` 都和現在一樣才啟動一次；否則拒絕（回 False）."""
        refused = (
            "already_starting" if name in self._starting else self.consent_refusal(name, consent)
        )
        if refused is not None:
            logger.warning("AUDIT: manual start of MCP %s refused (%s)", name, refused)
            return False
        self._starting.add(name)
        try:
            return await self._host.relaunch_with_consent(name, consent)
        finally:
            self._starting.discard(name)

    def consent_refusal(self, name: str, consent: Consent) -> str | None:
        """取鎖前與取鎖後共用核對，避免等待期間的狀態變動沿用舊同意。"""
        plan = self.manual_start_plan(name)
        entry = self._host.server_entry(name)
        if plan is None or entry is None:
            return "not_waiting_isolation"
        if consent.fallback != plan.kind or consent.features != plan.features:
            return "isolation_ack_mismatch"
        generation = self._host.get_installation_generation(name)
        subject = manual_start_subject(name, generation, bound_of(entry_identity(entry)))
        return None if consent.subject == subject else "entry_changed"
