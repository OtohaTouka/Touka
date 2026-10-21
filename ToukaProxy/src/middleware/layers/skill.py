"""技能中介層模組 (Pull 模式，ADR-0066 Phase 3)。"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.middleware.base import ProxyLayer
from src.models.identity import AgentIdentity
from src.services.permission_service import permission_service
from src.services.skill_service import skill_service
from src.utils.async_db import session_service

if TYPE_CHECKING:
    from src.models.context import PipelineContext

logger = logging.getLogger(__name__)

# 技能調度員 ID (Pull 模式：常駐注入，讓 Agent 看到可用技能清單)
_SKILL_DISPATCHER_ID = "system-skill-dispatcher"

# 深入研究協調 Skill ID（ADR-0087）：由 deep_research 旗標觸發強制注入
_DEEP_RESEARCH_SKILL_ID = "deep-research-coordinator"


class SkillLayer(ProxyLayer):
    """負責解析與注入「技能 (Skills)」(Pull 模式)。

    技能來源 = header (使用者手動選) ∪ session.active_skills (Agent 透過
    activate_skill 啟用 / pulse 場景)，並常駐注入 system-skill-dispatcher
    讓 Agent 看到可用技能清單。技能同時影響 System Prompt 與工具白名單。
    """

    async def process(self, request: Any, ctx: PipelineContext) -> None:
        """合併技能來源、RBAC 過濾、常駐注入調度員，並收集所需工具與 MCP。"""
        identity = ctx.get("identity")
        if not isinstance(identity, AgentIdentity):
            # 內部無身分路徑（如 SNS）：不捏造身分名稱；無 agent_role ＝ 零技能授權。
            identity = AgentIdentity(agent_id=ctx.get("agent_id") or "")

        # 1. 技能來源：header (手動選) ∪ session.active_skills (activate_skill / pulse)
        headers = ctx.get("headers", {})
        skill_header = headers.get("x-touka-skills", "")
        header_ids = [s.strip() for s in skill_header.split(",") if s.strip()]
        session_ids = await self._get_session_active_skills(ctx)
        requested_ids = list(dict.fromkeys(header_ids + session_ids))
        # P4（ADR-0161）：剔除手動勾選的 system-skill-dispatcher——其 body 由下方
        # has_dispatcher 路徑（dispatcher_body）統一注入；若又走手動授權路徑會被
        # apply_skills_to_request 再注一次＝同段調度員提示詞在 <capabilities> 雙重注入
        # （浪費 token）。**不可**一併剔 system-mcp-dispatcher（那是 MCP 動態掛載的啟用
        # 旗標，需保留進 active_skills 供 tool.py 讀；ADR-0161 後新啟用走 ctx.mcp_dispatcher，
        # 但既存 session 的 active_skills 仍靠此保留向後相容）。
        requested_ids = [s for s in requested_ids if s != _SKILL_DISPATCHER_ID]

        # 2. 過濾使用者/Agent 指定的技能：須「存在」且通過 RBAC。
        #    存在性過濾擋掉不存在的技能 id 混入 active_skills——allowed_skills=["*"] 下尤需
        #    （否則任意字串都被視為「已授權」）。system-skill-dispatcher 另於下方常駐注入。
        authorized_skill_ids = []
        for sid in requested_ids:
            if not skill_service.get_skill(sid):
                logger.warning("🛡️ Skill Skipped: requested skill `%s` not found.", sid)
                continue
            if permission_service.can_use_skill(identity, sid):
                authorized_skill_ids.append(sid)
            else:
                logger.warning(
                    "🛡️ Skill Blocked: Agent `%s` not authorized for skill `%s`.",
                    identity.agent_id,
                    sid,
                )

        # 2.5 Deep Research（ADR-0087）：旗標啟用時強制注入研究協調 Skill（提示詞層）。
        #     實際 research 工具仍受 get_allowed_tools(identity) RBAC 把關，無越權
        #     （skill_required_tools 從不授予工具，僅供 SkillLayer 收集所需 MCP）。
        if ctx.get("deep_research") and _DEEP_RESEARCH_SKILL_ID not in authorized_skill_ids:
            authorized_skill_ids.append(_DEEP_RESEARCH_SKILL_ID)

        # 3. 技能調度員（廣告面）——ADR-0148：依 can_use_skill 過濾，廣告面＝授權面。
        #    算出此身分「用得到」的技能集合；唯有非空才注入 dispatcher（連帶讓 tool.py
        #    廣告 activate_skill）＋只列 usable 技能。無可用技能的角色（如 Base/Subagent，
        #    allowed_skills=[]）自此**看不到** skill_registry 也拿不到 activate_skill，
        #    杜絕「看得到用不了」（舊制廣告全 catalog、activate 批准後才撞 RBAC）。
        usable_skill_ids = [
            s["id"]
            for s in skill_service.list_skills()
            if not s.get("is_system") and permission_service.can_use_skill(identity, s["id"])
        ]
        has_dispatcher = bool(usable_skill_ids)

        # 4. active_skills：已授權業務技能 ＋（僅在有可用技能時）dispatcher
        inject_ids = list(authorized_skill_ids)
        if has_dispatcher:
            inject_ids.append(_SKILL_DISPATCHER_ID)
        ctx["active_skills"] = inject_ids

        # 5. 注入 System Prompt：已授權技能 body ＋ RBAC 過濾後的 skill_registry
        #    （取代舊的靜態全 catalog dispatcher body）。registry 以 dispatcher_body
        #    併入同一個 <capabilities> 區塊（結構同舊制，context_breakdown 歸因不變）。
        dispatcher_body = (
            skill_service.render_skill_registry(usable_skill_ids) if has_dispatcher else None
        )
        skill_service.apply_skills_to_request(
            request, authorized_skill_ids, dispatcher_body=dispatcher_body or None
        )

        # 5 & 6. 收集所需工具與 MCP (僅來自已授權業務技能，不含系統 dispatcher)
        skill_tools: list[str] = []
        skill_mcp_servers: list[str] = []
        for sid in authorized_skill_ids:
            skill = skill_service.get_skill(sid)
            if not skill:
                continue
            if skill.get("required_tools"):
                skill_tools.extend(skill["required_tools"])
            if skill.get("mcp_servers"):
                skill_mcp_servers.extend(skill["mcp_servers"])

        if skill_tools:
            ctx["skill_required_tools"] = list(dict.fromkeys(skill_tools))
            _n_tools = len(ctx["skill_required_tools"])
            logger.info("SkillLayer: %d required tools identified.", _n_tools)

        if skill_mcp_servers:
            ctx["skill_required_mcp"] = list(dict.fromkeys(skill_mcp_servers))
            _n_mcp = len(ctx["skill_required_mcp"])
            logger.info("SkillLayer: %d required MCP servers identified.", _n_mcp)

    async def _get_session_active_skills(self, ctx: PipelineContext) -> list[str]:
        """從 session 讀取 active_skills (activate_skill 寫入 / pulse 場景來源)。"""
        session_id = ctx.get("session_id")
        if not session_id:
            return []
        try:
            session = await session_service.get_session_by_id(session_id)
        except Exception:
            logger.exception("SkillLayer: Failed to read session active_skills.")
            return []
        if session:
            return session.get("active_skills", []) or []
        return []
