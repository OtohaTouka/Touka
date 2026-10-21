"""記憶系統工具 Schema 定義 (SSOT).

本模組落實 MEMORY_SSOT_MANDATE.md，定義記憶與檔案庫工具的對外介面合約。
"""

from memory_server.tools.contracts import MEMORY_ACTION_CONTRACTS, action_parameters

VAULT_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vault_tools",
        "description": (
            "萬能檔案庫：提供個人私有硬碟 (personal)、歷史遺產 (legacy) "
            "與靈魂配置 (soul) 的操作。legacy shim 不向模型揭露；"
            "整合橋接中的 soul 僅唯讀，寫入請用 edit_soul。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "vault": {
                    "type": "string",
                    "enum": ["personal", "legacy", "soul"],
                    "description": "要存取的檔案庫類型",
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
                    "description": "執行動作",
                },
                "path": {"type": "string", "description": "目標相對路徑 (例如 config.json)"},
                "content": {"type": "string", "description": "write/append: 欲寫入或附加的內容"},
                "old_str": {"type": "string", "description": "str_replace: 欲替換的舊字串"},
                "new_str": {"type": "string", "description": "str_replace: 替換後的新字串"},
                "query": {"type": "string", "description": "grep: 搜尋關鍵字"},
                "pattern": {"type": "string", "description": "find: 檔名匹配模式"},
                "max_depth": {"type": "integer", "default": 2, "description": "目錄遞迴深度"},
                "offset": {"type": "integer", "default": 0, "description": "read: 起始行數"},
                "limit": {"type": "integer", "default": 500, "description": "read: 讀取上限"},
            },
            "required": ["vault", "action"],
        },
    },
}

MEMORY_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_tools",
        "description": "記憶管家：存儲與檢索您的長期記憶、事實與偏好。",
        "parameters": action_parameters(MEMORY_ACTION_CONTRACTS),
    },
}

MISSION_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mission_tools",
        "description": "任務看板：追蹤與管理您的自主任務、子任務拆解與執行進度。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["init", "get", "update"],
                    "description": "執行動作",
                },
                "goal": {"type": "string", "description": "init: 總體宏觀目標"},
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "dependencies": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "description": "init: 初始子任務清單",
                },
                "task_id": {"type": "string", "description": "update: 欲更新的任務 ID"},
                "status": {
                    "type": "string",
                    "enum": ["TODO", "DOING", "DONE", "FAILED", "BLOCKED"],
                    "description": "update: 新的任務狀態",
                },
                "artifact_ref": {"type": "string", "description": "update: 關聯的產出物 URI"},
            },
            "required": ["action"],
        },
    },
}

MEMORY_TOOLS_SCHEMA_LIST = [MEMORY_TOOLS_SCHEMA]
