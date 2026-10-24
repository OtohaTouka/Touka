"""可觀測性路由器，提供健康檢查、系統監控與日誌查詢接口。"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import psutil
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from memory_server.factual import FactsSchemaStatus, FactualStore, find_unmigrated_jsonl_agents

from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.models.observability import (
    HealthResponse,
    IsolationExecutorStatus,
    IsolationLocalStatus,
    IsolationMcpStatus,
    IsolationRepairs,
    IsolationStatusResponse,
    SystemHealthResponse,
    SystemNotice,
)
from src.services.authorization_gateway import require_authenticated, require_privileged
from src.services.autonomy import autonomy_service
from src.services.identity_router import get_verified_identity
from src.services.isolation.mcp_launch import mcp_launch_book
from src.services.isolation.status import IsolationSnapshot, isolation_monitor
from src.services.mcp_isolation_store import McpIsolationStore
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.utils.soul_tool_selection import TOOL_COMPANIONS
from src.utils.time_utils import get_current_epoch, get_current_taipei_iso

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["observability"])

# 存放最近的系統事件，供前端即時顯示
system_events = []

# 記憶寶庫跨 agent 列示上限（有界高上限；遠高於舊的 200，避免把 Alice/Bob
# 等較舊的記憶截斷掉）。未來資料量更大時可改為分頁。
MEMORY_LIST_LIMIT = 5000

# 記憶提示（/memory 的 memory_notices）：只放代碼與等級，文字由前端翻譯。
# 每一項是 {"code", "level", 選填欄位...}；新提示加新代碼，不改既有欄位。
MEMORY_NOTICE_LEVEL_INFO = "info"
MEMORY_NOTICE_LEVEL_WARNING = "warning"
MEMORY_NOTICE_UNMIGRATED_LEGACY_FACTS = "unmigrated_legacy_facts"
# 事實庫結構升級或讀取失敗（警告級；原因細節只進日誌）。
MEMORY_NOTICE_UPGRADE_NO_SPACE = "memory_upgrade_no_space"
MEMORY_NOTICE_UPGRADE_LOCKED = "memory_upgrade_locked"
MEMORY_NOTICE_STORE_UNREADABLE = "memory_store_unreadable"
MEMORY_NOTICE_UPGRADE_FAILED = "memory_upgrade_failed"
MEMORY_NOTICE_NEW_STORE_BESIDE_BACKUPS = "memory_new_store_beside_backups"
_SCHEMA_STATUS_NOTICES = {
    FactsSchemaStatus.NO_SPACE: MEMORY_NOTICE_UPGRADE_NO_SPACE,
    FactsSchemaStatus.LOCKED: MEMORY_NOTICE_UPGRADE_LOCKED,
    FactsSchemaStatus.UNREADABLE: MEMORY_NOTICE_STORE_UNREADABLE,
    FactsSchemaStatus.FAILED_OTHER: MEMORY_NOTICE_UPGRADE_FAILED,
}


def memory_schema_notices(
    status: FactsSchemaStatus | None, *, beside_backups: bool = False
) -> list[dict[str, Any]]:
    """事實庫結構狀態（＋新庫旁有升級備份）→ 警告級提示；沒有問題就是空清單。"""
    codes = [
        _SCHEMA_STATUS_NOTICES.get(status) if status else None,
        MEMORY_NOTICE_NEW_STORE_BESIDE_BACKUPS if beside_backups else None,
    ]
    return [{"code": code, "level": MEMORY_NOTICE_LEVEL_WARNING} for code in codes if code]


def collect_memory_notices() -> list[dict[str, Any]]:
    """Proxy 啟動時算一次記憶提示；只看檔名，不讀不改任何檔案。

    舊版 facts.jsonl 沒改名成 .bak，代表裡面的記憶從沒搬進記憶庫。這不是記憶系統壞掉，
    所以不走 system_degraded（那會讓模型以為記憶不可用），改用這條只給畫面看的提示。
    """
    try:
        agents = find_unmigrated_jsonl_agents()
    except Exception:
        logger.exception("Memory notice scan failed")
        return []
    if not agents:
        return []
    logger.warning(
        "Old facts.jsonl never moved into the memory database for %d agent(s): %s; "
        "files are left untouched",
        len(agents),
        ", ".join(agents),
    )
    return [
        {
            "code": MEMORY_NOTICE_UNMIGRATED_LEGACY_FACTS,
            "level": MEMORY_NOTICE_LEVEL_INFO,
            "count": len(agents),
        }
    ]


# 記憶寶庫／日誌端點閘（2026-07-07 權限稽查收緊；對齊 debug.py 三元組）。這些端點
# 回傳跨 agent 記憶內容與實際請求 payload，先前任何已驗證身分（含 Guest 級 token）
# 皆可讀＝繞過工具層 Gate-1 的 vault 範圍管制。人類 normal 角色一併拒絕——跨 agent
# 資料非成員面；日後成員要看「自己 agent」的記憶，應另做擁有權過濾版而非放寬此閘。


# 跨 agent 記憶／日誌類端點的角色閘（ADR-0141：三元組 → is_privileged 謂詞）。
#
# 不符一律 403 fail-loud。人類 admin 或特權 agent（Owner/Debugger）放行。
_require_privileged = require_privileged
# 隔離狀態（側欄橫幅）：任何已登入身分即可讀（ADR-0239 授權閘道的別名）。
_require_authenticated = require_authenticated


def _fact_to_memory_dto(fact: dict[str, Any]) -> dict[str, Any]:
    """將 FactualStore 事實列轉為記憶寶庫前端 DTO。

    created_at 以「秒」儲存（後端時間 SSOT, ``time_utils.get_current_epoch``）；
    前端時間 SSOT（``formatToTaipeiTime`` → ``new Date``）吃「毫秒」，
    故在此 API 邊界做秒→毫秒轉換（DB 儲存維持秒、不變動）。
    """
    created_at = fact.get("created_at") or 0
    return {
        "content": fact.get("content"),
        "timestamp": int(created_at) * 1000,
        "tags": fact.get("tags"),
        "agent_id": fact.get("agent_id"),
    }


def log_event(level: str, message: str, source: str = "System"):
    """記錄系統事件並保留最近 100 筆。"""
    event = {
        "timestamp": get_current_taipei_iso(),
        "level": level,
        "source": source,
        "message": message,
    }
    system_events.append(event)
    # 只保留最近 100 筆
    if len(system_events) > 100:
        system_events.pop(0)


# 通用提示（/health 的 system_notices）：功能被停掉的設定問題，只放代碼與等級，文字由前端
# 翻譯（原因與恢復方法）。代碼就是服務給的原因字串；新提示加新代碼，不改既有欄位。
SYSTEM_NOTICE_AUTONOMY_UNREADABLE = "autonomy_config_unreadable"
SYSTEM_NOTICE_AUTH_POLICIES_UNREADABLE = "auth_policies_unreadable"
_SYSTEM_NOTICE_CODES = (SYSTEM_NOTICE_AUTONOMY_UNREADABLE, SYSTEM_NOTICE_AUTH_POLICIES_UNREADABLE)


def collect_system_notices() -> list[SystemNotice]:
    """列出自主設定與權限服務的降級原因裡有提示代碼的（警告級，順序固定）。"""
    healths = (autonomy_service.config_health, permission_service.permission_config_health)
    reasons = {health.reason for health in healths if health.degraded}
    return [
        SystemNotice(code=code, level="warning") for code in _SYSTEM_NOTICE_CODES if code in reasons
    ]


def _health_status(permission_degraded: bool) -> str:
    """權限設定降級，或 autonomy.json 讀不到（自主執行已停）時回 degraded。

    autonomy 沒有自己的欄位，只反映在 status；原因寫在 AUDIT log。
    """
    degraded = permission_degraded or autonomy_service.config_health.degraded
    return "degraded" if degraded else "healthy"


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """健康檢查端點"""
    permission_health = permission_service.permission_config_health
    return HealthResponse(
        status=_health_status(permission_health.degraded),
        timestamp=get_current_taipei_iso(),
        permission_config_degraded=permission_health.degraded,
        permission_config_degraded_reason=permission_health.reason,
        system_notices=collect_system_notices(),
    )


@router.get("/system_health", response_model=SystemHealthResponse)
async def get_system_health() -> SystemHealthResponse:
    """取得系統資源狀態（CPU、內存等）"""
    permission_health = permission_service.permission_config_health
    return SystemHealthResponse(
        status=_health_status(permission_health.degraded),
        cpu_usage=psutil.cpu_percent(),
        memory_usage=psutil.virtual_memory().percent,
        disk_usage=psutil.disk_usage(str(Path.cwd().resolve())).percent,
        timestamp=get_current_taipei_iso(),
        permission_config_degraded=permission_health.degraded,
        permission_config_degraded_reason=permission_health.reason,
        system_notices=collect_system_notices(),
    )


def isolation_status_response(
    snap: IsolationSnapshot, *, mcp: IsolationMcpStatus
) -> IsolationStatusResponse:
    """判定結果轉成回應：只取層級與原因代碼；執行器只露 ABI 與網路設定，自癒只留宣告的計數."""
    local, ready = snap.local, snap.executor
    return IsolationStatusResponse(
        level=snap.level,
        expected=snap.expected,
        reason=snap.reason,
        detail=snap.detail,
        features=sorted(snap.features),
        local=IsolationLocalStatus(
            status=local.status, abi=local.abi, reason=local.reason, net=local.net
        ),
        local_issue=snap.local_issue,
        executor=IsolationExecutorStatus(abi=ready.abi, net=ready.net) if ready else None,
        block_ports_issue=snap.block_ports_issue,
        repairs=IsolationRepairs.model_validate(snap.repairs) if snap.repairs is not None else None,
        mcp=mcp,
    )


@router.get("/isolation", response_model=IsolationStatusResponse)
async def get_isolation_status(
    _identity: Annotated[AgentIdentity, Depends(_require_authenticated)],
) -> IsolationStatusResponse:
    """隔離狀態（側欄橫幅）：任何已登入身分可讀；不帶路徑、權杖、金鑰與代理人 ID.

    走 ``ensure_ready``：還沒有指令跑過時也會開始檢查（有上限，逾時照實回 none）。
    ``repairs`` 是 monitor 的自癒計數；沒有掛鉤（不是 Linux）時回 null。
    """
    snap = await isolation_monitor.ensure_ready()
    counts = mcp_launch_book.counts()
    store = await asyncio.to_thread(McpIsolationStore(get_config_dir()).load)
    return isolation_status_response(snap, mcp=IsolationMcpStatus(
        unisolated=counts.unisolated, waiting=counts.waiting, suspended=counts.suspended,
        store_issue=store.issue,
    ))


@router.get("/memory")
async def get_memory_stats(
    request: Request,
    _identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
):
    """記憶系統統計信息"""
    ctx = request.app.state.context
    # 讀目前的共用實例（換嵌入模型後會換新的），還沒建就用啟動時那個；絕不在這裡建構
    store = FactualStore.existing_shared() or ctx.get("memory_factual_store")
    schema_status = store.current_schema_status() if store else None
    if store is None and ctx.get("memory_store_open_failed"):  # 啟動時建不出來、之後也還沒建成
        schema_status = FactsSchemaStatus.UNREADABLE
    beside_backups = bool(store and store.new_store_beside_backups)
    return {
        "system_degraded": ctx.get("system_degraded", False),
        "degraded_reason": ctx.get("degraded_reason", ""),
        "memory_tools_count": len(ctx.get("memory_tool_registry", {})),
        "memory_notices": [
            *ctx.get("memory_notices", []),
            *memory_schema_notices(schema_status, beside_backups=beside_backups),
        ],
    }


@router.get("/memory/list")
async def get_memory_list(
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
):
    """讀取 ToukaMemory 事實庫（透過整合層，不直接操作 SQLite）。

    跨 agent 全量列示（記憶寶庫）→ 僅限 Owner／Debugger／人類 admin。
    ToukaProxy 獨立運作（無 ToukaMemory 整合）時優雅降級回傳空列表。
    """
    _require_privileged(identity)
    try:
        from memory_server.tools import (
            get_manager,  # noqa: LOCAL_IMPORT_BAN — 整合層延遲匯入，ToukaProxy 獨立運作時 ImportError
        )
        manager = get_manager()
        factual = manager.get_factual_store()
        all_facts = factual.query_all_facts(limit=MEMORY_LIST_LIMIT)
        memories = [_fact_to_memory_dto(f) for f in all_facts]
        return {"memories": memories}
    except ImportError:
        return {"memories": []}
    except Exception:
        # 讀不到不能顯示成「沒有記憶」
        logger.exception("Error fetching memory list")
        raise ToukaException(ErrorCode.SYSTEM_ERROR) from None

@router.get("/memory/health")
async def get_memory_health(
    _identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
):
    """檢查各項儲存組件是否正常運作"""
    base_dir = resource_manager.get_base_path()
    
    def check_exists(parts):
        """檢查給定路徑元件拼接後的檔案或目錄是否存在。"""
        return Path(Path(base_dir).joinpath(*parts)).exists()

    return {
        "facts": check_exists(["vault", "core_memory.db"]),
        "vector": check_exists(["vault", "_qdrant_db"]),
        "legacy": Path(Path(base_dir) / "legacy").is_dir(),
        "personal": Path(Path(base_dir) / "personal_drive").is_dir(),
    }


def _read_system_logs(lines: int) -> dict[str, Any]:
    """讀 system_status.log 最新幾行（``/system_logs`` 與 ``/logs/recent`` 共用）。"""
    base_dir = resource_manager.get_base_path()
    log_path = Path(base_dir) / "logs" / "system_status.log"
    if not Path(log_path).exists():
        return {"logs": ["Log file not found."]}

    try:
        content = resource_manager.read_file_text(log_path)
        if content:
            all_lines = content.splitlines(keepends=True)
            return {"logs": all_lines[-lines:]}
        return {"logs": ["Log file is empty."]}
    except Exception as e:
        return {"logs": [f"Error reading logs: {str(e)}"]}


def _read_proxy_payload_logs(limit: int) -> list:
    """讀 proxy_payload.log 並解析為結構化 JSON（``/logs`` 與 ``/logs/proxy`` 共用）。"""
    base_dir = resource_manager.get_base_path()
    log_path = Path(base_dir) / "logs" / "proxy_payload.log"
    if not Path(log_path).exists():
        return []

    logs = []
    try:
        content = resource_manager.read_file_text(log_path)
        if content:
            lines = content.splitlines()[-limit:]
            for line in reversed(lines):
                try:
                    data = json.loads(line.strip())
                    logs.append(data)
                except Exception:
                    pass
    except Exception:
        pass
    return logs


@router.get("/logs")
async def get_logs_alias(
    limit: int = 20,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
):
    """取得代理 Payload 日誌 (別名)。含實際請求 payload → 僅限三元組角色。"""
    _require_privileged(identity)
    return _read_proxy_payload_logs(limit)


@router.get("/system_logs")
async def get_system_logs_alias(
    lines: int = 50,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
):
    """取得系統狀態日誌 (別名)。僅限三元組角色。"""
    _require_privileged(identity)
    return _read_system_logs(lines)


@router.get("/tools")
async def list_all_tools(
    request: Request,
    _identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
):
    """列出所有可用工具 (SSOT 整合版)"""
    ctx = request.app.state.context
    tools_dict = {}
    
    # 整合來源：1. 內建工具, 2. 記憶工具, 3. 插件
    schemas_and_categories = [
        ("builtin_tools_schema", "系統核心"),
        ("memory_tools_schema", "記憶層"),
        ("plugin_schemas", "擴充插件")
    ]
    
    for key, category in schemas_and_categories:
        for schema in ctx.get(key, []):
            fn = schema.get("function", {})
            name = fn.get("name", "Unknown")
            # SSOT Overwrite: 後面的來源會覆蓋前面同名的工具
            tools_dict[name] = {
                "name": name,
                "description": fn.get("description", "無描述"),
                "category": category
            }
    
    # 如果系統降級且缺失記憶系統，加入一個提示性工具
    if ctx.get("system_degraded") and not ctx.get("memory_tools_schema"):
         tools_dict["!_MEMORY_SYSTEM_OFFLINE"] = {
            "name": "⚠️ 記憶系統離線",
            "description": f"警告：{ctx.get('degraded_reason', '記憶模組未載入')}。Agent 將無法使用長期記憶。",
            "category": "系統警報"
        }

    all_tools = list(tools_dict.values())

    # 4. MCP 動態工具
    # SSOT: manager.get_all_tools() 已保證每個工具只回傳一筆 canonical name (server__tool)。
    # 此層僅負責轉發，不做任何去重或名稱轉換（顯示美化由前端負責，遵守 SSOT_MASTER_MAP §5）。
    try:
        from ..main import protocol_manager
        all_mcp = await protocol_manager.get_all_tools()
        for tool_schema in all_mcp:
            fn = tool_schema.get("function", {})
            raw_name = fn.get("name", "Unknown")
            server_id = tool_schema.get("server", "MCP")
            all_tools.append({
                "name": raw_name,
                "description": fn.get("description", "自動偵測之 MCP 工具"),
                "category": f"MCP: {server_id}"
            })
    except Exception as e:
        logger.warning(f"Failed to integrate MCP tools into list: {e}")

    return {
        "tools": all_tools, 
        "total_count": len(all_tools)
    }


@router.get("/tools_stats")
async def get_tools_stats(
    request: Request,
    _identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
):
    """工具詳情 (包含 MCP 動態工具)"""
    ctx = request.app.state.context
    
    mcp_schemas = []
    try:
        from ..main import protocol_manager
        # get_all_tools 回傳 List[Dict]  --包含 server 屬性
        all_mcp = await protocol_manager.get_all_tools()
        seen_raw: set[str] = set()
        for tool_schema in all_mcp:
            fn = tool_schema.get("function", {})
            raw_name = fn.get("name", "Unknown")
            if raw_name in seen_raw:
                continue  # 跳過 alias 重複
            seen_raw.add(raw_name)
            display_name = raw_name.split("__", 1)[1] if "__" in raw_name else raw_name
            server_id = tool_schema.get("server", "MCP")
            mcp_schemas.append({
                "type": "function",
                "function": {
                    "name": display_name,
                    "raw_name": raw_name,
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {})
                },
                "category": f"MCP: {server_id}",
                # 靈魂工具清單存 `伺服器/工具`（ADR-0281 T2）：分開給，不從廣告名切
                "server": server_id,
                "tool": tool_schema.get("tool") or display_name,
            })
    except Exception as e:
        logger.warning(f"Failed to fetch MCP stats: {e}")

    return {
        "builtin_schemas": ctx.get("builtin_tools_schema", []),
        "memory_schemas": ctx.get("memory_tools_schema", []),
        "plugin_schemas": ctx.get("plugin_schemas", []),
        "mcp_schemas": mcp_schemas,
        "tool_companions": {tool: list(names) for tool, names in TOOL_COMPANIONS.items()},
    }


@router.get("/events")
async def get_recent_events(
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
):
    """系統事件最近 100 筆。低敏感但非公開探針 → 需已驗證身分（角色不限）。"""
    if not identity:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    return system_events


@router.get("/logs/recent")
async def get_recent_logs(
    lines: int = 50,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
):
    """讀取 data/logs/system_status.log 的最新幾行。僅限三元組角色。"""
    _require_privileged(identity)
    return _read_system_logs(lines)


@router.get("/logs/proxy")
async def get_proxy_payload_logs(
    limit: int = 20,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
):
    """讀取 proxy_payload.log 並解析為結構化 JSON。含實際請求 payload → 僅限三元組角色。"""
    _require_privileged(identity)
    return _read_proxy_payload_logs(limit)


@router.get("/monitor/summary")
async def get_system_monitor_summary():
    """提供系統運行狀態的綜合彙整"""
    base_dir = resource_manager.get_base_path()

    # 統計最近一小時的請求量
    proxy_log = Path(base_dir) / "logs" / "proxy_payload.log"
    requests_last_hour = 0
    if Path(proxy_log).exists():
        try:
            now_epoch = get_current_epoch()
            one_hour_ago = datetime.fromtimestamp(now_epoch - 3600, ZoneInfo("Asia/Taipei"))
            content = resource_manager.read_file_text(proxy_log)
            if content:
                for line in reversed(content.splitlines()[-200:]):
                    try:
                        data = json.loads(line.strip())
                        ts = datetime.fromisoformat(data["timestamp"])
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=ZoneInfo("Asia/Taipei"))
                            
                        if ts > one_hour_ago:
                            requests_last_hour += 1
                        else:
                            break
                    except Exception:
                        pass
        except Exception:
            pass

    return {
        "status": "healthy",
        "requests_last_hour": requests_last_hour,
        "uptime_seconds": int(get_current_epoch()),  # 簡化，實際應記錄啟動時間
        "active_events_count": len(system_events),
    }


@router.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await websocket.accept()
    try:
        last_count = len(system_events)
        while True:
            if len(system_events) > last_count:
                new_events = system_events[last_count:]
                for ev in new_events:
                    await websocket.send_json(ev)
                last_count = len(system_events)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
