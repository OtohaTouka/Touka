"""Custom model layer for ToukaProxy."""

import logging
from typing import Any

from src.middleware.base import ProxyLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services.custom_model_service import custom_model_service
from src.services.prompt_assembler import prompt_assembler

logger = logging.getLogger(__name__)

class CustomModelLayer(ProxyLayer):
    """負責「自定義模型 (Level 2)」的參數與 Prompt 注入。

    與 SoulLayer 隔離，僅處理 custom/ 目錄下的自定義模型。
    """
    async def process(self, request: Any, ctx: PipelineContext) -> None:
        """Process custom model layer parameters."""
        model_name = getattr(request, "model", "")
        # 靈魂請求交給 SoulLayer：同名自訂模型不得改寫已授權的靈魂目標（ADR-0245 修補）
        if not model_name or ctx.get("is_toukasoul"):
            return

        # 1. 檢查是否為自定義模型 (Level 2)
        # 我們檢查 custom/ 目錄下是否存在此 ID 的資料夾
        temp_identity = AgentIdentity(agent_id=model_name, agent_role="Base")
        prompt_text, config = custom_model_service.get_model_data(temp_identity)
        
        if not prompt_text and not config:
            # 不是自定義模型，跳過
            return

        logger.info(f"🎨 Custom Model (Level 2) detected: {model_name}")
        
        # 2. 注入參數
        # 替換基礎模型
        base_model = config.get("base_model")
        if base_model:
            request.model = base_model
            logger.debug(f"  -> Swapped base model: {base_model}")

        # 注入推理參數（Temperature, TopP, TopK）
        for param in ["temperature", "top_p", "top_k", "max_tokens"]:
            if param in config:
                if hasattr(request, param):
                    setattr(request, param, config[param])
                else:
                    logger.debug(f"  ! Request object does not support parameter: {param}")

        # 3. 標記狀態與工具意圖
        ctx["is_custom"] = True
        ctx["agent_id"] = model_name

        # 4. 提取工具意圖與組裝 capabilities (SSOT: config.json)
        enabled_tools = config.get("enabled_tools", [])
        if enabled_tools:
            ctx["desired_tools"] = enabled_tools
            logger.info(f"  -> Agent desired tools: {ctx['desired_tools']}")
        
        # 5. 注入 System Prompt ( Personality + Capabilities )
        # 這裡將人格與能力技術規範組裝
        capabilities_prompt = prompt_assembler.assemble_capabilities(enabled_tools)
        
        final_system_prompt = prompt_text or ""
        if capabilities_prompt:
            final_system_prompt = f"{final_system_prompt}\n\n{capabilities_prompt}".strip()

        if final_system_prompt and hasattr(request, "messages"):
            # 如果是 Anthropic (撇除 system 欄位)，或原生 OpenAI 格式
            is_anthropic = "anthropic" in str(type(request)).lower()
            
            if is_anthropic:
                # Anthropic 注入到 system 欄位
                current_system = getattr(request, "system", "")
                if current_system:
                    request.system = f"{final_system_prompt}\n\n{current_system}"
                else:
                    request.system = final_system_prompt
            else:
                # OpenAI 格式注入到 messages 陣列最前方
                new_msg = {"role": "system", "content": final_system_prompt}
                request.messages.insert(0, new_msg)
        
        # 注入安全設定（如果有）
        if "safety_settings" in config:
            request.safety_settings = config["safety_settings"]
