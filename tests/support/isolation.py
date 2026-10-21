"""隔離層的測試替身（iso-c2）.

conftest 的 autouse fixture 用 ``install_isolation_doubles`` 把層級釘在 ``landlock``、runner
換成直通，讓既有測試不受隔離層影響；``tests/integration/test_isolation_linux.py`` 以
``real_isolation`` fixture 退出替身。其他模組不得使用 ``real_isolation``（有 lint 測試）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from src.services import mcp_isolation_store
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation import agent_command, evolution_command
from src.services.isolation.consent import consent_ledger
from src.services.isolation.evolution_command import EvolutionLaunch
from src.services.isolation.mcp_launch import (
    McpLaunch,
    McpLaunchInfo,
    mcp_launch_book,
    mcp_launcher,
)
from src.services.isolation.profiles import SandboxLayout, SandboxProfile, blocked_ports
from src.services.isolation.runners import (
    Acquired,
    LaunchPlan,
    ProfileSource,
    RunnerBroker,
    runner_broker,
)
from src.services.isolation.status import (
    FallbackPlan,
    IsolationSnapshot,
    LandlockState,
    LevelDecision,
    LocalDecision,
    isolation_monitor,
)
from src.utils.isolation_policy import (
    EXECUTOR_LAYER_MIN_ABI,
    LANDLOCK_LAYER_MIN_ABI,
    TCP_FEATURE,
    effective_abi,
    landlock_features,
)

# 真的演化 profile（替身裝上前取得；演化的隔離測試要換回真的時用）
REAL_EVOLUTION_PROFILE = evolution_command.evolution_profile

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from src.services.isolation.agent_command import CommandScope
    from src.services.isolation.consent import ExecutionGrant
    from src.services.isolation.profiles import Location
    from src.services.isolation.runners import ExecRequest
    from src.services.isolation.status import IsolationMonitor
    from src.utils.isolation_policy import IsolationLevel


def pinned_snapshot(
    level: IsolationLevel = "landlock",
    *,
    abi: int = 7,
    local_status: str = "available",
    reason: str | None = None,
    net: str | None = None,
    expected: IsolationLevel | None = None,
    block_ports_issue: str | None = None,
) -> IsolationSnapshot:
    """固定的判定結果（預設：landlock 層、ABI 7），欄位照真的 monitor 組.

    本機 Landlock 只有 unavailable 時帶原因；``net`` 是自測回報的網路設定問題（features 扣掉 tcp）；
    ``expected`` 預設 none 時是 landlock、其餘同 ``level``（設了執行器目錄時傳 executor）；
    ``block_ports_issue`` 是封鎖埠擋不全（讀不到設定或溢位）。
    """
    local_reason = reason if local_status == "unavailable" else None
    local = LandlockState(local_status, abi=abi, reason=local_reason, net=net)  # type: ignore[arg-type]
    if expected is None:
        expected = "landlock" if level == "none" else level
    return IsolationSnapshot(
        level, expected, reason, None, local, local.features, block_ports_issue=block_ports_issue
    )


@dataclass
class PinnedMonitor:
    """釘住層級的 monitor；介面與 ``IsolationMonitor`` 相同，記錄失敗標記.

    ``state_ok``、``kernel_issue`` 是真的 monitor 的 ST 與 kernel 健康檢查結果（可能被更前面的原因
    蓋住，所以不從 ``snap.reason`` 推）；``executor_features`` 是可用、只差網路設定或封鎖埠的執行器
    的 features（None＝執行器不能當降級去處）。``fallback_for`` 的規則和真的相同（有測試逐一比對）。
    ``scope_issue`` 是資料夾的範圍判定（None＝在範圍內；綁了專案一律不在）；``block_ports`` 給
    Touka 自己的埠（monitor 的上游埠由 ``test_isolation_monitor_block_ports_security`` 另外驗）。
    """

    snap: IsolationSnapshot = field(default_factory=pinned_snapshot)
    state_ok: bool = True
    kernel_issue: str | None = None
    executor_features: frozenset[str] | None = None
    checked_kernel_digest: str | None = None
    failures: list[str] = field(default_factory=list)
    scope_issue: str | None = None
    state_detail: str = "unset"  # state_ok 是假時真的 monitor 的 ST 細節（unset／not_writable）

    def snapshot(self) -> IsolationSnapshot:
        return self.snap

    def local_decision(self) -> LocalDecision:
        """本機判定，規則同真的 monitor（有測試逐一比對）；預期 none 當作不是 Linux."""
        snap, local = self.snap, self.snap.local
        if snap.expected == "none":
            return LocalDecision("unavailable", "native_platform")
        if local.status == "pending":
            return LocalDecision("pending", "probe_pending")
        if local.status == "unavailable":
            return LocalDecision("unavailable", local.reason)
        if self.kernel_issue:
            return LocalDecision("unavailable", self.kernel_issue)
        if not self.state_ok:
            return LocalDecision("unavailable", "sandbox_state_unconfigured", self.state_detail)
        tcp = {TCP_FEATURE} if snap.block_ports_issue else set()
        return LocalDecision("available", features=local.features - tcp)

    def local_level(self) -> LevelDecision:
        decision, local = self.local_decision(), self.snap.local
        if decision.status != "available":
            return LevelDecision("none", decision.reason)
        if effective_abi(local.abi) < LANDLOCK_LAYER_MIN_ABI:
            return LevelDecision("none", "landlock_abi_insufficient")
        if local.net or self.snap.block_ports_issue:
            return LevelDecision("none", local.net or self.snap.block_ports_issue)
        return LevelDecision("landlock")

    async def ensure_ready(self) -> IsolationSnapshot:
        return self.snap

    async def refresh(self) -> IsolationSnapshot:
        return self.snap

    async def level_for(self, _identity: Any, _project_root: Any) -> LevelDecision:
        return LevelDecision(self.snap.level, self.snap.reason)

    def fallback_for(
        self, *, profile_complete: bool, executor_eligible: bool = False
    ) -> FallbackPlan:
        if executor_eligible and self.executor_features is not None:
            return FallbackPlan("executor", self.executor_features)
        local = self.snap.local
        usable = local.status == "available" and effective_abi(local.abi) >= EXECUTOR_LAYER_MIN_ABI
        if usable and self.state_ok and not self.kernel_issue and profile_complete:
            tcp = {TCP_FEATURE} if self.snap.block_ports_issue else set()
            return FallbackPlan("landlock", local.features - tcp)
        return FallbackPlan("direct", frozenset())

    def executor_scope_issue(self, _identity: Any, project_root: Any) -> str | None:
        if self.scope_issue is not None or project_root is None:
            return self.scope_issue
        return "project_not_in_executor"  # 同真的 monitor：綁了專案一律不在執行器範圍

    def block_ports(self, location: Location) -> tuple[int, ...]:
        return blocked_ports(location)

    def report_failure(self, code: str) -> None:
        self.failures.append(code)

    def on_level_change(self, _callback: Any) -> None:
        return None

    def with_level(self, level: IsolationLevel) -> PinnedMonitor:
        return replace(self, snap=pinned_snapshot(level))


@dataclass(frozen=True)
class PassthroughRunner:
    """直通 runner：argv、env 原樣，層級照 grant。"""

    level: IsolationLevel

    def plan(self, request: ExecRequest) -> LaunchPlan:
        profile = request.profile
        return LaunchPlan(
            argv=tuple(request.argv),
            cwd=profile.cwd,
            env=dict(request.env),
            preexec_fn=request.preexec_fn,
            pass_fds=(),
            level=self.level,
            features=landlock_features(7),
        )

    def failure(self, _status: bytes) -> None:
        return None


async def passthrough_acquire(grant: ExecutionGrant, source: ProfileSource) -> Acquired:
    """測試替身的 ``runner_broker.acquire``：一律在 proxy 位置建 profile。"""
    return Acquired(PassthroughRunner(grant.policy_level), await source.build("proxy", None))


async def passthrough_command_profile(
    _scope: CommandScope, cwd: Path, _location: str = "proxy", _executor_state: str | None = None
) -> SandboxProfile:
    """測試替身的代理人指令 profile：cwd 原樣（既有測試的 drive 常是 tmp_path）。"""
    return SandboxProfile(name="agent_command", cwd=cwd, rules=(), block_ports=())


def passthrough_evolution_profile(
    skill_id: str, _location: str = "proxy", _executor_state: str | None = None
) -> SandboxProfile:
    """測試替身的演化 profile：cwd 是工具沙盒（既有測試常把沙盒根目錄換成 tmp_path）。"""
    cwd = EvolutionPathGuard.get_tool_root(skill_id)
    return SandboxProfile(name="evolution", cwd=cwd, rules=(), block_ports=())


def passthrough_launch(skill_id: str) -> EvolutionLaunch:
    """直接呼叫 ``run_sandbox_command`` 的既有測試用：直通 runner 加替身 profile。"""
    return EvolutionLaunch(PassthroughRunner("landlock"), passthrough_evolution_profile(skill_id))


@dataclass(frozen=True)
class FixedBroker:
    """既有測試用：以固定 profile（不在執行器範圍）呼叫真的 ``RunnerBroker.acquire``，回 runner."""

    broker: RunnerBroker

    async def acquire(self, grant: ExecutionGrant, profile: SandboxProfile) -> Any:
        return (await self.broker.acquire(grant, ProfileSource.fixed(profile))).runner


def fixed_interpreter(_layout: SandboxLayout) -> str:
    """測試用的 runner 直譯器：測試環境的 python 不是 root 擁有（假 kernel 只看 argv）。"""
    return sys.executable


def fake_kernel_layout(tmp_path: Path) -> SandboxLayout:
    """容器內的版面，kernel 目錄放一個假的 launcher（只看 argv，不真的啟動）。"""
    kernel = tmp_path / "kernel"
    kernel.mkdir(exist_ok=True)
    (kernel / "launcher.py").write_text("# kernel\n", encoding="utf-8")
    return SandboxLayout(
        data_root=tmp_path / "data",
        config_dir=tmp_path / "config",
        install_root=tmp_path,
        proxy_root=tmp_path / "ToukaProxy",
        env_file=tmp_path / ".env",
        state_root=tmp_path / "state",
        kernel_dir=kernel,
        executor_dir=None,
        mirror_roots=(),
        in_container=True,
    )


# 替身換掉的 monitor 方法（agent_command／evolution_command 會用到的全部）
MONITOR_METHODS = (
    "snapshot", "ensure_ready", "level_for", "fallback_for", "executor_scope_issue", "block_ports",
    "local_decision", "local_level",
)


def install_real_broker(
    monkeypatch: pytest.MonkeyPatch, snap: IsolationSnapshot, layout: SandboxLayout
) -> PinnedMonitor:
    """monitor 釘在 ``snap``，broker 換回真的 ``RunnerBroker``（降級規則照實執行）。"""
    monitor = PinnedMonitor(snap=snap)
    broker = RunnerBroker(
        monitor=monitor, layout_provider=lambda: layout, interpreter=fixed_interpreter
    )
    monkeypatch.setattr(runner_broker, "acquire", broker.acquire)
    for name in (*MONITOR_METHODS, "report_failure"):
        monkeypatch.setattr(isolation_monitor, name, getattr(monitor, name))
    return monitor


def real_isolation_handles() -> tuple[IsolationMonitor, RunnerBroker]:
    """退出替身時用的真實單例。"""
    return isolation_monitor, runner_broker


def reset_consent_ledger() -> None:
    """每個測試邊界清空同意、來源讀取與演化鎖的程序內狀態。"""
    consent_ledger.clear()
    mcp_launch_book.retain(())
    mcp_isolation_store._SEEN.clear()
    mcp_isolation_store._UNREADABLE.clear()
    evolution_command._skill_locks.clear()


# MCP 直通：照今天的方式啟動（SDK 的 stdio_client），既有的 MCP 測試不受啟動判定影響
PASSTHROUGH_MCP = McpLaunch(McpLaunchInfo("third_party", None, "none", "launched", "none"))


async def passthrough_mcp_launch(*_args: Any, **_kwargs: Any) -> McpLaunch:
    """測試替身的 ``mcp_launcher.plan_server_launch``／``candidate_launch``：不判定、不碰 ST."""
    return PASSTHROUGH_MCP


def install_isolation_doubles(
    monkeypatch: pytest.MonkeyPatch, level: IsolationLevel = "landlock"
) -> PinnedMonitor:
    """把 ``isolation_monitor`` 與 ``runner_broker`` 換成替身（不 spawn、不探測）。"""
    pinned = PinnedMonitor(snap=pinned_snapshot(level))
    for name in (*MONITOR_METHODS, "refresh"):
        monkeypatch.setattr(isolation_monitor, name, getattr(pinned, name))
    monkeypatch.setattr(isolation_monitor, "report_failure", pinned.report_failure)
    monkeypatch.setattr(isolation_monitor, "on_level_change", pinned.on_level_change)
    monkeypatch.setattr(runner_broker, "acquire", passthrough_acquire)
    monkeypatch.setattr(agent_command, "build_command_profile", passthrough_command_profile)
    monkeypatch.setattr(evolution_command, "evolution_profile", passthrough_evolution_profile)
    monkeypatch.setattr(mcp_launcher, "plan_server_launch", passthrough_mcp_launch)
    monkeypatch.setattr(mcp_launcher, "candidate_launch", passthrough_mcp_launch)
    return pinned
