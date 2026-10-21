"""代理人指令（終端、互審驗證指令）接上隔離 runner（iso-c2）.

- grant：同意帳本有這次呼叫的紀錄就取回（只能一次）；沒有就照現在的層級、沒有同意。
- spawn 前才建 profile、``acquire``、``plan``；呼叫端 spawn 之後（成功或失敗）都要
  ``plan.release()``，跑完（含逾時、取消、spawn 失敗）都要 ``report()``（讀狀態 fd、關掉讀端）。
- 沒有隔離脈絡（``scope`` 是 None）的呼叫一律以層級 none、沒有同意交給 broker：真的 broker
  一律拒絕（fail-closed）。
- profile 由 broker 選好位置才建（proxy 或執行器）；B 用 monitor 的 ``block_ports(location)``；
  這次在不在執行器範圍由 monitor 的 ``executor_scope_issue`` 判定（資料夾、專案）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.services.isolation.consent import (
    ConsentKey,
    ExecutionGrant,
    consent_ledger,
    consent_subject,
)
from src.services.isolation.profiles import (
    ProfileError,
    SandboxProfile,
    build_agent_command_profile,
    current_layout,
)
from src.services.isolation.runners import (
    ISOLATION_UNAVAILABLE,
    ExecRequest,
    IsolationUnavailableError,
    ProfileSource,
    runner_broker,
)
from src.services.isolation.status import isolation_monitor

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from src.models.identity import AgentIdentity
    from src.services.isolation.profiles import Location
    from src.services.isolation.runners import LaunchPlan, SandboxRunner
    from src.utils.isolation_policy import IsolationLevel

UNBOUND_PROFILE = "unbound"
# 埠提示：隔離內用封鎖埠起服務或連線失敗時，給模型看的一行英文
BLOCKED_PORT_HINT = (
    "[isolation] Port {ports} is blocked inside the sandbox (reserved for Touka or a local "
    "upstream service); use a different port."
)
_PERMISSION_DENIED = re.compile(r"permission denied|eacces", re.IGNORECASE)


@dataclass(frozen=True)
class CommandScope:
    """一條代理人指令的隔離脈絡：身分、執行時取回的 grant、專案根（只來自 session）。"""

    identity: AgentIdentity
    grant: ExecutionGrant
    project_root: Path | None = None


@dataclass(frozen=True)
class PreparedCommand:
    """選好的 runner 與它改寫後的啟動參數。"""

    runner: SandboxRunner
    plan: LaunchPlan
    profile: SandboxProfile

    def port_hint(self, exit_code: int, text: str) -> str | None:
        """隔離內（不是 none）失敗、輸出有 Permission denied／EACCES、而且出現 B 裡的埠時的提示."""
        if self.plan.level == "none" or exit_code == 0 or not _PERMISSION_DENIED.search(text):
            return None
        ports = [p for p in self.profile.block_ports if re.search(rf"(?<!\d){p}(?!\d)", text)]
        return BLOCKED_PORT_HINT.format(ports=", ".join(map(str, ports))) if ports else None

    def report(self) -> None:
        """跑完（含逾時、取消、spawn 失敗）讀狀態 fd：只認啟動器寫的代碼、只請求重探；讀完就關."""
        failure = self.runner.failure(self.plan.read_status())
        if failure is not None:
            isolation_monitor.report_failure(failure.code)


def take_grant(
    identity: AgentIdentity,
    session_id: str | None,
    call_id: str | None,
    *,
    tool: str,
    args: Mapping[str, Any],
) -> ExecutionGrant | None:
    """取回這次呼叫在政策時記下的 grant（subject 用自己的參數重算；只能一次）。"""
    key = ConsentKey.of(identity, session_id, call_id)
    return consent_ledger.take(key, subject=consent_subject(tool, args))


async def command_level(identity: AgentIdentity, project_root: Path | None) -> IsolationLevel:
    """這條指令現在的層級（有看專案）；驗證指令分類與沒有 grant 時共用。"""
    await isolation_monitor.ensure_ready()
    return (await isolation_monitor.level_for(identity, project_root)).level


async def command_scope(
    identity: AgentIdentity, project_root: Path | None, grant: ExecutionGrant | None
) -> CommandScope:
    """沒有取回 grant（沒經過政策或卡片）就照現在的層級、沒有同意。"""
    if grant is None:
        grant = ExecutionGrant(await command_level(identity, project_root))
    else:
        await isolation_monitor.ensure_ready()  # 等人核准期間狀態可能變了：spawn 前重看
    return CommandScope(identity, grant, project_root)


def profile_unavailable(exc: ProfileError | OSError) -> IsolationUnavailableError:
    """組不出 profile：帶出真正的原因（ST 未設定或不可寫），其他問題用籠統原因。"""
    reason = exc.reason if isinstance(exc, ProfileError) else None
    return IsolationUnavailableError(reason or ISOLATION_UNAVAILABLE)


def unbound_or_refuse(
    grant: ExecutionGrant, cwd: Path, error: IsolationUnavailableError
) -> SandboxProfile:
    """組不出 profile 時：只有人在「未隔離」卡上核准過，才改用不需要 ST 的 profile.

    這個 profile 標成不完整，broker 只會給 ``DirectRunner``；卡片寫「隔離降級」或沒有同意
    都照舊拒絕。
    """
    if grant.consent is None or grant.consent.fallback != "direct":
        raise error
    return SandboxProfile(
        name=UNBOUND_PROFILE, cwd=cwd, rules=(), block_ports=(), ungranted=(error.reason,)
    )


async def build_command_profile(
    scope: CommandScope, cwd: Path, location: Location = "proxy", executor_state: str | None = None
) -> SandboxProfile:
    """代理人指令的 profile；組不出來（路徑形狀不對、ST 不可用）就當隔離不可用（見上）.

    執行器位置組不出來一律拒絕（「未隔離」的不完整 profile 只在 proxy 位置）。
    """
    try:
        return await build_agent_command_profile(
            scope.identity,
            cwd,
            layout=current_layout(),
            project_root=scope.project_root,
            location=location,
            block_ports=isolation_monitor.block_ports(location),
            executor_state=executor_state,
        )
    except (ProfileError, OSError) as exc:
        if location != "proxy":
            raise profile_unavailable(exc) from exc
        return unbound_or_refuse(scope.grant, cwd, profile_unavailable(exc))


async def prepare_command(
    scope: CommandScope | None,
    argv: Sequence[str],
    env: Mapping[str, str],
    cwd: Path | str,
    *,
    max_seconds: int | None = None,
) -> PreparedCommand:
    """選 runner（spawn 前才呼叫）；拿不到就拋 ``IsolationUnavailableError``.

    ``max_seconds`` 是執行器那邊的上限（``executor_max_seconds``：本機逾時＋30 秒）。
    """
    if scope is None:
        unbound = SandboxProfile(name=UNBOUND_PROFILE, cwd=Path(cwd), rules=(), block_ports=())
        grant, source = ExecutionGrant("none"), ProfileSource.fixed(unbound)
    else:
        issue = isolation_monitor.executor_scope_issue(scope.identity, scope.project_root)
        grant = scope.grant
        source = ProfileSource(partial(build_command_profile, scope, Path(cwd)), issue)
    acquired = await runner_broker.acquire(grant, source)
    request = ExecRequest(
        argv=tuple(argv), env=env, profile=acquired.profile, max_seconds=max_seconds
    )
    return PreparedCommand(acquired.runner, acquired.runner.plan(request), acquired.profile)
