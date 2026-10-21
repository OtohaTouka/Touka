"""專案與任務上下文注入中介軟體層。"""

import logging
from typing import Any

from src.adapters.content_normalize import append_text_part
from src.adapters.schemas import OpenAIMessage
from src.middleware.base import ProxyLayer
from src.models.context import PipelineContext
from src.services.prompt_blocks import (
    IDE_CONTEXT,
    MISSION_AWARENESS,
    PROJECT_CONTEXT,
    wrap_block,
)
from src.utils.async_db import get_project_context

logger = logging.getLogger(__name__)


def inject_anthropic_system(request: Any, ctx_str: str) -> None:
    """把上下文文字附掛到 Anthropic 形狀請求的 system（str/list/None 三態）。"""
    if isinstance(request.system, str):
        request.system = (request.system or "") + ctx_str
    elif isinstance(request.system, list):
        request.system.append({"type": "text", "text": ctx_str})
    elif request.system is None:
        request.system = ctx_str


def inject_openai_system(messages: list, ctx_str: str) -> None:
    """把上下文文字附掛到 OpenAI 形狀 messages 的 system（無則插入首位）。"""
    found_sys = False
    for m in messages:
        role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
        if role == "system":
            if isinstance(m, dict):
                m["content"] += ctx_str
            elif hasattr(m, "content"):
                m.content += ctx_str
            found_sys = True
            break
    if not found_sys:
        messages.insert(0, OpenAIMessage(role="system", content=ctx_str))


def render_mission_block(mission: Any) -> str:
    """把 Mission 物件渲染成 <mission_awareness> 區塊文字（ContextLayer / ToolLayer 共用）。

    以 XML 標籤包覆，含大目標、進度比例與逐項任務狀態。ContextLayer 早於 ToolLayer
    執行、無法讀取後者才備妥的 mission，故兩層皆以本函式產生**一致**的區塊文字，
    再各自注入 system（渲染 SSOT，避免格式漂移）。標籤名稱為 MISSION_AWARENESS，
    供 ADR-0092 context breakdown 解析——不得更動。

    Args:
        mission: 具 ``goal`` 與 ``tasks`` 屬性的任務看板物件（MissionState 形狀）。

    Returns:
        以 <mission_awareness> XML 標籤包覆、前後各帶一個換行的區塊文字。
    """
    awareness_block = [
        f"\n<{MISSION_AWARENESS}>",
        "### 📋 目前任務狀態 (Current Mission Progress)",
        f"**大目標**: {mission.goal}",
        f"**進度**: {len([t for t in mission.tasks if t.status == 'DONE'])}"
        f"/{len(mission.tasks)}",
        "---",
    ]
    for t in mission.tasks:
        icon = "✅" if t.status == "DONE" else "⏳" if t.status == "DOING" else "⬜"
        dep = f" (依賴: {', '.join(t.dependencies)})" if t.dependencies else ""
        art = f" [產出: {t.artifact_ref}]" if t.artifact_ref else ""
        awareness_block.append(f"{icon} {t.id}: {t.description}{dep}{art}")
    awareness_block.append(f"</{MISSION_AWARENESS}>\n")
    return "\n".join(awareness_block)


class ContextLayer(ProxyLayer):
    """負責將 IDE、專案與任務看板 (Mission) 上下文注入請求 (SOLID - SRP)。"""

    async def process(self, request: Any, ctx: PipelineContext) -> None:
        """依 ctx 與 request 附帶資訊注入各 context 區塊。"""
        is_anthropic = "anthropic" in str(type(request)).lower()
        
        # 1. 任務看板感知 (Mission Awareness) 注入 ⭐
        # 以標籤包覆，確保 Agent 能區分這是在執行的任務上下文。
        # 註：pipeline 中 ContextLayer 早於 ToolLayer，故生產路徑的 mission 由 ToolLayer
        # 於備妥後自行注入（見 ToolLayer._inject_mission_awareness）；此分支保留供
        # 「ctx 已預先帶有 mission」的呼叫端使用（渲染邏輯統一由 render_mission_block 提供）。
        mission = ctx.get("mission_awareness")
        if mission:
            mission_text = render_mission_block(mission)
            logger.info("ContextLayer: Injected mission awareness block.")

            if is_anthropic:
                self._inject_anthropic_system(request, mission_text)
            else:
                self._inject_openai_system(request.messages, mission_text)

        # 2. IDE Context Injection
        ide_ctx = getattr(request, "ide_context", None)
        if ide_ctx and request.messages:
            file_name = ide_ctx.get("fileName", "Unknown")
            line_num = ide_ctx.get("line", "?")
            context_note = "\n\n" + wrap_block(
                IDE_CONTEXT, f"📄 {file_name} | 📍 Line {line_num}"
            )
            
            for msg in reversed(request.messages):
                role = getattr(msg, "role", None) or (
                    msg.get("role") if isinstance(msg, dict) else None
                )
                if role == "user":
                    self._append_context(msg, context_note)
                    break

        # 3. 專案上下文注入
        project_id = getattr(request, "project_id", None)
        if project_id:
            project_ctx = await get_project_context(project_id)
            if project_ctx:
                logger.info(f"🚀 Injecting project context for project_id={project_id}")
                wrapped_ctx = "\n" + wrap_block(PROJECT_CONTEXT, project_ctx)
                if is_anthropic:
                    self._inject_anthropic_system(request, wrapped_ctx)
                else:
                    self._inject_openai_system(request.messages, wrapped_ctx)

    def _append_context(self, msg: Any, note: str) -> None:
        """比照 continuity：多模態 JSON 字串先還原成 list 再加一段（直接串字串會弄壞圖片）。"""
        is_dict = isinstance(msg, dict)
        updated = append_text_part(
            msg.get("content") if is_dict else getattr(msg, "content", None), note
        )
        if updated is None:
            return
        if is_dict:
            msg["content"] = updated
        else:
            msg.content = updated

    def _inject_anthropic_system(self, request: Any, ctx_str: str) -> None:
        inject_anthropic_system(request, ctx_str)

    def _inject_openai_system(self, messages: list, ctx_str: str) -> None:
        inject_openai_system(messages, ctx_str)
