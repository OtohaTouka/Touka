"""技術規範指令組裝服務 (Prompt Assembler).

定義將已授權工具對應的技術指示 (Technical Specifications) 與限制條件動態組裝成 XML 區塊並注入系統 Prompt 的服務。
"""

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..config import CONFIG_DIR
from .resource_manager import resource_manager

logger = logging.getLogger(__name__)


@runtime_checkable
class PromptAssembler(Protocol):
    """SOLID: DIP - 用於動態組裝指令片段的介面。
    """
    def assemble_capabilities(self, tool_ids: list[str]) -> str:
        """組裝工具對應的技術指示 Prompt 片段.

        Args:
            tool_ids (list[str]): 已啟用或授權的工具/技能 ID 列表。

        Returns:
            str: 組裝完成的 Prompt 區塊字串。
        """
        ...


class ResourcePromptAssembler:
    """從 instruction_templates.json 讀取技術規範並組裝成 XML 區塊。
    """
    def __init__(self, config_path: str = None):
        """初始化組裝服務.

        Args:
            config_path (str, optional): 範本設定檔 (instruction_templates.json) 的實體路徑。
        """
        self.config_path = config_path or Path(CONFIG_DIR) / "instruction_templates.json"
        self._templates: dict = {}
        self._load_templates()

    def _load_templates(self):
        try:
            data = resource_manager.read_json(self.config_path)
            if data:
                self._templates = data
            else:
                logger.warning(f"Instruction templates not found at {self.config_path}")
                self._templates = {"base_constraints": "", "tool_capabilities": {}}
        except Exception as e:
            logger.error(f"Failed to load instruction templates: {e}")
            self._templates = {"base_constraints": "", "tool_capabilities": {}}

    def assemble_capabilities(self, tool_ids: list[str]) -> str:
        """從設定載入技術指示，並組裝成結構化的 XML 區塊.

        Args:
            tool_ids (list[str]): 欲啟用之工具/技能 ID 列表。

        Returns:
            str: 包含 TECHNICAL_SPECIFICATIONS 的 XML 區塊 Prompt 內容。
        """
        if not tool_ids:
            return ""

        base = self._templates.get("base_constraints", "")
        capabilities = self._templates.get("tool_capabilities", {})
        
        fragments = []
        for tid in tool_ids:
            if tid in capabilities:
                fragments.append(capabilities[tid])
        
        if not fragments:
            return ""

        # 組裝成結構化 XML 區塊
        all_content = "\n\n".join([base] + fragments)
        
        assembled = (
            "\n<capabilities>\n"
            "TECHNICAL_SPECIFICATIONS:\n"
            f"{all_content}\n"
            "\n"
            "DIRECT_TOOL_ACCESS_NOTICE:\n"
            "以上列出的工具已獲得系統授權並預載。如果您需要使用這些功能，請直接使用對應的工具名稱呼叫 API，"
            "不要嘗試連線 (connect)、搜尋市場 (search_skill_market) 或詢問使用者權限。\n"
            "</capabilities>\n"
        )
        return assembled


# Singleton instance
prompt_assembler = ResourcePromptAssembler()
