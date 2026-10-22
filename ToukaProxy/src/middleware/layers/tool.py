"""工具裝配與動態權限過濾中介軟體層。"""

import logging
from typing import Any

from src.adapters import tool_schema
from src.middleware.base import ProxyLayer
from src.middleware.layers.context import (
    inject_anthropic_system,
    inject_openai_system,
    render_mission_block,
)
from src.models.advertised_tools import AdvertisedTools
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext
from src.services.agent_service import agent_service
from src.services.permission_service import permission_service
from src.services.prompt_blocks import (
    TOOL_AVAILABILITY,
    build_harness_context,
    build_validation_protocol,
    wrap_block,
)
from src.tools.builtin import builtin_tools_schema
from src.tools.builtin.schemas import (
    ACTIVATE_SKILL_SCHEMA,
    CONNECT_MCP_SERVER_SCHEMA,
)
from src.tools.file_verbs import INCOGNITO_FILE_VERB_SCHEMAS
from src.utils.auth_mode import (
    enforce_incognito_manual,
    mode_for_acting_agent,
    normalize_auth_mode,
    request_is_incognito,
)
from src.utils.presence import is_human_present

logger = logging.getLogger(__name__)

# P2a：改動類（mutating）開發工具——廣告集含其一即注入 <validation_protocol>
# 慣例段（開發流程才提示自我驗證，不擾唯讀/一般 persona 對話）。
_MUTATING_DEV_TOOLS = frozenset({
    "write", "edit", "insert", "delete",
    "execute_command",
    "evolution_define", "evolution_implement_and_verify",
    "edit_soul",
})

class ToolLayer(ProxyLayer):
    """負責動態裝配模型可使用的工具 (Builtin, Memory, Plugins) (SOLID - SRP)。

    實作動態權限管理 (Tiered Permissions) 與分級授權 (AuthLevel)。
    """

    async def process(self, request: Any, ctx: PipelineContext) -> None:
        """組裝並過濾本次請求可用的工具 schema（Gate 1）。"""
        # 優先從 Context 獲取已驗證的身分實體 (SSOT)
        identity = ctx.get("identity")
        agent_id = ctx.get("agent_id", "Unknown")
        is_custom = ctx.get("is_custom", False)

        # 降級相容（針對舊路徑）
        if not isinstance(identity, AgentIdentity):
            agent_info = agent_service.identify_agent(agent_id) or {"role": "Base"}
            identity = AgentIdentity.for_role(
                agent_id, agent_info.get("role", "Base"), capabilities=[]
            )

        session_id = ctx.get("session_id")
        desired_tools = ctx.get("desired_tools")
        
        # 1. 獲取 Session 的模式與已掛載的 MCP (SSOT)；舊值讀取時對照（ADR-0246）
        auth_level = "Manual"
        active_session_mcps = []
        if session_id:
            from src.utils.async_db import session_service
            session = await session_service.get_session_by_id(session_id)
            if session:
                # 存下的 Auto／YOLO 只給 session 綁的 agent：和這次執行的 agent（前景代打後的
                # ctx agent_id，ADR-0154）用正式 ID 比，不一致或判斷不了一律 Manual（R-2c）
                bound = session.get("agent_id")
                passport = agent_service.identify_agent(bound) if bound else None
                auth_level = mode_for_acting_agent(
                    session.get("auth_level"),
                    bound_agent=passport["agent_id"] if passport else bound,
                    acting_agent=agent_id,
                )
                active_session_mcps = session.get("active_mcp_servers", [])
        elif ctx.get("subagent_run"):
            # 子代理沒有 session：繼承父代理的模式（組裝與執行一致，D_permissions §1.9-2）；
            # 子代理一律無人在場，要人核准的動作照樣跑不了（U2c-2）。
            auth_level = normalize_auth_mode(ctx.get("auth_level"))
        if ctx.get("heartbeat_run"):
            # 心跳保留設定時決定的模式（ADR-0246 心跳）：家對話共用人類的 session，人類選的
            # 模式不套到無人的排程 run；看不懂一律 Manual。
            auth_level = normalize_auth_mode(ctx.get("auth_level"))
        # 無痕對話一律 Manual（ADR-0246）：蓋過上面任何來源（存檔、心跳、子代理繼承）
        incognito = bool(ctx.get("incognito")) or request_is_incognito(request)
        auth_level = enforce_incognito_manual(auth_level, incognito=incognito)

        ctx["auth_level"] = auth_level
        ctx["session_active_mcp"] = active_session_mcps
        skill_required_mcp: list[str] = ctx.get("skill_required_mcp") or []

        # 2. 獲取基礎分配工具清單 (Tiered Permissions)
        allowed_tools = permission_service.get_allowed_tools(identity)
        
        # 3. 檢測是否有「動態調度員」技能 (Dispatcher Logic)
        # ADR-0161：MCP 動態掛載改由輸入列 toggle（ctx.mcp_dispatcher，逐則旗標）啟用——
        # is_system 系統技能自技能選單移出後，此為使用者面唯一啟用入口。保留舊
        # active_skills 路徑向後相容（既存 session 曾勾 system-mcp-dispatcher 者仍生效）。
        active_skill_ids = ctx.get("active_skills", [])
        is_dispatcher_active = (
            "system-mcp-dispatcher" in active_skill_ids
            or bool(ctx.get("mcp_dispatcher", False))
        )
        
        if request.tools is None:
            request.tools = []
        # 軌C canonical：工具全程維持 OpenAI 形狀（客戶端若送 Anthropic 工具於此正規化）。
        request.tools = tool_schema.anthropic_to_openai_tools(request.tools) or []

        # 4. 準備工具清單 (透過 PaC 引擎過濾)
        # Tier 1（可讀拒絕）：政策 DENY 不再無聲——記錄名稱＋原因，稍後注入
        # <tool_availability> 說明（模型不再幻覺被過濾的工具）。
        active_builtin_schemas = []
        denied_tools: list[dict[str, str]] = []
        builtin_filtered = False
        for schema in builtin_tools_schema:
             tool_name = schema["function"]["name"]
             # G10（ADR-0085）：ask_user 僅互動式（UI 對話）請求注入——心跳/SNS 等自主情境
             # 無真人可問，注入會導致逾時掛住。resumable_stream 旗標僅 proxy.py 的 UI 端點設。
             if tool_name == "ask_user" and not is_human_present(ctx):
                 continue
             if incognito:  # 無痕：檔案動詞換成沒有 legacy 的版本（hist-bak b）
                 schema = INCOGNITO_FILE_VERB_SCHEMAS.get(tool_name, schema)

             eval_ctx = PolicyContext(
                 actor=identity,
                 resource=tool_name,
                 action="schema_injection",
                 environment={"auth_level": auth_level, "session_id": session_id}
             )
             decision = permission_service.evaluate_policy(eval_ctx)

             if decision.effect == "DENY":
                 denied_tools.append(
                     {"tool": tool_name, "reason": decision.reason or "policy"}
                 )
                 builtin_filtered = True
                 continue

             if permission_service.is_tool_allowed(tool_name, allowed_tools):
                 active_builtin_schemas.append(schema)
             else:
                 builtin_filtered = True
        
        # 5. 注入調度工具 (MCP 掛載 + 技能啟用)
        if is_dispatcher_active:
            active_builtin_schemas.append(CONNECT_MCP_SERVER_SCHEMA)
        if "system-skill-dispatcher" in active_skill_ids:
            active_builtin_schemas.append(ACTIVATE_SKILL_SCHEMA)
              
        # 6.（Tier 3）project_tools 條件注入已退役：統一動詞常駐廣告，scope=project
        #    於未選專案時由執行層回可讀錯誤（結構化、模型可自我修正）。

        # 7. 處理記憶工具 (Memory)
        # ADR-0086：無痕模式完全失憶——不注入記憶工具，AI 無從讀寫記憶。
        memory_tools_schema = [] if ctx.get("incognito") else ctx.get("memory_tools_schema", [])
        active_memory_schemas = []
        for s in memory_tools_schema:
            t_name = s["function"]["name"]
            if permission_service.is_tool_allowed(t_name, allowed_tools):
                active_memory_schemas.append(s)
 
        # 8. 整合基礎工具
        combined_schemas = active_builtin_schemas + active_memory_schemas
        
        # 9. 處理 MCP 工具
        protocol_manager = ctx.get("protocol_manager")
        if protocol_manager:
            if is_custom and desired_tools:
                registry = getattr(protocol_manager, "registry", None)
                if registry:
                    for t_name in desired_tools:
                        server_id = registry.find_server_by_tool_name(t_name)
                        if server_id:
                            is_authorized = permission_service.is_mcp_server_allowed(
                                server_id, identity
                            )
                            if is_authorized:
                                await protocol_manager.ensure_connected(server_id)

            # 9a. 技能所需的 MCP 伺服器 — 主動確保連線 (SRP: SkillLayer 宣告, ToolLayer 執行)
            if skill_required_mcp:
                _registry = getattr(protocol_manager, "registry", None)
                for _srv in skill_required_mcp:
                    if _registry and not _registry.get_server(_srv):
                        logger.warning(
                            f"ToolLayer: Skill MCP '{_srv}' not in registry, skipping."
                        )
                        continue
                    if permission_service.is_mcp_server_allowed(_srv, identity):
                        await protocol_manager.ensure_connected(_srv)
                    else:
                        logger.warning(
                            f"ToolLayer: Skill MCP '{_srv}' RBAC denied for {identity.agent_id}."
                        )

            # 軸 A（per-installation）已於 get_all_tools 供給面濾除，此處只做軸 B（身分面）。
            mcp_tools = await protocol_manager.get_all_tools()
            for mcp_tool in mcp_tools:
                server_name = mcp_tool.get("server", "Unknown")

                show_tool = False
                t_name = mcp_tool.get("function", {}).get("name") or mcp_tool.get("name")
                # 裸名由 get_all_tools 投影（ADR-0195）；缺席時退回廣告名（ToukaUI 等非 MCP
                # 條目本就無前綴），不在此 split("__") 重刻切法。
                bare_tool = mcp_tool.get("tool") or t_name
                is_authorized = permission_service.is_mcp_tool_allowed(
                    server_name, bare_tool, identity
                )

                if is_custom and desired_tools and t_name in desired_tools and is_authorized:
                    show_tool = True
                elif is_dispatcher_active:
                    if is_authorized and (
                        server_name in active_session_mcps or server_name in skill_required_mcp
                    ):
                        show_tool = True
                else:
                    if is_authorized:
                        show_tool = True
                
                if show_tool:
                    mcp_tool["category"] = "MCP"
                    combined_schemas.append(mcp_tool)
        
        # 11. 最終過濾
        if desired_tools:
            essential_tools = ["connect_mcp_server", "agent_control", "search_skill_market"]
            if is_custom:
                essential_tools = [t for t in essential_tools if t in desired_tools]
            
            final_schemas = []
            for s in combined_schemas:
                t_name = s.get("function", {}).get("name") or s.get("name")
                if t_name in desired_tools or t_name in essential_tools:
                    final_schemas.append(s)
            combined_schemas = final_schemas

        # 這一輪實際列給模型的伺服器端工具（去重後；被客戶端同名 schema 蓋掉的不算）。
        # 一律覆寫：心跳與子代理組裝共用 ctx，舊清單不得沿用。
        ctx["advertised_tools"] = AdvertisedTools.from_schemas(
            self._inject_tools(request, combined_schemas)
        )

        # 11b. Tier 1（可讀拒絕）：有任何 builtin 被過濾時，記錄到 ctx 並注入說明。
        # 注意：pipeline 中 ContextLayer 先於 ToolLayer 執行，故說明由本層自行注入。
        if denied_tools:
            ctx["denied_tools"] = denied_tools
        if denied_tools or builtin_filtered:
            ctx["tools_filtered"] = True
            self._inject_availability_note(request, denied_tools)

        # 11c. P2a 驗證慣例：廣告集含 mutating dev verb（開發流程）時注入自我驗證段
        # （唯讀/一般 persona 對話無改動類工具→不注入，避免無謂 token 與語氣干擾）。
        advertised = {
            (t.get("function", {}) or {}).get("name")
            for t in (request.tools or [])
            if isinstance(t, dict)
        }
        if advertised & _MUTATING_DEV_TOOLS:
            # H4：Manual 句依「有無審批通道」分寫——互動會話（resumable_stream）走
            # 審批卡（H2/H3 broker），headless 則 fail-closed 拒絕；提示與行為對齊。
            self._inject_validation_protocol(
                request, auth_level, interactive=is_human_present(ctx)
            )

        # 11d. OS 環境事實（ADR-0139）：toukasoul 面注入 <harness_context>——soul 管
        # 「我是誰」、本塊管「我在哪」（模型／角色／機制），內容按實際廣告面條件化；
        # 子代理（_assemble_subagent_request 設 is_toukasoul=False）與 L2 直通不注入。
        if ctx.get("is_toukasoul"):
            self._inject_harness_context(request, ctx, advertised)

        # 12. --- 任務感知注入 (Mission Awareness) ---
        # pipeline 中 ContextLayer 早於本層執行，無法讀取本層此刻才備妥的 mission
        # （舊實作僅寫入 ctx["mission_awareness"]，區塊因而永不注入——靜默流失）。
        # 比照 _inject_availability_note：由本層自行注入 <mission_awareness>。
        try:
            from memory_server.services.mission_manager import MissionManager

            mission_manager = MissionManager()
            # 傳遞完整的 identity 物件 (SSOT)
            mission = await mission_manager.get_current_mission(identity)
            if mission and mission.status != "DONE":
                ctx["mission_awareness"] = mission  # 保留於 ctx 供觀測／其他消費者
                self._inject_mission_awareness(request, mission)
                logger.info(f"ToolLayer: Active mission injected for {identity.agent_id}")
        except Exception:
            logger.exception("Failed to prepare mission awareness data")

        return

    def _inject_mission_awareness(self, request: Any, mission: Any) -> None:
        """把 <mission_awareness> 區塊直接注入 system（本層晚於 ContextLayer，須自注入）。"""
        block = render_mission_block(mission)
        if "anthropic" in str(type(request)).lower():
            inject_anthropic_system(request, block)
            return
        messages = getattr(request, "messages", None)
        if messages is not None:
            inject_openai_system(messages, block)

    def _inject_availability_note(self, request: Any, denied: list[dict[str, str]]) -> None:
        """把 Gate 1 過濾的可讀說明注入 system（防止模型幻覺被過濾的工具）。"""
        lines: list[str] = []
        if denied:
            lines.append("以下工具存在但本會話不可用（政策限制）——請勿呼叫、也不要聲稱已執行：")
            lines.extend(f"- {d['tool']}（原因：{d['reason']}）" for d in denied)
        lines.append("提示：工具清單以實際提供者為準；請只使用清單內的工具。")
        note = "\n" + wrap_block(TOOL_AVAILABILITY, "\n".join(lines)) + "\n"

        if "anthropic" in str(type(request)).lower():
            inject_anthropic_system(request, note)
            return
        messages = getattr(request, "messages", None)
        if messages is not None:
            inject_openai_system(messages, note)

    def _inject_validation_protocol(
        self, request: Any, auth_level: str, *, interactive: bool = True
    ) -> None:
        """把 <validation_protocol> 慣例段注入 system（P2a；積極度依審批模式）。"""
        block = build_validation_protocol(auth_level, interactive=interactive)
        if "anthropic" in str(type(request)).lower():
            inject_anthropic_system(request, block)
            return
        messages = getattr(request, "messages", None)
        if messages is not None:
            inject_openai_system(messages, block)

    def _inject_harness_context(
        self, request: Any, ctx: PipelineContext, advertised: set
    ) -> None:
        """把 <harness_context>（OS 環境事實）注入 system（ADR-0139）。

        model 取 SoulLayer 解析後的 ``request.model``（本層晚於 SoulLayer）；
        advertised 為 Gate-1 過濾後的實際廣告面（含 None 無妨，僅做成員檢查）。
        """
        identity = ctx.get("identity")
        block = build_harness_context(
            model=str(getattr(request, "model", "") or ""),
            role=str(identity.display_role if identity else ""),
            advertised=frozenset(advertised),
        )
        if "anthropic" in str(type(request)).lower():
            inject_anthropic_system(request, block)
            return
        messages = getattr(request, "messages", None)
        if messages is not None:
            inject_openai_system(messages, block)

    def _inject_tools(self, request: Any, schemas: list) -> list:
        """注入工具（canonical OpenAI schema，軌C）；以 function name 去重，回傳實際注入的。

        request.tools 全程維持 OpenAI 形狀；各供應商所需的轉換只在出場做一次
        （Claude→Anthropic 於 claude_handler、Gemini 於 openai_to_gemini、OpenAI 直通）。
        撞名去重不再無聲（Tier 1）：丟棄重複定義時記 warning。
        """
        injected: list = []
        current_tools = request.tools or []
        names = {
            t.get("function", {}).get("name")
            for t in current_tools if isinstance(t, dict) and "function" in t
        }
        for s in schemas:
            t_name = s.get("function", {}).get("name")
            if not t_name:
                continue
            if t_name in names:
                logger.warning(
                    "ToolLayer: duplicate tool name '%s' dropped (first definition wins).",
                    t_name,
                )
                continue
            request.tools.append(s)
            injected.append(s)
            names.add(t_name)
        return injected
