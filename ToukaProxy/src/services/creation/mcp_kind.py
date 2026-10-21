"""ADR-0069 MCP 產物類型——委派既有 MCPHubService（``server_config.json`` SSOT）。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.services.autonomy import autonomy_service
from src.services.creation.models import CommitResult, EvalTask, SmokeReport, ValidationReport
from src.services.creation.naming import server_name as _server_name
from src.services.creation.smoke import list_mcp_tools, smoke_mcp_server
from src.services.mcp_hub_service import MCPHubService
from src.services.skill_service import skill_service
from src.utils.mcp_security import is_command_safe

if TYPE_CHECKING:
    from src.services.creation.kinds import CreationLaunchContext
    from src.services.creation.models import McpSpec

_EVAL_N_MAX = 20


def _smoke_timeout() -> float:
    """從 autonomy_service 的 sandbox_limits 取 SMOKE 逾時秒數，預設 30。"""
    config = getattr(autonomy_service, "config", None)
    limits = config.get("sandbox_limits", {}) if isinstance(config, dict) else {}
    return float(limits.get("execution_timeout_sec", 30))


class McpCreationKind:
    """MCP 的 VALIDATE / COMMIT；COMMIT 委派 ``register_server`` + 同步系統調度員。"""

    def __init__(self, hub_factory: type[MCPHubService] = MCPHubService) -> None:
        self._hub_factory = hub_factory

    def validate(self, spec: McpSpec) -> ValidationReport:
        """檢查 name/command 非空、命令過 shell 注入防護、name 不可覆寫既有 server。"""
        errors: list[str] = []
        name = _server_name(spec.name)
        if not name:
            errors.append("name 不可為空")
        if not spec.command.strip():
            errors.append("command 不可為空")
        elif not is_command_safe(spec.command):
            errors.append(f"command {spec.command!r} 含 shell 注入元字元，已拒絕")
        if name and name in self._hub_factory().get_server_names():
            errors.append(f"MCP server {name!r} 已存在，創建不可覆寫既有產物")
        return ValidationReport(ok=not errors, errors=errors)

    async def smoke(
        self, spec: McpSpec, context: CreationLaunchContext | None = None,
    ) -> SmokeReport:
        """以淨環境啟動候選 MCP server 並握手驗證（委派 smoke_mcp_server）。"""
        if context is None:
            return SmokeReport(ok=False, detail="isolation_unavailable: launch_context_missing")
        return await smoke_mcp_server(
            spec.command, spec.args, spec.env, timeout=_smoke_timeout(),
            name=_server_name(spec.name), context=context,
        )

    def commit(self, spec: McpSpec, context: CreationLaunchContext | None = None) -> CommitResult:
        """寫入單筆 server_config 條目並重新同步系統調度員（DispatchAgent）。"""
        name = _server_name(spec.name)
        self._hub_factory().register_server(
            name, spec.command, spec.args, spec.env,
            origin=context.origin if context else "creation",
        )
        skill_service.sync_system_dispatcher()
        return CommitResult(
            id=name, status="created", artifact="config/server_config.json"
        )

    async def probe_eval_inputs(
        self, spec: McpSpec, context: CreationLaunchContext | None = None,
    ) -> list:
        """MCP 的 EVAL 素材＝唯讀枚舉候選 server 的工具清單（委派 list_mcp_tools）。"""
        if context is None:
            return []
        return await list_mcp_tools(
            spec.command, spec.args, spec.env, timeout=_smoke_timeout(),
            name=_server_name(spec.name), context=context,
        )

    def generate_eval_tasks(self, spec: McpSpec, probes: list) -> list[EvalTask]:
        """每工具一題 schema 驗證任務（唯讀、不執行；上限 _EVAL_N_MAX）。"""
        name = _server_name(spec.name)
        tasks: list[EvalTask] = []
        for i, tool in enumerate(probes[:_EVAL_N_MAX], 1):
            tname = getattr(tool, "name", "") or ""
            tasks.append(
                EvalTask(
                    id=f"{name}-t{i}",
                    title=f"工具 {tname} 結構驗證",
                    probe=tname,
                    expectation=f"server '{name}' 暴露工具 '{tname}'，其 input_schema 與宣告一致。",
                    description=(
                        f"描述對 '{tname}' 的一次安全唯讀呼叫"
                        "（依 schema 填最小參數；本基線不實際執行）。"
                    ),
                )
            )
        return tasks
