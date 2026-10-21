"""內建工具之 JSON Schema 定義清單，用於向 LLM 宣告可用的 API。"""

from src.tools.contracts import native_parameters
from src.tools.editor import IDE_OPEN_DIFF_SCHEMA
from src.tools.file_verbs import FILE_VERB_SCHEMAS
from src.tools.mission_tools import MISSION_TOOLS_SCHEMA
from src.tools.project_tools import PROJECT_TOOLS_SCHEMA
from src.tools.terminal import (
    BASH_SCHEMA,
    EXECUTE_COMMAND_SCHEMA,
    GET_COMMAND_RESULT_SCHEMA,
    STOP_COMMAND_SCHEMA,
)
from src.tools.vault_tools import VAULT_TOOLS_SCHEMA

# --- Tool Schemas ---

UPDATE_MY_SOUL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_my_soul",
        "description": (
            "最高權限工具：演化妳的核心意識 (soul.md) 或調整系統參數 (config.json)——"
            "這是 soul/config 的唯一寫入路徑。何時使用：更新自我認知、人格設定、系統"
            "參數。何時不要用：一般個人檔案請用 vault_tools(personal)；專案源碼請用 "
            "project_tools；唯讀查看 soul 亦可用 vault_tools(vault=soul)。"
            "read/write 等動作需 type（soul/config）或 filename 擇一；list 免參數。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "read",
                        "write",
                        "list",
                        "delete",
                        "str_replace",
                        "insert",
                        "undo_edit",
                    ],
                },
                "type": {"type": "string", "enum": ["soul", "config"]},
                "filename": {"type": "string"},
                "content": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
                "insert_line": {"type": "integer"},
                "view_range": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["action"],
        },
    },
}

AGENT_CONTROL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agent_control",
        "description": "代理系統控制台：管理模型切換與目前狀態查詢。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_brains",
                        "switch_brain",
                        "get_status",
                    ],
                },
                "target_model": {"type": "string"},
            },
            "required": ["action"],
        },
    },
}

RESEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "research",
        "description": (
            "全能研究工具：預設 auto 備援鏈（Tavily→SearXNG→DuckDuckGo，自動切換、"
            "免金鑰後援），亦可指定單一來源或 Gemini 原生 Google 搜尋。"
            "何時不要用：查詢本機專案或個人檔案內容（請用 project_tools／"
            "vault_tools），或答案已在對話脈絡中時。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source": {
                    "type": "string",
                    "enum": ["auto", "tavily", "searxng", "ddg", "google"],
                    "default": "auto",
                },
            },
            "required": ["query"],
        },
    },
}


SNS_CONTROL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sns_control",
        "description": "SNS 外部通知工具：讓 Agent 能夠主動發送訊息至 Discord 或 Telegram 頻道。適用於心跳報告或主動通知使用者。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["send_message"],
                    "description": "動作：目前僅支援 send_message",
                },
                "platform": {
                    "type": "string",
                    "description": "平台名稱，如 'discord' 或 'telegram'",
                },
                "channel_id": {
                    "type": "string",
                    "description": "頻道或聊天 ID (Discord Channel ID 或 Telegram Chat ID)",
                },
                "content": {
                    "type": "string",
                    "description": "發送內容",
                },
            },
            "required": ["action", "platform", "channel_id", "content"],
        },
    },
}

CONNECT_MCP_SERVER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "connect_mcp_server",
        "description": "動態連線工具：根據 server_id 將特定的 MCP 伺服器掛載到當前會話中。連線後，該伺服器的工具將在下一輪對話可用。",
        "parameters": {
            "type": "object",
            "properties": {
                "server_id": {
                    "type": "string",
                    "description": "欲連線的 MCP 伺服器 ID（需參考 <mcp_registry> 中的清單）",
                }
            },
            "required": ["server_id"],
        },
    },
}

ACTIVATE_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "activate_skill",
        "description": "技能啟用工具：根據 skill_id 啟用特定技能，其完整指令將在下一輪對話注入生效。當你判斷某個技能（見 <skill_registry>）有助於當前任務時調用。",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": "欲啟用的技能 ID（需參考 <skill_registry> 中的清單）",
                }
            },
            "required": ["skill_id"],
        },
    },
}

SEARCH_SKILL_MARKET_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_skill_market",
        "description": "技能市場搜尋工具：在官方核准的市場中尋找新的擴展能力。當您發現現有工具無法完成任務時，請調用此工具發現解決方案。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜尋關鍵字（如 '影像處理'、'網路搜尋'），留空則列出所有類別。",
                }
            },
        },
    },
}

REQUEST_SKILL_INSTALL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "request_skill_install",
        "description": "技能安裝申請工具：向使用者提交特定的技能安裝請求。當您在市場搜尋到中意的工具後，請調用此工具進行實體裝載。",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": "欲安裝的技能 ID（需參考 search_skill_market 的結果）",
                }
            },
            "required": ["skill_id"],
        },
    },
}

PROPOSE_NEW_CAPABILITY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "propose_new_capability",
        "description": "演化第一步：提案新能力。定義工具的名稱、描述與參數 Schema，預先配置託管空間。",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string", "description": "唯一 ID，例如 'file-counter'"},
                "name": {"type": "string", "description": "人類可讀名稱"},
                "description": {"type": "string", "description": "詳細功能說明"},
                "schema": {
                    "type": "object",
                    "description": "工具調用的 JSON Schema (parameters 部份)",
                },
            },
            "required": ["skill_id", "name", "description", "schema"],
        },
    },
}

WRITE_MCP_IMPLEMENTATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_mcp_implementation",
        "description": "演化第二步：實體開發。自動建立獨立虛擬環境並寫入 Python 原始碼，安裝必要依賴。",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string"},
                "code": {
                    "type": "string",
                    "description": "實作 MCP 協議的 Python 原始碼 (stdio 模式)",
                },
                "dependencies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "需要安裝的 PIP 套件清單",
                },
            },
            "required": ["skill_id", "code"],
        },
    },
}

VERIFY_MCP_LOCALLY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "verify_mcp_locally",
        "description": "演化第三步：沙盒測試。在獨立環境中執行生成的伺服器並模擬 JSON-RPC 調用，驗證輸出是否符合預期。",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string"},
                "tool_name": {"type": "string", "description": "欲測試的工具名稱"},
                "arguments": {"type": "object", "description": "調用參數內容"},
            },
            "required": ["skill_id", "tool_name", "arguments"],
        },
    },
}

REQUEST_MCP_CREATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "request_mcp_creation",
        "description": "演化最終步：代碼審查與發布。向使用者提交生成的源碼與測試報告。核准後將自動熱插拔並啟用此能力。",
        "parameters": {
            "type": "object",
            "properties": {"skill_id": {"type": "string", "description": "欲部署的技能 ID"}},
            "required": ["skill_id"],
        },
    },
}

EDIT_SOUL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "edit_soul",
        "description": (
            "最高權限寫入儀式：演化妳的核心意識（soul.md）或系統參數（config.json）——"
            "soul/config 的唯一寫入路徑（獨立審計＋確認）。唯讀查看請用 read(scope=soul)"
            "／list(scope=soul)。一般檔案請用統一檔案動詞（write/edit 等）。"
            "write 需 content；str_replace 需 old_str+new_str；insert 需 "
            "insert_line+new_str；目標以 type（soul/config）或 filename 擇一指定。"
        ),
        "parameters": native_parameters("edit_soul"),
    },
}

SEND_MESSAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_message",
        "description": (
            "SNS 主動通知：發送訊息至 Discord 或 Telegram 頻道，適用於心跳報告或"
            "主動通知使用者。何時不要用：在對話中回覆使用者不需此工具（直接回覆即可）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "description": "平台名稱，如 'discord' 或 'telegram'",
                },
                "channel_id": {
                    "type": "string",
                    "description": "頻道或聊天 ID (Discord Channel ID 或 Telegram Chat ID)",
                },
                "content": {"type": "string", "description": "發送內容"},
            },
            "required": ["platform", "channel_id", "content"],
        },
    },
}

EVOLUTION_DEFINE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "evolution_define",
        "description": (
            "演化第一段：提案新能力——定義工具名稱、描述與參數 Schema，預先配置"
            "託管空間。流程：evolution_define → evolution_implement_and_verify"
            "（實作＋沙盒驗證，驗證通過後可送審部署）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string", "description": "唯一 ID，例如 'file-counter'"},
                "name": {"type": "string", "description": "人類可讀名稱"},
                "description": {"type": "string", "description": "詳細功能說明"},
                "schema": {
                    "type": "object",
                    "description": "工具調用的 JSON Schema (parameters 部份)",
                },
            },
            "required": ["skill_id", "name", "description", "schema"],
        },
    },
}

EVOLUTION_IMPLEMENT_AND_VERIFY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "evolution_implement_and_verify",
        "description": (
            "演化第二段：一次完成實體開發（獨立 venv＋依賴安裝＋寫入 server.py）"
            "與沙盒驗證（運行時監獄模擬調用）。需先 evolution_define（未提案會被"
            "拒）。可反覆呼叫迭代修正；驗證通過後以 submit_for_deploy=true 送使用"
            "者審核並熱部署——驗證未過絕不部署。"
        ),
        "parameters": native_parameters("evolution_implement_and_verify"),
    },
}

# --- Builtin Tool Schemas List ---

CREATE_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_skill",
        "description": "創建工具（限 Owner/admin）：以原生引擎從零建立新 skill，走完整流水線、不覆寫既有。",
        "parameters": native_parameters("create_skill"),
    },
}

CREATE_MCP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_mcp",
        "description": "創建工具（限 Owner/admin）：註冊既有指令的 MCP server，先沙箱試跑握手再寫入。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "server 名稱"},
                "command": {"type": "string", "description": "啟動指令（如 uvx / python）"},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "指令參數",
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "環境變數佔位（勿放密鑰）",
                },
                "description": {"type": "string", "description": "server 用途描述"},
            },
            "required": ["name", "command"],
        },
    },
}

SCAFFOLD_MCP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "scaffold_mcp",
        "description": "創建工具（限 Owner/admin）：從零生成 MCPServer v2 stub 並走完整流水線。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "server 名稱（正規化為單段 slug）"},
                "description": {"type": "string", "description": "server 用途描述"},
                "tools": {
                    "type": "array",
                    "description": "要生成的工具清單",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "工具名（合法 Python 識別字）",
                            },
                            "description": {"type": "string", "description": "工具描述"},
                            "params": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "type": {
                                            "type": "string",
                                            "description": "str/int/float/bool",
                                        },
                                    },
                                    "required": ["name"],
                                },
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["name", "tools"],
        },
    },
}

ASK_USER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "當你需要使用者做決定或提供關鍵資訊、且無法從現有脈絡合理推斷時，呼叫此工具向使用者提問。"
            "會暫停生成、等使用者作答後再以答案續跑。支援單選/複選/連續多問題，每題可附『其他』自由填。"
            "僅在有真人互動的對話中使用；不要為了確認顯而易見的事而打斷使用者。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "一到多個要問使用者的問題（連續多問題）。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "問題文字。"},
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "供選擇的選項。",
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "description": "true=可複選；false=單選。",
                            },
                            "allowOther": {
                                "type": "boolean",
                                "description": "是否允許使用者自由填寫『其他』。",
                            },
                            "header": {"type": "string", "description": "選填的簡短標籤。"},
                        },
                        "required": ["question", "options"],
                    },
                }
            },
            "required": ["questions"],
        },
    },
}

# Tier 3 雙軌：舊 god-tools 降為 shim（可執行、不再廣告）。schema 常數保留＝shim 的
# 形狀文件（debug/測試引用）；廣告面由統一動詞（FILE_VERB_SCHEMAS）＋ edit_soul ＋
# send_message 接手（提案 §7 工具數學：廣告約 28、每顆憑名字即可選對）。
LEGACY_SHIM_SCHEMAS = [
    UPDATE_MY_SOUL_SCHEMA,  # 寫入 → edit_soul；讀取 → read(scope=soul)
    SNS_CONTROL_SCHEMA,  # → send_message（假 action enum 移除）
    PROJECT_TOOLS_SCHEMA,  # → 統一動詞 scope=project
    VAULT_TOOLS_SCHEMA,  # → 統一動詞 scope=personal/legacy/soul
    BASH_SCHEMA,  # → execute_command（非同步審批）
    PROPOSE_NEW_CAPABILITY_SCHEMA,  # → evolution_define
    WRITE_MCP_IMPLEMENTATION_SCHEMA,  # → evolution_implement_and_verify
    VERIFY_MCP_LOCALLY_SCHEMA,  # → evolution_implement_and_verify（合併驗證）
    REQUEST_MCP_CREATION_SCHEMA,  # → evolution_implement_and_verify(submit_for_deploy)
]

# in-process 子代理派發（提案 Phase 1 MVP）：schema 為純資料、定義於此（SSOT）；
# 實作在 src/tools/subagent_tools.py（registry 走延遲載入，避免 import factory 連鎖）。
DISPATCH_SUBAGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "dispatch_subagent",
        "description": (
            "派出一個 in-process 子代理（Touka 內部執行，不外派外部 AI）非同步完成"
            "單一自包含任務。立即回傳 callback_token——之後用 mission_tools("
            "action='await_dispatch_result', callback_token=...) 等待結果，期間可先"
            "做其他事。子代理以受限工具面（Subagent）獨立執行、看不到你的對話上下文、"
            "也無法再派子代理。何時不要用：任務依賴目前對話上下文，或你幾步內就能"
            "自己完成時。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": (
                        "子任務目標與完整指示；必須自包含"
                        "（子代理只看得到這段，含必要的檔案路徑與驗收條件）"
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "（選）對應 mission board 的 task ID，僅作結果標記；"
                        "看板狀態更新仍由你自己執行"
                    ),
                },
                "sub_model": {
                    "type": "string",
                    "description": "（選）子代理底層模型；預設沿用你目前的模型",
                },
            },
            "required": ["goal"],
        },
    },
}

# in-process 多子代理並行 fan-out（Phase 4）：一次派多個獨立子任務、阻塞至全完成匯總。
DISPATCH_SUBAGENT_GROUP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "dispatch_subagent_group",
        "description": (
            "同時派多個 in-process 子代理各跑各的獨立子任務，阻塞至全部完成後把每個"
            "子任務的結構化回報匯總回來。適合把一個大任務拆成數個**彼此獨立**的子任務"
            "並行完成再收斂。並發與子任務數皆有上限。何時不要用：子任務彼此相依（改逐一"
            "派）、或需要「先派了先做別的」的非阻塞語義（改用 dispatch_subagent 拿 token）。"
        ),
        "parameters": native_parameters("dispatch_subagent_group"),
    },
}

builtin_tools_schema = [
    CREATE_SKILL_SCHEMA,
    CREATE_MCP_SCHEMA,
    SCAFFOLD_MCP_SCHEMA,
    IDE_OPEN_DIFF_SCHEMA,
    EDIT_SOUL_SCHEMA,
    AGENT_CONTROL_SCHEMA,
    RESEARCH_SCHEMA,
    EXECUTE_COMMAND_SCHEMA,
    GET_COMMAND_RESULT_SCHEMA,
    STOP_COMMAND_SCHEMA,
    SEND_MESSAGE_SCHEMA,
    *FILE_VERB_SCHEMAS,
    CONNECT_MCP_SERVER_SCHEMA,
    ACTIVATE_SKILL_SCHEMA,
    SEARCH_SKILL_MARKET_SCHEMA,
    REQUEST_SKILL_INSTALL_SCHEMA,
    EVOLUTION_DEFINE_SCHEMA,
    EVOLUTION_IMPLEMENT_AND_VERIFY_SCHEMA,
    MISSION_TOOLS_SCHEMA,
    ASK_USER_SCHEMA,
    DISPATCH_SUBAGENT_SCHEMA,
    DISPATCH_SUBAGENT_GROUP_SCHEMA,
]
