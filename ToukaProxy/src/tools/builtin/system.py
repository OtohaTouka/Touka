"""系統控制中心與自主演化工具組 (System & Evolution Gateway).

包含代理人的核心狀態管理、靈魂核心 (Soul) 編輯、動態 MCP 掛載以及技能市場 (Skill Market)。
這是 ToukaProxy 的神經中樞，支援 Agent 的環境感知與主動演化。
"""

import json
import logging
from typing import Any

from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.session_persistence import SavedResult, SessionChanges
from src.protocols.registry import ProtocolServerConfig
from src.routers.terminal import request_approval
from src.services import tool_errors
from src.services.mcp_isolation_store import McpIsolationStore, assign, bound_of
from src.services.models import model_manager
from src.services.permission_service import permission_service
from src.services.rhythm_service import rhythm_service
from src.services.skill_service import skill_service
from src.utils import config_utils, mcp_tool_policy, soul_io
from src.utils.async_db import session_service
from src.utils.brain_persistence import get_active_brain, set_active_brain
from src.utils.config_utils import MCP_CONFIG_FILENAME
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)

# --- 模組路徑常量 (SSOT) ---
# 這些檔名將傳送至 config_utils 處理
MARKET_FILENAME = "skill_market.json"


async def agent_control(
    action: str,
    identity: AgentIdentity,
    target_model: str | None = None,
    _reason: str | None = None,
    **kwargs: Any,
) -> str:
    """代理系統控制中心：管理大腦切換、自主時間與狀態查詢.

    Args:
        action (str): 執行動作 ('list_brains', 'switch_brain', 'get_status').
        identity (AgentIdentity): 當前代理人身分實體.
        target_model (str | None): switch_brain 的目標模型名稱.
        _reason (str | None): 執行此動作的理由 (供日誌核審).
        **kwargs (Any): 其他參數，如 request_id.

    Returns:
        str: 系統狀態描述或操作結果.
    """
    request_id = kwargs.get("request_id")
    agent_id = identity.agent_id

    if action == "list_brains":
        models = model_manager.get_models()
        if not models:
            return "目前沒有可用的模型清單。"
        res = "--- Current Available Brains List ---\n"
        for m in models:
            res += f"- {m['id']} ({m.get('owned_by', 'unknown')})\n"
        res += "\nHint: Use 'switch_brain' action to switch."
        return res

    if action == "switch_brain":
        if not target_model:
            return "Error: 'target_model' is required for switch_brain."
        if target_model.lower() == "toukasoul":
            return (
                "Error: `ToukaSoul` is a routing core name and cannot be used "
                "as an underlying brain."
            )

        old_model = get_active_brain(identity)
        set_active_brain(identity, target_model)
        logger.info(f"🧠 Brain Switch: {agent_id} from {old_model} to {target_model}")
        return (
            f"Brain switch ready! My underlying brain has been updated "
            f"from {old_model} to {target_model}."
        )

    if action == "get_status":
        brain = get_active_brain(identity)
        remaining_rounds = 0
        if request_id:
            total_rounds = rhythm_service.get_total_rounds(request_id)
            used_rounds = rhythm_service.get_used_rounds(request_id)
            remaining_rounds = max(total_rounds - used_rounds, 0)
        return (
            f"=== Agent Status: {agent_id} ===\n"
            f"- Active Brain: {brain}\n"
            f"- Remaining Tool Rounds: {remaining_rounds}"
        )

    return f"Error: Unknown action '{action}'."


async def update_my_soul(
    identity: AgentIdentity,
    type: str | None = None,
    action: str = "read",
    content: str = "",
    filename: str | None = None,
    old_str: str | None = None,
    new_str: str | None = None,
    insert_line: int | None = None,
    view_range: list[int] | None = None,
    **_kwargs: Any,
) -> str:
    """自主更新、讀取或檢視靈魂核心檔案集.

    整合了高精度編輯與檔案管理功能，確保 Agent 能在自定義的人格邊界內演化.
    """
    # 1. 確定目標檔案
    if type == "soul":
        target_filename = "soul.md"
    elif type == "config":
        target_filename = "config.json"
    elif filename:
        target_filename = filename
    else:
        if action != "list":
            return "Error: Either 'type' (soul/config) or 'filename' must be provided."
        target_filename = None

    try:
        # --- [分發邏輯] ---
        if action == "list":
            files = soul_io.list_soul_files(identity)
            if not files:
                return "Your soul directory is empty."
            return "Files in your soul directory:\n" + "\n".join(
                [f"- {f}" for f in files]
            )

        if action == "read":
            if not target_filename:
                return "Error: target_filename is required for read."
            data = soul_io.read_soul_file(identity, target_filename)
            if data is None:
                return f"Error: {target_filename} does not exist."
            
            lines = data.splitlines(keepends=True)
            start, end = 1, len(lines)
            if view_range and len(view_range) == 2:
                start = max(1, view_range[0])
                end = min(len(lines), view_range[1])
            output = [f"{i + 1:4}: {lines[i]}" for i in range(start - 1, end)]
            return (
                f"=== {target_filename} (Lines {start}-{end}) ===\n"
                + "".join(output)
            )

        if action == "write":
            if not target_filename or not content:
                return "Error: 'target_filename' and 'content' are required for write."
            if target_filename == "soul.md" and len(content) < 50:
                return f"Error: soul.md 內容過短 ({len(content)} 字)，可能損壞核心人格。"
            
            if type == "config":
                json.loads(content)  # 格式校驗

            if soul_io.write_soul_file(identity, target_filename, content):
                return f"Success: {target_filename} has been safely overwritten."
            return f"Error: Failed to write {target_filename}."

        if action == "str_replace":
            if not target_filename or not old_str or new_str is None:
                return "Error: Requirements (target_filename/old_str/new_str) not met."
            
            data = soul_io.read_soul_file(identity, target_filename)
            if data is None:
                return f"Error: {target_filename} 不存在。"
            
            if data.count(old_str) != 1:
                return "Error: 找不到唯一匹配的 'old_str'。"
            
            new_data = data.replace(old_str, new_str)
            if target_filename == "soul.md" and len(new_data) < 50:
                return "Error: 替換失敗。修改後的 soul.md 太短。"

            if soul_io.write_soul_file(identity, target_filename, new_data):
                return f"Success: Modified {target_filename} via string replacement."
            return "Error: Failed to update soul file."

        if action == "insert":
            if not target_filename or insert_line is None or new_str is None:
                return "Error: 'target_filename', 'insert_line' and 'new_str' are required."
            
            data = soul_io.read_soul_file(identity, target_filename)
            if data is None:
                return f"Error: {target_filename} 不存在。"
            
            lines = data.splitlines(keepends=True)
            lines.insert(min(len(lines), max(0, insert_line)), new_str + "\n")
            final_content = "".join(lines)
            
            if target_filename == "soul.md" and len(final_content) < 50:
                return "Error: 插入失敗。內容校驗未通過。"

            if soul_io.write_soul_file(identity, target_filename, final_content):
                return f"Success: Inserted content into {target_filename}."
            return "Error: Failed to insert content."

        if action == "undo_edit":
            if not target_filename:
                return "Error: No file selected."
            if soul_io.undo_soul_edit(identity, target_filename):
                return f"Success: Reverted last edit for {target_filename}."
            return "Error: No backup found or failed to revert."

        if action == "delete":
            if not target_filename:
                return "Error: No file selected."
            if target_filename in ["soul.md", "config.json"]:
                return "Error: Protected files cannot be deleted."
            if soul_io.delete_soul_file(identity, target_filename):
                return f"Success: Deleted {target_filename}."
            return "Error: File not found or failed to delete."

    except json.JSONDecodeError as e:
        return f"Error: JSON 格式錯誤: {e}"
    except Exception as e:
        logger.exception("🔥 Soul Update Exception")
        return f"Error executing {action}: {str(e)}"

    return "Error: Invalid action."


# Tier 3（讀統一、寫獨立）：edit_soul 只承載寫入儀式；讀取走統一 read(scope=soul)。
_SOUL_WRITE_ACTIONS = frozenset({"write", "str_replace", "insert", "undo_edit", "delete"})


async def edit_soul(action: str = "write", **kwargs: Any) -> str:
    """靈魂／系統設定的專屬寫入儀式（soul/config 的唯一寫入路徑）.

    Tier 3 定案「soul 讀統一、寫獨立」：讀取類請走統一 ``read(scope=soul)`` /
    ``list(scope=soul)``；本工具只收寫入動作並委派 :func:`update_my_soul`
    （靈魂保護、審計與 HITL 行為完全沿用）。``update_my_soul`` 保留為 shim
    （可執行、不再廣告）。
    """
    if action not in _SOUL_WRITE_ACTIONS:
        return tool_errors.build_tool_error(
            tool_errors.VALIDATION_ERROR,
            f"edit_soul only carries write actions; '{action}' is not one of them.",
            suggestion=(
                "To view soul/config use read(scope=soul) or list(scope=soul); "
                f"write actions: {', '.join(sorted(_SOUL_WRITE_ACTIONS))}."
            ),
            param="action",
        )
    return await update_my_soul(action=action, **kwargs)


def _executor_may_write_session(session: dict[str, Any], kwargs: dict[str, Any]) -> bool:
    """掛 MCP／啟用技能前：session 綁的 agent 必須等於執行中的 agent（ADR-0245 修補）。

    綁定在 ToukaChat 存的是正式靈魂 id；舊前綴 ``ToukaSoul<id>`` 去前綴後再比。綁定不符
    就拒絕，upsert 也不再以 ``identity=`` 把綁定改成執行中的靈魂（判斷不了就保護）。
    """
    agent_id = kwargs.get("agent_id")
    bound_raw = session.get("agent_id")
    bound = bound_raw.removeprefix("ToukaSoul") if bound_raw else None
    return bool(agent_id) and bound == agent_id


async def connect_mcp_server(
    server_id: str,
    **kwargs: Any,
) -> str:
    """動態連線工具：將指定的 MCP 伺服器掛載到當前對話 Session 中.

    Args:
        server_id (str): 目標 MCP 伺服器識別碼.
        **kwargs (Any): 上下文參數，必須包含 'session_id'.

    Returns:
        str: 連線狀態描述.
    """
    session_id = kwargs.get("session_id")
    identity = kwargs.get("identity")
    if not session_id:
        return "Error: No session_id found in context. MCP connection failed."

    if not config_utils.config_exists(MCP_CONFIG_FILENAME):
        return "Error: MCP configuration file missing."

    try:
        data = config_utils.read_config_json(MCP_CONFIG_FILENAME)
        if not data:
            return "Error: Failed to read MCP configuration."
            
        mcp_servers = data.get("mcpServers", {})

        if server_id not in mcp_servers:
            return (
                f"Error: MCP server '{server_id}' not found. "
                "Check <mcp_registry> for IDs."
            )

        if not mcp_servers[server_id].get("enabled", True):
            return f"Error: MCP server '{server_id}' is currently disabled."

        snapshot = await session_service.read_persistence_snapshot(session_id, identity)
        session = snapshot.session
        if not session:
            return f"Error: Session '{session_id}' not found."
        if not _executor_may_write_session(session, kwargs):
            return tool_errors.build_tool_error(
                tool_errors.PERMISSION_DENIED,
                "This session is not bound to you; MCP connection refused.",
            )

        current_active = session.get("active_mcp_servers", [])
        if server_id in current_active:
            return f"Status: MCP server '{server_id}' is already connected."

        current_active.append(server_id)
        await _save_activation(
            session_id, snapshot.revision, identity,
            SessionChanges(active_mcp_servers=current_active),
        )
    except ToukaException as exc:
        return _activation_error(exc)
    except Exception:
        logger.exception(f"Failed to connect MCP server {server_id}")
        return tool_errors.build_tool_error(tool_errors.INTERNAL_ERROR, "SESSION_SAVE_FAILED")
    else:
        logger.info(f"✅ Connected MCP '{server_id}' to session '{session_id}'")
        return f"Success: MCP server '{server_id}' has been connected."


async def activate_skill(
    skill_id: str,
    **kwargs: Any,
) -> str:
    """技能啟用工具：將指定技能掛載到當前 Session，下一輪對話注入其完整指令.

    Pull 模式 (ADR-0066 Phase 3)：Agent 讀 <skill_registry> 清單後，自主呼叫本
    工具啟用所需技能。啟用狀態寫入 session.active_skills，下一輪由 SkillLayer 注入。

    Args:
        skill_id (str): 目標技能 ID（需參考 <skill_registry> 清單）.
        **kwargs (Any): 上下文參數，須含 'session_id' 與 'identity'.

    Returns:
        str: 啟用狀態描述.
    """
    session_id = kwargs.get("session_id")
    identity = kwargs.get("identity")
    if not session_id:
        return "Error: No session_id found in context. Skill activation failed."

    # 1. 技能存在性
    if skill_service.get_skill(skill_id) is None:
        return (
            f"Error: Skill '{skill_id}' not found. "
            "Check <skill_registry> for valid IDs."
        )

    # 2. RBAC：僅授權身分可啟用
    if identity is not None and not permission_service.can_use_skill(identity, skill_id):
        return f"Error: You are not authorized to use skill '{skill_id}'."

    try:
        snapshot = await session_service.read_persistence_snapshot(session_id, identity)
        session = snapshot.session
        if not session:
            return f"Error: Session '{session_id}' not found."
        if not _executor_may_write_session(session, kwargs):
            return tool_errors.build_tool_error(
                tool_errors.PERMISSION_DENIED,
                "This session is not bound to you; skill activation refused.",
            )

        current_active = session.get("active_skills", [])
        if skill_id in current_active:
            return f"Status: Skill '{skill_id}' is already active."

        current_active.append(skill_id)
        await _save_activation(
            session_id, snapshot.revision, identity, SessionChanges(active_skills=current_active),
        )
    except ToukaException as exc:
        return _activation_error(exc)
    except Exception:
        logger.exception(f"Failed to activate skill {skill_id}")
        return tool_errors.build_tool_error(tool_errors.INTERNAL_ERROR, "SESSION_SAVE_FAILED")
    else:
        logger.info(f"✅ Activated skill '{skill_id}' for session '{session_id}'")
        return (
            f"Success: Skill '{skill_id}' activated. "
            "Its full instructions will be available in your next turn."
        )


async def _save_activation(
    session_id: str, revision: str, identity: AgentIdentity, changes: SessionChanges
) -> None:
    result = await session_service.conditional_save_session(session_id, revision, changes, identity)
    if isinstance(result, SavedResult):
        return
    latest = await session_service.read_execution_snapshot(session_id, identity)
    code = ErrorCode.SESSION_SAVE_FAILED if latest.session else ErrorCode.SESSION_NOT_FOUND
    raise ToukaException(code)


def _activation_error(exc: ToukaException) -> str:
    category = {
        403: tool_errors.PERMISSION_DENIED,
        404: tool_errors.RESOURCE_NOT_FOUND,
    }.get(exc.status_code, tool_errors.INTERNAL_ERROR)
    return tool_errors.build_tool_error(category, exc.code.name)


async def search_skill_market(
    query: str | None = None,
    **_kwargs: Any,
) -> str:
    """技能市場搜尋工具：在預載的「官方技能市場」中尋找可擴展的能力.

    Args:
        query (str | None): 搜尋關鍵字。留空則列出熱門類別.
        **_kwargs (Any): 上下文接收參數 (不直接使用).

    Returns:
        str: 搜尋結果列表或類別導航.
    """
    if not config_utils.config_exists(MARKET_FILENAME):
        return "Error: Skill Market registry missing."

    try:
        data = config_utils.read_config_json(MARKET_FILENAME)
        if not data:
            return "Error: Failed to read Skill Market registry."
        skills = data.get("skills", {})

        if not query:
            categories = list({s.get("category", "General") for s in skills.values()})
            return (
                f"=== Touka Skill Market ===\n"
                f"目前市場共有 {len(skills)} 個可用技能，涵蓋類別：{', '.join(categories)}\n"
                "請提供關鍵字來查詢具體技能及其權限要求。"
            )

        q = query.lower()
        matches = [s for s in skills.values() if (
            q in s.get("name", "").lower() or
            q in s.get("description", "").lower() or
            q in s.get("category", "").lower()
        )]

        if not matches:
            return f"Market: 找不到與 '{query}' 相關的技能。"

        res = f"=== Market Search Results for '{query}' ===\n"
        for s in matches:
            res += f"- ID: {s['id']}\n"
            res += f"  Name: {s['name']}\n"
            res += f"  Description: {s['description']}\n"
            res += f"  Permissions: {', '.join(s.get('permissions', []))}\n"
            res += "  ----\n"
        
        res += "\n[Evolution]: 若符合需求，請發起『安裝申請』(request_skill_install)。"
    except Exception as e:
        logger.exception("🔥 Skill Market Search Error")
        return f"Error: {str(e)}"
    else:
        return res


async def request_skill_install(
    skill_id: str,
    **kwargs: Any,
) -> str:
    """技能安裝申請工具：向使用者提交特定的技能安裝請求 (HITL).

    Args:
        skill_id (str): 目標技能識別碼.
        **kwargs (Any): 需包含 agent_id.

    Returns:
        str: 申請狀態與安裝結果文字.
    """
    identity = kwargs.get("identity")
    if not identity:
        return "Error: Missing identity in request_skill_install."

    # ADR-0148（Bug A）：本工具自帶 EvolutionModal 審批（收 env 輸入），已於
    # tool_executor 標記為 _SELF_APPROVING_TOOLS 跳過 broker 雙重審批。互動會話走
    # 下方自帶審批卡；headless（heartbeat/派工，無人可填 env 也無人可批）→ 立即
    # fail-closed，不呼叫 request_approval 空等 180s（H1 headless 語意保持）。
    if not kwargs.get("_interactive", False):
        return (
            "Error: Skill installation requires an interactive session (a human to "
            "approve and provide credentials); it cannot run in an unattended "
            "(heartbeat/dispatch) turn. Ask the owner to install it from a chat session."
        )

    if not config_utils.config_exists(MARKET_FILENAME):
        return "Error: Skill Market registry missing."
        
    try:
        market_data = config_utils.read_config_json(MARKET_FILENAME)
        if not market_data:
            return "Error: Failed to read Skill Market registry."
            
        skill = market_data.get("skills", {}).get(skill_id)
            
        if not skill:
            return f"Error: Skill ID '{skill_id}' not found in market."
            
        if config_utils.config_exists(MCP_CONFIG_FILENAME):
            current_config = config_utils.read_config_json(MCP_CONFIG_FILENAME)
            if current_config and skill_id in current_config.get("mcpServers", {}):
                return f"Status: Skill '{skill_id}' is already installed."

        exec_id = f"install_{skill_id}_{int(get_current_epoch())}"
        
        approved, payload = await request_approval(
            exec_id=exec_id,
            identity=identity,
            command=f"Install Skill: {skill['name']}",
            type="skill_install_approval_request",
            # H2：審批請求同步推進當前聊天 SSE（ToukaChat 白名單早已認得此事件型別，
            # 但過去只推 /terminal/stream＝沒人聽 → 空等 180s 後拒絕）
            emit=kwargs.get("_emit_event"),
            extra_data={
                "skill_id": skill_id,
                "name": skill["name"],
                "description": skill["description"],
                "command": skill["mcp_config"]["command"],
                "args": skill["mcp_config"]["args"],
                "permissions": skill.get("permissions", []),
                "env_requirements": skill.get("env_requirements", [])
            }
        )
        
        if not approved:
            return f"Rejected: User denied the installation of skill '{skill_id}'."
        
        env_vars = payload.get("env_vars", {})
        mcp_config = {
            "command": skill["mcp_config"]["command"],
            "args": skill["mcp_config"]["args"],
            "enabled": True
        }
        if env_vars:
            mcp_config["env"] = env_vars
            
        data = config_utils.read_config_json(MCP_CONFIG_FILENAME)
        if not data:
            data = {}
        if "mcpServers" not in data:
            data["mcpServers"] = {}
        # 同名重裝不得抹掉該台既有的政策欄位（ADR-0195）。
        # 誠實註記：上方 `已安裝就早退` 的守衛使本行的 old_entry 目前恆為 None＝純防禦。
        # 保留而非刪除，是因為那道守衛一旦放寬（例如支援「重裝/升級」），抹除就會無聲回歸。
        old_entry = data["mcpServers"].get(skill_id)
        entry = mcp_tool_policy.carry_over_policy(mcp_config, old_entry)
        # 先寫隔離設定檔（skill＋bound）再寫設定（ADR-0275）；讀不到時拋 PERSISTENCE_ERROR
        record = assign(skill_id, "skill", bound_of(entry), fresh=old_entry is None)
        await McpIsolationStore(get_config_dir()).amutate(record)
        data["mcpServers"][skill_id] = entry
        config_utils.save_config_json(MCP_CONFIG_FILENAME, data)
            
        try:
            pm = (kwargs.get("_ctx") or {}).get("protocol_manager")
            started = bool(pm and await pm.add_server(skill_id, ProtocolServerConfig(**entry)))
        except Exception as e:
            logger.warning(f"Hot-swap failed but config saved: {e}")
            return f"Success: Installed, but needs manual restart. Error: {e}"

    except Exception as e:
        logger.exception("🔥 Skill Installation Error")
        return f"Error during installation: {str(e)}"
    else:
        state = (
            "started" if started
            else "saved; not started, check MCP settings for isolation status or pending reload"
        )
        return f"Success: Skill '{skill_id}' {state}."
