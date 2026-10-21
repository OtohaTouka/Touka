"""萬能檔案庫 (Vault Tools) 橋接模組.

負責管理代理人的個人私有空間 (personal)、遺產紀錄 (legacy) 與靈魂設定 (soul)。
本模組作為 Proxy 與實體存儲引擎 (ToukaMemory) 間的身分感知橋樑。
"""

from __future__ import annotations

import logging
from typing import Any

from src.models.identity import AgentIdentity
from src.services.resource_manager import resource_manager
from src.services.tool_errors import PERMISSION_DENIED, build_tool_error
from src.tools.project_tools import FILE_ACTION_CONDITIONALS, FILE_ACTION_PARAM_HINT
from src.utils.auth_mode import incognito_hides_vault

logger = logging.getLogger(__name__)

# --- [依賴管理：SSOT Import] ---
try:
    from memory_server.services.personal_catalog import CatalogPathDeniedError
    from memory_server.tools import vault_tools as memory_vault_impl
except ImportError:
    # 這裡允許延遲錯誤，以便在沒有安裝 ToukaMemory 的環境下仍能啟動 Proxy 進行基本測試
    memory_vault_impl = None


def incognito_scope_error(scope: str) -> str:
    """無痕對話碰不到這個範圍（檔案動詞與舊 shim 共用同一句）。"""
    return build_tool_error(
        PERMISSION_DENIED,
        f"Scope '{scope}' (old conversations) is not available in incognito chats.",
        suggestion="Do not retry; tell the user old conversations cannot be read here.",
        denied_scope=scope,
        reason="incognito",
    )


async def vault_tools(vault: str, action: str, **kwargs: Any) -> Any:
    """個人檔案庫操作入口 (Identity-Aware Bridge).

    根據主權身分實體 (AgentIdentity) 自動切換物理路徑，並將請求轉發至存儲引擎。

    Args:
        vault (str): 目標檔案庫類型 ('personal', 'legacy', 'soul')。
        action (str): 執行動作 (read, write, append, str_replace, list, find, grep, delete)。
        **kwargs (Any): 動作所需參數。
                        必須包含 'identity' (AgentIdentity) 以進行身分鎖定。

    Returns:
        Any: 操作結果文字或錯誤訊息。
    """
    if memory_vault_impl is None:
        return "Error: Memory system (ToukaMemory) is not integrated in this environment."

    # 1. 提取並驗證主權身分 (SSOT Identity Anchoring)
    identity = kwargs.get("identity")
    if not isinstance(identity, AgentIdentity):
        logger.error("🛡️ Security Violation: vault_tools called without valid AgentIdentity.")
        return "Error: Missing or invalid authentication identity."

    agent_id = identity.agent_id
    # 無痕對話讀寫不到舊對話庫：檔案動詞與舊 shim 都經過這裡（shim 在九個動詞都列出時也算列出）
    if incognito_hides_vault(vault, kwargs.get("_ctx")):
        return incognito_scope_error(vault)

    # 2. 資源強化檢查 (Gate 2 - Resource Hardening)
    # soul 檔案庫僅限唯讀操作（修改必須透過專門的 soul_service）
    if vault == "soul" and action not in ["read", "list", "grep", "find"]:
        msg = f"Agent '{agent_id}' attempted illegal write to 'soul' vault."
        logger.warning(f"🛡️ [Security] {msg}")
        return "Error: 'soul' vault is read-only via vault_tools."

    # 3. 準備轉發參數，強制將 agent_id 錨定為經驗證的身分
    payload = {**kwargs}
    payload["agent_id"] = agent_id
    payload["resource_proxy"] = resource_manager

    try:
        # 4. 執行物理操作 (邏輯下沉至 Memory 引擎)
        res = await memory_vault_impl(vault=vault, action=action, **payload)
    except CatalogPathDeniedError as e:
        # 個人空間目錄由系統維護：預期中的拒絕，只記一行 warning、不記 stack trace
        logger.warning("Agent '%s' tried to change '.touka/' via %s; refused.", agent_id, action)
        return build_tool_error(
            PERMISSION_DENIED,
            str(e),
            suggestion=(
                "Read .touka/catalog.json to view it; the system rebuilds it from your files."
            ),
        )
    except Exception as e:
        logger.exception("🔥 Error in vault_tools bridge for agent %s", agent_id)
        return f"Error executing {action}: {str(e)}"
    else:
        # 5. 變更動作的審計日誌 (Audit Trace)
        if "Success" in str(res) and action in ["write", "append", "str_replace", "delete"]:
            logger.info("Audit: Agent '%s' modified vault '%s' via %s", agent_id, vault, action)
        return res


# --- Schema Definition（Tier 2：過去無 schema → 模型只能猜參數／永不被廣告）---

VAULT_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vault_tools",
        "description": (
            "個人檔案庫：讀寫你自己的私有檔案空間。何時使用：個人筆記、草稿、"
            "跨任務的工作檔案（不屬於任何專案的檔案）。何時不要用：專案源碼請用 "
            "project_tools；修改靈魂或系統設定請用 update_my_soul；語意記憶"
            "（事實、對話回憶）請用 memory_tools。身分由系統自動注入並鎖定為你"
            "自己——路徑永遠解析到你自己的檔案庫。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "vault": {
                    "type": "string",
                    "enum": ["personal", "legacy", "soul"],
                    "description": (
                        "目標檔案庫：personal=私人空間（可讀寫）；legacy=遺產紀錄"
                        "（可讀寫）；soul=靈魂設定（唯讀——寫入請改用 update_my_soul）"
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": [
                        "read",
                        "write",
                        "append",
                        "str_replace",
                        "list",
                        "find",
                        "grep",
                        "delete",
                    ],
                    "description": FILE_ACTION_PARAM_HINT + "（與 project_tools 同義同名）",
                },
                "path": {"type": "string", "description": "相對於該檔案庫根目錄的路徑"},
                "content": {"type": "string", "description": "write 或 append 的內容"},
                "old_str": {
                    "type": "string",
                    "description": "str_replace 的舊字串（需精確且唯一匹配）",
                },
                "new_str": {"type": "string", "description": "str_replace 的新字串"},
                "query": {"type": "string", "description": "grep 的搜尋關鍵字"},
                "pattern": {"type": "string", "description": "find 的檔名匹配模式"},
                "max_depth": {"type": "integer", "default": 2, "description": "list 的深度"},
            },
            "required": ["vault", "action"],
            "oneOf": FILE_ACTION_CONDITIONALS,
        },
    },
}
