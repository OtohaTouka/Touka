"""靈魂服務 (SoulService) 模組.

負責 Touka 系統中代理人 (Agent) 靈魂資料 (System Prompt) 與實體設定 (config.json) 的加載,
並執行請求攔截與人格注入 (Soul Sanctuary).
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from memory_server import config_utils
from memory_server.utils.historical_data import (
    HISTORICAL_DATA_CLOSE,
    historical_data_open,
    neutralize_historical_tags,
    wrap_historical_data,
)

from src.adapters.content_normalize import normalize_message_content
from src.adapters.schemas import OpenAIMessage
from src.models.identity import AgentIdentity
from src.services.prompt_blocks import CONVERSATION_SUMMARY, PERSONA, wrap_block

logger = logging.getLogger(__name__)


def _map_text(content: Any, fn: Callable[[str], str]) -> Any:
    """對一則訊息內容的文字套用 ``fn``：字串直接套，多模態 list 套在每個 text part。"""
    if isinstance(content, str):
        return fn(content)
    if isinstance(content, list):
        return [
            {**p, "text": fn(p["text"])}
            if isinstance(p, dict) and isinstance(p.get("text"), str)
            else p
            for p in content
        ]
    return content


def _add_text(content: Any, text: str, *, before: bool) -> Any:
    """在一則訊息內容前或後加一段文字；多模態 JSON 字串先還原成 list、加一個 text part。"""
    content = normalize_message_content(content)
    if isinstance(content, list):
        part = {"type": "text", "text": text}
        return [part, *content] if before else [*content, part]
    return f"{text}\n{content or ''}" if before else f"{content or ''}\n{text}"


def _mark_home_history(home_msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """家對話第一則前加歷史資料界線開頭、最後一則後加結尾：只加文字，角色與則數不變。

    先複製再改，不動到讀出來的 session 樹；每則文字裡的界線 tag 先中和。
    """
    marked = [
        {**m, "content": _map_text(m.get("content"), neutralize_historical_tags)}
        for m in home_msgs
    ]
    opening = historical_data_open("home_conversation")
    marked[0]["content"] = _add_text(marked[0]["content"], opening, before=True)
    marked[-1]["content"] = _add_text(marked[-1]["content"], HISTORICAL_DATA_CLOSE, before=False)
    return marked


def _after_leading_system(messages: list[Any]) -> int:
    """開頭連續 system 訊息之後的位置（第一則就不是 system＝0）。"""
    index = 0
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role != "system":
            break
        index += 1
    return index


class SoulService:
    """代理人人格與設定管理服務.

    實施「靈魂庇護所 (Soul Sanctuary)」機制, 確保代理人的核心人格與設定
    在任何管道的請求中都能精確覆蓋與保持 SSOT.
    """

    def __init__(self) -> None:
        """初始化靈魂服務."""
        pass

    @property
    def base_dir(self) -> Path:
        """此屬性已廢棄，請使用 resource_manager 獲取路徑。"""
        msg = "🚫 Direct access to soul base_dir is banned. Use resource_manager."
        raise RuntimeError(msg)

    def get_soul_data(self, identity: str | AgentIdentity) -> tuple[str | None, dict]:
        """從物理路徑載入指定 Agent 的靈魂資料與設定檔 (Identity SSOT).
        
        Args:
            identity (str | AgentIdentity): 經驗證的身分對象或原始字串.

        Returns:
            tuple[str | None, dict]: 包含 (System Prompt, Config Dictionary) 的元組.
        """
        agent_id = identity if isinstance(identity, str) else identity.agent_id
        
        app_data_dir = Path(config_utils.get_app_data_dir())
        agent_dir = app_data_dir / "soul" / agent_id
        logger.info(f"🧬 Loading soul for {agent_id} from {agent_dir}")
        
        if not agent_dir.exists():
            logger.warning(f"🧬 Soul directory NOT FOUND: {agent_dir}")
            return None, {}

        soul_path = agent_dir / "soul.md"
        config_path = agent_dir / "config.json"

        from .resource_manager import resource_manager  # noqa: LOCAL_IMPORT_BAN
        system_prompt = None
        if soul_path.exists():
            try:
                system_prompt = resource_manager.read_file_text(soul_path)
            except Exception:
                logger.exception(f"Error reading soul.md for {agent_id}")

        config = {}
        if config_path.exists():
            try:
                config = json.loads(resource_manager.read_file_text(config_path))
            except Exception:
                logger.exception(f"Error reading config.json for {agent_id}")

        return system_prompt, config

    def is_toukasoul(self, identity: str | AgentIdentity) -> bool:
        """判斷指定身分是否具備正式靈魂特徵 (Identity SSOT).
        
        優先信任身分物件中的角色判定。
        
        Args:
            identity (str | AgentIdentity): 經驗證的身分對象或字串.

        Returns:
            bool: 是否為正式靈魂.
        """
        agent_id = identity if isinstance(identity, str) else identity.agent_id
        # ADR-0141：靈魂＝Owner 軀體判定，讀 agent_role（人類 agent_role=None→非 Owner，
        # 與拆分前 human_role 恆非 "Owner" 一致）；字串身分無角色資訊，預設 Base。
        role = "Base" if isinstance(identity, str) else (identity.agent_role or "Base")

        # 1. 優先信任角色判定 (SOLID - SRP)
        if role == "Owner":
            return True
            
        # 2. 檢查物理路徑存在（soul 目錄即判定為靈魂）

        # 3. 檢查物理路徑是否存在
        app_data_dir = Path(config_utils.get_app_data_dir())
        agent_dir = app_data_dir / "soul" / agent_id
        if not agent_dir.exists():
            return False

        # 4. 檢查設定檔中的靈魂標記 (符合 SSOT 原則)
        try:
            _, config = self.get_soul_data(identity)
            return config.get("is_toukasoul", config.get("is_soul", False))
        except Exception:
            return False

    def apply_soul_to_request(self, request: Any, identity: str | AgentIdentity) -> None:
        """將靈魂與設定強制覆蓋至請求中，並保留技術性指令 (Identity SSOT).
        
        Args:
            request (Any): 模型請求對象 (支援 Anthropic/OpenAI 結構).
            identity (str | AgentIdentity): 經驗證的身分對象或原始 ID.
        """
        agent_id = identity if isinstance(identity, str) else identity.agent_id
        soul_text, config = self.get_soul_data(identity)
        if not soul_text and not config:
            return

        # 1. 處理 System Prompt 覆蓋與技術指令保留
        if soul_text and hasattr(request, "messages"):
            # 提取現有的 system 訊息中的技術內容
            technical_parts = []
            new_messages = []

            # 從獨立的 system 欄位 (Anthropic) 或 messages 中提取
            raw_system_content = getattr(request, "system", "")
            if not raw_system_content:
                for msg in request.messages:
                    is_dict = isinstance(msg, dict)
                    role = msg.get("role") if is_dict else getattr(msg, "role", "")
                    if role == "system":
                        content = (
                            msg.get("content") if is_dict else getattr(msg, "content", "")
                        )
                        if content:
                            raw_system_content += "\n" + content

            # 啟發式過濾：尋找工具定義、環境說明等技術關鍵字
            technical_keywords = [
                "tool",
                "function",
                "environment",
                "capability",
                "specification",
                "api",
                "path",
            ]
            if raw_system_content and any(
                kw in raw_system_content.lower() for kw in technical_keywords
            ):
                technical_parts.append(raw_system_content.strip())

            # 過濾掉原本的 system 訊息
            for msg in request.messages:
                role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
                if role != "system":
                    new_messages.append(msg)

            # 組合最終的 System Prompt
            final_soul = soul_text
            if technical_parts:
                final_soul += "\n\n---\n"
                final_soul += (
                    "（系統提示：你還是你，下面只是為了連線到外部工具所必須遵守的技術規範。）\n"
                )
                final_soul += "\n".join(technical_parts)

            # Phase B0（ADR-0092）：外層包 <persona> delimiter，內文逐字不變，
            # 供 context breakdown 以一致 XML 邊界切段。soul 受 AGENT_SOUL_PROTECTION
            # 僅擁有者可改，內容可信，無需轉義。
            final_soul = wrap_block(PERSONA, final_soul)

            # 注入回請求
            if hasattr(request, "system"):
                request.system = final_soul
                request.messages = new_messages
                logger.debug("Applied soul + technical info to Anthropic 'system' field.")
            else:
                is_pydantic = len(new_messages) > 0 and not isinstance(new_messages[0], dict)
                if is_pydantic:
                    new_messages.insert(0, OpenAIMessage(role="system", content=final_soul))
                else:
                    new_messages.insert(0, {"role": "system", "content": final_soul})

                request.messages = new_messages
                logger.debug("Applied soul + technical info to OpenAI-style 'messages'.")

        # 2. 處理參數覆蓋
        if config:
            for key, value in config.items():
                if key in ["is_soul", "is_toukasoul"]:  # 跳過非參數欄位
                    continue
                if hasattr(request, key):
                    setattr(request, key, value)
                elif key == "safety_mode" and hasattr(request, "safety_mode"):
                    request.safety_mode = value

        logger.info(f"🛡️ Soul Sanctuary ACTIVE for {agent_id}: System Prompt & Config Overridden.")

    async def inject_home_messages(self, request: Any, identity: str | AgentIdentity) -> None:
        """為請求注入家園訊息（記憶錨點） (Identity SSOT).
        
        Args:
            request (Any): 模型請求對象.
            identity (str | AgentIdentity): 經驗證的身分對象或原始 ID.
        """
        from src.utils.async_db import get_home_messages  # noqa: LOCAL_IMPORT_BAN

        if isinstance(identity, str):
            identity = AgentIdentity(agent_id=identity)

        agent_id = identity.agent_id
        home_msgs = await get_home_messages(identity)
        if not home_msgs:
            return
        home_msgs = _mark_home_history(home_msgs)  # 歷史資料界線，不是指令

        # 只送角色與內容（ADR-0260）：節點 ID、工具步驟、思考、附件、name 等 UI 樹欄位一律不外送
        formatted_home = [{"role": m["role"], "content": m["content"]} for m in home_msgs]
        is_anthropic = "anthropic" in str(type(request)).lower()
        if is_anthropic:
            request.messages = formatted_home + request.messages
        else:
            # OpenAI style：排在開頭的 system（人格）之後
            at = _after_leading_system(request.messages)
            request.messages = [*request.messages[:at], *formatted_home, *request.messages[at:]]

        logger.info(f"🏠 Injected {len(home_msgs)} home messages for {agent_id}")

    async def inject_context_summary(
        self, request: Any, session_id: str | None
    ) -> None:
        """注入 session 級 ``context_summary`` 為 ``<conversation_summary>`` system 區塊。

        ADR-0092 Phase D：摘要固定前置、**不進對話訊息樹**（故不被 round 計數/再剪）。
        無 session_id 或無摘要則 no-op。呼叫端負責無痕 gating（無痕不注入）。
        """
        if not session_id:
            return
        from src.utils.async_db import session_service  # noqa: LOCAL_IMPORT_BAN
        session = await session_service.get_session_by_id(session_id)
        if not session:
            return
        summary = session.get("context_summary")
        if not summary or not isinstance(summary, str):
            return

        # 歷史資料界線包在 <conversation_summary> 裡面：位置與角色都不變，只加文字
        historical = wrap_historical_data("conversation_summary", summary)
        block = "\n\n" + wrap_block(CONVERSATION_SUMMARY, historical)
        if hasattr(request, "system"):
            if isinstance(request.system, list):
                request.system.append({"type": "text", "text": block})
            else:
                request.system = (request.system or "") + block
        else:
            for m in request.messages:
                role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
                if role == "system":
                    if isinstance(m, dict):
                        m["content"] = (m.get("content") or "") + block
                    elif hasattr(m, "content"):
                        m.content = (m.content or "") + block
                    break
            else:
                request.messages.insert(0, OpenAIMessage(role="system", content=block))
        logger.info("🪟 Injected context_summary for session %s", session_id)


# Global instance
soul_service = SoulService()
