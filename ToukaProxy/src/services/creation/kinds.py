"""ADR-0069 CreationKind 協定（SOLID-OCP / Strategy）。

每種產物（skill / mcp / 未來 workflow…）提供一個實作本協定的策略物件；
``CreationEngine`` 只編排，新增產物類型不需改引擎（結構化型別，無需繼承）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from src.services.isolation.consent import ExecutionGrant
from src.services.isolation.status import isolation_monitor

if TYPE_CHECKING:
    from src.services.creation.models import (
        CommitResult,
        CreationSpec,
        EvalTask,
        SmokeReport,
        ValidationReport,
    )


@dataclass(frozen=True)
class CreationLaunchContext:
    """整次創建共用的授權；來源由呼叫程式決定，不能從 spec 取得。"""

    grant: ExecutionGrant
    origin: Literal["creation", "scaffold"] = "creation"


async def launch_context(
    origin: Literal["creation", "scaffold"], grant: ExecutionGrant | None = None,
) -> CreationLaunchContext:
    """沒有工具帳本時只取目前層級，不憑空給予降級同意。"""
    if grant is None:
        await isolation_monitor.ensure_ready()
        if origin == "scaffold":
            level = isolation_monitor.local_level().level
        else:
            level = isolation_monitor.snapshot().level
        grant = ExecutionGrant(level)
    return CreationLaunchContext(grant, origin)


class CreationKind(Protocol):
    """產物類型策略：VALIDATE → SMOKE → COMMIT →（後置）EVAL。"""

    def validate(self, spec: CreationSpec) -> ValidationReport:
        """靜態驗證規格；``ok=False`` 時引擎不進入 smoke/commit。"""
        ...

    async def smoke(
        self, spec: CreationSpec, context: CreationLaunchContext | None = None,
    ) -> SmokeReport:
        """試跑候選產物（MCP 握手 / skill 乾跑）；``ok=False`` 時不 commit。"""
        ...

    def commit(
        self, spec: CreationSpec, context: CreationLaunchContext | None = None,
    ) -> CommitResult:
        """原子寫入 + dispatcher 同步；僅在 validate+smoke 通過後呼叫。"""
        ...

    async def probe_eval_inputs(
        self, spec: CreationSpec, context: CreationLaunchContext | None = None,
    ) -> list:
        """EVAL 前置：唯讀蒐集生成評測任務所需素材（MCP→工具清單；skill→工具名）。"""
        ...

    def generate_eval_tasks(self, spec: CreationSpec, probes: list) -> list[EvalTask]:
        """由 spec ＋ probes 決定性生成 N 題可驗證唯讀評測任務（純函式、不執行工具）。"""
        ...
