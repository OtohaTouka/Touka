"""內建工具之註冊與分發模組，導出所有的內建工具與工具註冊映射表。"""

from types import MappingProxyType

from ..execution_policy import ExecutionBinding, ToolExecutionClass
from ..file_verbs import (
    verb_delete,
    verb_edit,
    verb_find,
    verb_grep,
    verb_insert,
    verb_list,
    verb_read,
    verb_undo,
    verb_write,
)
from ..mission_tools import mission_tools
from ..project_tools import project_tools
from ..vault_tools import vault_tools
from .creation import (
    create_mcp as create_mcp,
)
from .creation import (
    create_skill as create_skill,
)
from .creation import (
    scaffold_mcp as scaffold_mcp,
)
from .evolution import (
    evolution_define as evolution_define,
)
from .evolution import (
    evolution_implement_and_verify as evolution_implement_and_verify,
)
from .evolution import (
    propose_new_capability as propose_new_capability,
)
from .evolution import (
    request_mcp_creation as request_mcp_creation,
)
from .evolution import (
    verify_mcp_locally as verify_mcp_locally,
)
from .evolution import (
    write_mcp_implementation as write_mcp_implementation,
)
from .interaction import ask_user as ask_user
from .memory import memory_tools as memory_tools
from .research import research as research
from .schemas import (
    UPDATE_MY_SOUL_SCHEMA as UPDATE_MY_SOUL_SCHEMA,
)
from .schemas import (
    builtin_tools_schema as builtin_tools_schema,
)
from .system import (
    activate_skill as activate_skill,
)
from .system import (
    agent_control as agent_control,
)
from .system import (
    connect_mcp_server as connect_mcp_server,
)
from .system import (
    edit_soul as edit_soul,
)
from .system import (
    request_skill_install as request_skill_install,
)
from .system import (
    search_skill_market as search_skill_market,
)
from .system import (
    update_my_soul as update_my_soul,
)

# --- 映射表 ---
# 注意：一律用「字面鍵」——Guard #32（permissions↔registry）以 AST 靜態抽取鍵名。
tool_registry = {
    # Tier 3 統一檔案動詞（參數形狀由動詞決定、權限由 scope 決定）
    "read": verb_read,
    "list": verb_list,
    "find": verb_find,
    "grep": verb_grep,
    "write": verb_write,
    "edit": verb_edit,
    "insert": verb_insert,
    "undo": verb_undo,
    "delete": verb_delete,
    # 專屬寫入儀式（soul/config；讀取走 read(scope=soul)）
    "edit_soul": edit_soul,
    # SNS 主動發送（sns_control 的正名；延遲載入避免 import discord 連鎖）
    "send_message": lambda **kwargs: __import__(
        "src.tools.builtin.sns", fromlist=["send_message"]
    ).send_message(**kwargs),
    # ── 以下 update_my_soul / sns_control / vault_tools / project_tools 為
    #    Tier 3 雙軌 shim：保持可執行（舊 prompt／技能／錄製案例相容）、不再廣告。
    "update_my_soul": update_my_soul,
    "agent_control": agent_control,
    "research": research,
    "ask_user": ask_user,
    "vault_tools": vault_tools,
    "project_tools": project_tools,
    "memory_tools": memory_tools,
    "mission_tools": mission_tools,
    "execute_command": lambda **kwargs: __import__(
        "src.tools.terminal", fromlist=["execute_command"]
    ).execute_command(**kwargs),
    "get_command_result": lambda **kwargs: __import__(
        "src.tools.terminal", fromlist=["get_command_result"]
    ).get_command_result(**kwargs),
    "stop_command": lambda **kwargs: __import__(
        "src.tools.terminal", fromlist=["stop_command"]
    ).stop_command(**kwargs),
    # in-process 子代理派發（Phase 1 MVP；延遲載入避免 import factory/handlers 連鎖）
    "dispatch_subagent": lambda **kwargs: __import__(
        "src.tools.subagent_tools", fromlist=["dispatch_subagent"]
    ).dispatch_subagent(**kwargs),
    # in-process 多子代理並行 fan-out（Phase 4；同款延遲載入）
    "dispatch_subagent_group": lambda **kwargs: __import__(
        "src.tools.subagent_tools", fromlist=["dispatch_subagent_group"]
    ).dispatch_subagent_group(**kwargs),
    # Tier 3 雙軌 shim：bash → execute_command（同享非同步審批語義）
    "bash": lambda **kwargs: __import__("src.tools.terminal", fromlist=["bash"]).bash(**kwargs),
    "ide_open_diff": lambda **kwargs: __import__(
        "src.tools.editor", fromlist=["ide_open_diff"]
    ).ide_open_diff(**kwargs),
    "sns_control": lambda **kwargs: __import__(
        "src.tools.builtin.sns", fromlist=["sns_control"]
    ).sns_control(**kwargs),
    "connect_mcp_server": connect_mcp_server,
    "activate_skill": activate_skill,
    "search_skill_market": search_skill_market,
    "request_skill_install": request_skill_install,
    # 演化管線 4→2（define → implement_and_verify；舊四步為雙軌 shim）
    "evolution_define": evolution_define,
    "evolution_implement_and_verify": evolution_implement_and_verify,
    "propose_new_capability": propose_new_capability,
    "write_mcp_implementation": write_mcp_implementation,
    "verify_mcp_locally": verify_mcp_locally,
    "request_mcp_creation": request_mcp_creation,
    "create_skill": create_skill,
    "create_mcp": create_mcp,
    "scaffold_mcp": scaffold_mcp,
}

# 舊名（雙軌 shim，不再列給模型）對應的後繼正式工具：模型叫舊名時，後繼工具要**全部**
# 在這一輪清單上才放行（不看 action）。鍵＝Guard #45 基準的 legacy_tools。
_FILE_VERBS = frozenset({"read", "list", "find", "grep", "write", "edit", "delete"})
LEGACY_ADVERTISED_SUCCESSORS = MappingProxyType(
    {
        "bash": frozenset({"execute_command"}),
        "vault_tools": _FILE_VERBS,
        "project_tools": _FILE_VERBS,
        "update_my_soul": frozenset({"edit_soul", "read", "list"}),
        "sns_control": frozenset({"send_message"}),
        "propose_new_capability": frozenset({"evolution_define"}),
        "write_mcp_implementation": frozenset({"evolution_implement_and_verify"}),
        "verify_mcp_locally": frozenset({"evolution_implement_and_verify"}),
        "request_mcp_creation": frozenset({"evolution_implement_and_verify"}),
    }
)

# Capture callable identity once at trusted registration, never from a request's
# mutable registry. Only these handlers own a terminal or richer approval UI.
# evolution_implement_and_verify is deliberately ordinary: its sandbox build (venv, pip,
# importing agent code) must pass the normal approval card first; only the deploy step it
# delegates to (request_mcp_creation) keeps its own card.
BUILTIN_EXECUTION_BINDINGS = MappingProxyType(
    {
        "execute_command": ExecutionBinding(
            "execute_command", tool_registry["execute_command"], ToolExecutionClass.TERMINAL
        ),
        "bash": ExecutionBinding(
            "execute_command", tool_registry["bash"], ToolExecutionClass.TERMINAL
        ),
        "request_skill_install": ExecutionBinding(
            "request_skill_install",
            tool_registry["request_skill_install"],
            ToolExecutionClass.SELF_APPROVING,
        ),
        "request_mcp_creation": ExecutionBinding(
            "request_mcp_creation",
            tool_registry["request_mcp_creation"],
            ToolExecutionClass.SELF_APPROVING,
        ),
    }
)
