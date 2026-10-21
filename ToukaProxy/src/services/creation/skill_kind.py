"""ADR-0069 Skill 產物類型——委派既有 SkillService（``config/skills`` SSOT）。"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from src.services import skill_parser
from src.services.creation.models import (
    CommitResult,
    EvalTask,
    SmokeReport,
    ValidationReport,
)
from src.services.mcp_hub_service import MCPHubService
from src.services.skill_service import skill_service

if TYPE_CHECKING:
    from src.services.creation.kinds import CreationLaunchContext
    from src.services.creation.models import SkillSpec
    from src.services.skill_service import SkillService

_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EVAL_N_MAX = 20


class SkillCreationKind:
    """Skill 的 VALIDATE / COMMIT；COMMIT 委派 ``SkillService.update_skill``。"""

    def __init__(self, service: SkillService | None = None) -> None:
        self._service = service or skill_service

    def validate(self, spec: SkillSpec) -> ValidationReport:
        """檢查 id 格式、description 非空，且 id 不可覆寫既有 skill。"""
        errors: list[str] = []
        valid_id = bool(_KEBAB_RE.match(spec.id))
        if not valid_id:
            errors.append(f"id {spec.id!r} 必須為 kebab-case（小寫字母/數字/連字號）")
        if not spec.description.strip():
            errors.append("description 不可為空")
        if valid_id and self._service.get_skill(spec.id) is not None:
            errors.append(f"skill id {spec.id!r} 已存在，創建不可覆寫既有產物")
        return ValidationReport(ok=not errors, errors=errors)

    async def smoke(
        self, spec: SkillSpec, context: CreationLaunchContext | None = None,
    ) -> SmokeReport:
        """乾跑（不執行碼）確認產出的 SKILL.md 健全。

        序列化後 parse 回來確認 round-trip 無損、frontmatter/body 完整；
        宣告的 mcp_servers 未註冊則列警告（不擋）。
        """
        del context
        frontmatter = skill_parser.build_frontmatter(
            name=spec.id,
            description=spec.description,
            display_name=spec.name or spec.id,
            category=spec.category,
            author=spec.author,
            is_system=spec.is_system,
            allowed_tools=spec.required_tools or None,
        )
        content = skill_parser.serialize_skill_md(frontmatter, spec.system_prompt)
        fm2, body2 = skill_parser.parse_skill_md(content)
        if fm2.get("name") != spec.id or body2 != spec.system_prompt:
            return SmokeReport(ok=False, detail="SKILL.md round-trip 失真")
        if not fm2.get("description"):
            return SmokeReport(ok=False, detail="frontmatter description 遺失")
        warnings: list[str] = []
        if spec.mcp_servers:
            registered = MCPHubService().get_server_names()
            missing = [s for s in spec.mcp_servers if s not in registered]
            if missing:
                warnings.append(f"宣告的 MCP server 尚未註冊：{missing}")
        return SmokeReport(ok=True, detail="skill 乾跑通過", warnings=warnings)

    def commit(self, spec: SkillSpec, context: CreationLaunchContext | None = None) -> CommitResult:
        """組裝 skill_data 並委派 ``update_skill``（寫檔 + 同步技能調度員）。"""
        del context
        skill_data = {
            "name": spec.name or spec.id,
            "display-name": spec.name or spec.id,
            "description": spec.description,
            "category": spec.category,
            "author": spec.author,
            "is_system": spec.is_system,
            "system_prompt": spec.system_prompt,
            "required_tools": spec.required_tools,
            "mcp_servers": spec.mcp_servers,
        }
        self._service.update_skill(spec.id, skill_data)
        return CommitResult(
            id=spec.id, status="created", artifact=f"config/skills/{spec.id}/SKILL.md"
        )

    async def probe_eval_inputs(
        self, spec: SkillSpec, context: CreationLaunchContext | None = None,
    ) -> list:
        """Skill 的 EVAL 素材＝宣告的 required_tools（無需動態探測）。"""
        del context
        return list(spec.required_tools)

    def generate_eval_tasks(self, spec: SkillSpec, probes: list) -> list[EvalTask]:
        """每工具一題唯讀使用驗證 ＋ 一題描述觸發題（決定性、不執行）。"""
        tasks: list[EvalTask] = []
        for i, tool in enumerate(probes[:_EVAL_N_MAX], 1):
            tasks.append(
                EvalTask(
                    id=f"{spec.id}-tool-{i}",
                    title=f"技能使用工具 {tool}",
                    probe=str(tool),
                    expectation=f"當任務需要 '{tool}' 時，技能 '{spec.id}' 會以唯讀方式選用它。",
                    description=f"依描述：{spec.description[:160]}",
                )
            )
        tasks.append(
            EvalTask(
                id=f"{spec.id}-desc",
                title="技能觸發與意圖",
                expectation=f"提示符合描述意圖時，技能 '{spec.id}' 會被選用且不執行破壞性動作。",
                description=spec.description or spec.system_prompt[:200],
            )
        )
        return tasks
