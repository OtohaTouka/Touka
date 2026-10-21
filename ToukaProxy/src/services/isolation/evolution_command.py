"""演化（建 venv、pip、本機驗證）接上隔離 runner（iso-c2）.

- grant：工具開頭取一次（同意帳本用正規化 subject 取回，只能一次；沒有就照現在的層級、沒有
  同意），同一次呼叫的每條指令共用。沒有已驗證身分＝沒有 grant：以層級 none、沒有同意交給
  broker，真的 broker 一律拒絕（fail-closed）。
- 每條指令 spawn 前才建 profile、``acquire``；``plan`` 在 ``to_thread`` 的 worker 裡建，spawn
  也在那裡，結束後 ``finish``（關 fd、收下狀態 fd 的內容）。PDEATHSIG 綁在建立子行程的執行緒：
  worker 同步等子行程結束，子行程存活期間 worker 不會結束。回到事件迴圈後 ``report``：啟動器的
  失敗代碼交給 monitor 重探（monitor 只在事件迴圈上用）。
- 執行器範圍：取 grant 時（有身分）算好放進 grant（``executor_eligible``），和政策層級同一套
  判定（``executor_scope_issue``，看身分的資料夾）；沒有算過的 grant 一律不在範圍。
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation.agent_command import (
    UNBOUND_PROFILE,
    command_scope,
    profile_unavailable,
    take_grant,
    unbound_or_refuse,
)
from src.services.isolation.consent import ExecutionGrant
from src.services.isolation.mcp_launch import McpLaunch
from src.services.isolation.mcp_profiles import mcp_classification_bases
from src.services.isolation.profiles import (
    ProfileError,
    SandboxProfile,
    build_evolution_profile,
    current_layout,
)
from src.services.isolation.runners import (
    ExecRequest,
    IsolationUnavailableError,
    ProfileSource,
    executor_max_seconds,
    runner_broker,
)
from src.services.isolation.status import AGENT_DIR_NOT_IN_EXECUTOR, isolation_monitor
from src.utils import config_utils
from src.utils.config_utils import MCP_CONFIG_FILENAME
from src.utils.mcp_origin import deployed_skills, proxy_base_dir

if TYPE_CHECKING:
    import subprocess
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from src.models.identity import AgentIdentity
    from src.sandbox_kernel.spec import RlimitName
    from src.services.isolation.profiles import Location
    from src.services.isolation.runners import LaunchPlan, SandboxRunner


MAX_SKILL_LOCKS = 4096
SKILL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
WINDOWS_RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul"}) | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}
_skill_locks: dict[Path, tuple[asyncio.Lock, asyncio.Task | None]] = {}


class EvolutionRefused(Exception):
    """演化的驗證拒絕，與隔離環境不可用分開。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_skill_id(skill_id: str) -> None:
    """寫檔與取得 runner 之前，只接受單層技能名稱。"""
    if (
        not isinstance(skill_id, str)
        or not SKILL_ID_PATTERN.fullmatch(skill_id)
        or skill_id.casefold() in WINDOWS_RESERVED_NAMES
    ):
        raise EvolutionRefused("invalid_skill_id")


def _overlapping_roots(first: Path, second: Path) -> bool:
    if first.exists() and second.exists() and os.path.samefile(first, second):  # noqa: PTH121
        return True
    first, second = (
        Path(*(os.path.normcase(part.rstrip(". ")).casefold() for part in path.parts))
        for path in (first, second)
    )
    return first.is_relative_to(second) or second.is_relative_to(first)


@asynccontextmanager
async def skill_lock(skill_id: str, inherited: object = None) -> AsyncIterator[asyncio.Lock]:
    """同技能不排隊；只有同一 task 持有的實體憑證能重入，閒置鎖立即淘汰。"""
    key = EvolutionPathGuard.get_tool_root(skill_id)
    key = next((held for held in _skill_locks if _overlapping_roots(key, held)), key)
    held = _skill_locks.get(key)
    if held is not None:
        if inherited is not held[0] or asyncio.current_task() is not held[1]:
            raise EvolutionRefused("skill_busy")
        yield held[0]
        return
    if len(_skill_locks) >= MAX_SKILL_LOCKS:
        raise EvolutionRefused("skill_busy")
    lock = asyncio.Lock()
    await lock.acquire()
    _skill_locks[key] = (lock, asyncio.current_task())
    try:
        yield lock
    finally:
        del _skill_locks[key]
        lock.release()


def evolution_config() -> dict[str, Any]:
    """讀原始設定，含停用條目；存在卻讀不出來時拒絕。"""
    try:
        document = config_utils.read_config_json(MCP_CONFIG_FILENAME)
        if document is None and not config_utils.config_file_exists(MCP_CONFIG_FILENAME):
            document = {}
        if not isinstance(document, dict) or not isinstance(document.get("mcpServers", {}), dict):
            raise EvolutionRefused("config_unreadable")
    except (OSError, ValueError) as exc:
        raise EvolutionRefused("config_unreadable") from exc
    else:
        return document


def refuse_deployed(skill_id: str) -> None:
    """工具入口與取得 runner 共用同一份凍結判定。"""
    layout, base_dir = current_layout(), proxy_base_dir()
    deployed = deployed_skills(
        evolution_config(), layout, base_dir=base_dir,
        bases_for=partial(mcp_classification_bases, layout=layout, base_dir=base_dir),
    )
    root = EvolutionPathGuard.get_tool_root(skill_id)
    same_path = any(
        _overlapping_roots(root, EvolutionPathGuard.get_tool_root(name)) for name in deployed.skills
    )
    if deployed.covers(skill_id) or same_path:
        raise EvolutionRefused("skill_deployed")


@dataclass(frozen=True)
class EvolutionStdioLaunch(McpLaunch):
    """驗證沿用 MCP 傳輸，同時保留演化的配額與執行器時限。"""

    preexec_fn: Callable[[], None] | None = None
    timeout: float = 30

    def request(self, launcher_env: Mapping[str, str]) -> ExecRequest:
        """保留 stdio，另帶 direct 的配額與執行器逾時。"""
        return replace(
            super().request(launcher_env), preexec_fn=self.preexec_fn,
            max_seconds=executor_max_seconds(self.timeout),
        )


@dataclass(frozen=True)
class EvolutionLaunch:
    """spawn 前選好的 runner 與 profile（一條指令一份）；``statuses``：worker 收下的狀態 fd 內容."""

    runner: SandboxRunner
    profile: SandboxProfile
    statuses: list[bytes] = field(default_factory=list)

    def finish(self, plan: LaunchPlan) -> None:
        """在 worker 裡 spawn 結束後（成功或失敗）：關掉交給子行程的 fd，讀完狀態 fd 並關掉讀端."""
        plan.release()
        self.statuses.append(plan.read_status())

    async def run(
        self, skill_id: str, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess:
        """在 ``to_thread`` 的 worker 裡跑一條沙盒指令；結束後（含失敗）回報啟動器的失敗代碼."""
        try:
            return await asyncio.to_thread(
                EvolutionPathGuard.run_sandbox_command, skill_id, argv, launch=self, **kwargs
            )
        finally:
            self.report()

    def report(self) -> None:
        """事件迴圈上：狀態 fd 裡啟動器寫的失敗代碼交給 monitor（只請求重探）."""
        while self.statuses:
            failure = self.runner.failure(self.statuses.pop(0))
            if failure is not None:
                isolation_monitor.report_failure(failure.code)

    def plan(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        preexec_fn: Callable[[], None] | None,
        rlimits: Mapping[RlimitName, int],
        timeout: float | None = None,
    ) -> LaunchPlan:
        """改寫啟動參數；rlimit 交給啟動器，直接執行時由同一份上限的 ``preexec_fn`` 套用.

        ``timeout`` 是本機逾時；執行器那邊的上限是它加 30 秒（``executor_max_seconds``）。
        """
        profile = replace(self.profile, rlimits=dict(rlimits))
        request = ExecRequest(
            argv=tuple(argv), env=env, profile=profile, preexec_fn=preexec_fn,
            max_seconds=executor_max_seconds(timeout),
        )
        return self.runner.plan(request)


async def evolution_grant(
    identity: AgentIdentity | None,
    session_id: str | None,
    call_id: str | None,
    *,
    tool: str,
    args: Mapping[str, Any],
) -> ExecutionGrant | None:
    """工具開頭取一次：帳本有這次呼叫的紀錄就取回，沒有就照現在的層級；沒有身分回 None."""
    if identity is None:
        return None
    grant = take_grant(identity, session_id, call_id, tool=tool, args=args)
    grant = (await command_scope(identity, None, grant)).grant
    eligible = isolation_monitor.executor_scope_issue(identity, None) is None
    return replace(grant, executor_eligible=eligible)


def evolution_profile(
    skill_id: str, location: Location = "proxy", executor_state: str | None = None
) -> SandboxProfile:
    """演化的 profile（建置與驗證共用路徑權利；B 用 monitor 的）；組不出來就當隔離不可用."""
    root = EvolutionPathGuard.get_tool_root(skill_id)
    try:
        return build_evolution_profile(
            skill_id,
            root,
            layout=current_layout(),
            location=location,
            block_ports=isolation_monitor.block_ports(location),
            executor_state=executor_state,
        )
    except (ProfileError, OSError) as exc:
        raise profile_unavailable(exc) from exc


async def _profile_for(
    grant: ExecutionGrant, skill_id: str, cwd: Path, location: Location, executor_state: str | None
) -> SandboxProfile:
    try:
        return evolution_profile(skill_id, location, executor_state)
    except IsolationUnavailableError as exc:
        if location != "proxy":
            raise
        return unbound_or_refuse(grant, cwd, exc)  # 「未隔離」卡核准過才改直接執行


async def acquire_evolution_launch(grant: ExecutionGrant | None, skill_id: str) -> EvolutionLaunch:
    """每條指令在 spawn 前才呼叫；拿不到 runner 就拋 ``IsolationUnavailableError``."""
    validate_skill_id(skill_id)
    await asyncio.to_thread(refuse_deployed, skill_id)
    cwd = EvolutionPathGuard.get_tool_root(skill_id)
    if grant is None:
        unbound = SandboxProfile(name=UNBOUND_PROFILE, cwd=cwd, rules=(), block_ports=())
        grant, source = ExecutionGrant("none"), ProfileSource.fixed(unbound)
    else:
        issue = None if grant.executor_eligible else AGENT_DIR_NOT_IN_EXECUTOR
        source = ProfileSource(partial(_profile_for, grant, skill_id, cwd), issue)
    acquired = await runner_broker.acquire(grant, source)
    return EvolutionLaunch(acquired.runner, acquired.profile)
