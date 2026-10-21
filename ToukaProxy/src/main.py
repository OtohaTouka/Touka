"""Touka Proxy Server (閘道大腦).

負責所有 Agent 的身分驗證、工具調度、多脈衝 ReAct 循環管理以及與外部 MCP 伺服器的通訊橋接。
"""

from __future__ import annotations

import importlib
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from memory_server.factual import FactualStore

from src.core.exceptions import ErrorCode, ToukaException

from . import config  # noqa: F401 # 確保環境變數在任何其他 import 之前載入
from .config import (
    ALLOWED_ORIGINS,
    ANTHROPIC_API_BASE_URL,
    CONFIG_DIR,
    MAX_REQUEST_BODY_BYTES,
    get_config_dir,
    is_bootstrap_enabled,
    persist_generated_secret,
    persist_generated_secrets,
    read_persisted_secret_values,
    settings,
    strip_anthropic_sdk_env_overrides,
)
from .core.logging_config import setup_logging

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import Request

    from .models.identity import AgentIdentity
    from .protocols.mcp.oauth.bootstrap import McpOAuthRuntimeBundle
    from .protocols.mcp.oauth.models import OAuthAuthorizationSlotKey
    from .protocols.mcp.oauth.workload.bootstrap import McpWorkloadOAuthRuntimeBundle
from .handlers.adapters.stream_timeout import liveness_event_hooks
from .middleware.request_size_limit import RequestSizeLimitMiddleware
from .middleware.security_headers import SecurityHeadersMiddleware
from .protocols.client import ProtocolManager
from .protocols.mcp.oauth.bootstrap import (
    McpOAuthAuthorizationFence,
    McpOAuthBootstrapConfig,
    build_mcp_oauth_runtime_bundle,
)
from .protocols.mcp.oauth.workload.adapters import (
    CurrentOwnerAuthority,
    CurrentWorkloadPolicyAuthority,
)
from .protocols.mcp.oauth.workload.bootstrap import (
    McpWorkloadOAuthBootstrapConfig,
    build_mcp_workload_oauth_runtime_bundle,
)
from .protocols.registry import ProtocolRegistry
from .routers.admin import router as admin_router
from .routers.agents import router as agents_router
from .routers.ask_user import router as ask_user_router
from .routers.auth import router as auth_router
from .routers.autonomy import router as autonomy_router
from .routers.bots import router as bots_router
from .routers.chat_upload import router as chat_upload_router
from .routers.config import router as config_router
from .routers.connections import router as connections_router
from .routers.creation import router as creation_router
from .routers.custom_models import router as custom_models_router
from .routers.debug import router as debug_router
from .routers.dispatch_callback import router as dispatch_callback_router
from .routers.editor import router as editor_router
from .routers.feedback import router as feedback_router
from .routers.group_settings import router as group_settings_router
from .routers.heartbeat import router as heartbeat_router
from .routers.identity import router as identity_router
from .routers.mcp import MCP_MANUAL_START_STARTER
from .routers.mcp import router as mcp_router
from .routers.mcp_oauth import router as mcp_oauth_router
from .routers.mcp_oauth_workload import router as mcp_oauth_workload_router
from .routers.models import router as models_router
from .routers.observability import collect_memory_notices
from .routers.observability import router as observability_router
from .routers.permissions import router as permissions_router
from .routers.proxy import router as proxy_router
from .routers.setup import router as setup_router
from .routers.skills import router as skills_router  # Add this
from .routers.souls import router as souls_router
from .routers.stats import router as stats_router
from .routers.system import router as system_router
from .routers.terminal import router as terminal_router
from .routers.tools_approval import router as tools_approval_router
from .routers.ui_interjections import router as ui_interjections_router
from .routers.ui_sessions import router as ui_sessions_router
from .services import audit_service, avatar_service
from .services.auth_service import AuthService
from .services.auto_reviewer import auto_reviewer
from .services.autonomy import autonomy_service
from .services.backup_scheduler_service import backup_scheduler
from .services.bot_service import bot_service
from .services.completion import complete_once
from .services.continuity_events import start_recording, stop_recording
from .services.creation.evaluation import eval_runner
from .services.creation.model_judge import ModelJudge, resolve_judge_model
from .services.dispatch_supervisor import DispatchSupervisor
from .services.identity_router import get_verified_identity
from .services.identity_service import identity_service
from .services.isolation.status import isolation_monitor
from .services.model_manager import model_manager
from .services.permission_service import permission_service
from .services.resource_manager import resource_manager
from .services.sns_gateway import sns_gateway
from .tools.builtin import builtin_tools_schema, tool_registry
from .tools.plugin_loader import load_plugins
from .utils.async_db import init_db_async, session_service
from .utils.task_utils import cancel_all_background_tasks, spawn_logged

# 初始化日誌
logger = setup_logging()


# 全域實例與狀態
def _build_anthropic_client() -> AsyncAnthropic:
    """主迴圈 Claude client：目的地固定（不看 ANTHROPIC_BASE_URL）、不跟隨轉址（ADR-0247）。"""
    strip_anthropic_sdk_env_overrides()  # ToukaMemory 匯入時可能把 .env 灌回環境
    return AsyncAnthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        base_url=ANTHROPIC_API_BASE_URL,
        max_retries=0,  # 重試由主迴圈控制（ADR-0248 3b-U5a）
        http_client=DefaultAsyncHttpxClient(  # 禁令 22（SDK 預設會跟隨）
            follow_redirects=False,
            event_hooks=liveness_event_hooks(),  # 串流閒置計時看位元組（ADR-0248）
        ),
    )


anthropic_client = _build_anthropic_client()


# Auto 審查員經 complete_once（ADR-0246 U2c-3）；組裝根注入，避免工具模組循環 import
auto_reviewer.set_completer(complete_once)


def _configure_eval_judge(enabled: str | None, model: str | None) -> None:
    """ADR-0069 Phase D — 選用：啟動時注入 ModelJudge 供 EVAL LLM 計分。

    預設關閉；雙閘：需 ``TOUKA_EVAL_JUDGE_ENABLED`` 為真 ＋ 呼叫端 eval_scoring=True 才會花
    token。沒有寫死的評分模型預設：啟用但沒設 ``TOUKA_EVAL_JUDGE_MODEL`` 時不啟用。
    """
    requested, judge_model = resolve_judge_model(enabled, model)
    if judge_model:
        eval_runner.set_judge(ModelJudge(complete_once, judge_model))
        logger.info("ModelJudge enabled for creation evals (model=%s).", judge_model)
    elif requested:
        logger.error(
            "ModelJudge 未啟用：TOUKA_EVAL_JUDGE_ENABLED 為真但未設定 TOUKA_EVAL_JUDGE_MODEL"
            "（不再使用寫死的預設模型）。"
        )


_configure_eval_judge(
    os.environ.get("TOUKA_EVAL_JUDGE_ENABLED"), os.environ.get("TOUKA_EVAL_JUDGE_MODEL")
)

registry = ProtocolRegistry(Path(CONFIG_DIR) / "server_config.json")


protocol_manager = ProtocolManager(registry)

# 隔離封鎖埠的上游來源：已設定的全部模型連線 URL（存檔後立刻生效、不等探測）。在這裡接，
# 避開 isolation → models → agent_service → permission_service → isolation 的循環匯入。
isolation_monitor.add_upstream_source(model_manager.route_endpoints)


def _build_mcp_oauth_authorization_fence() -> McpOAuthAuthorizationFence:
    auth_service = AuthService()

    def human_authorizer(slot: OAuthAuthorizationSlotKey) -> bool:
        user = auth_service.get_user_by_id(slot.human_principal_id)
        return bool(
            user is not None
            and user.is_active
            and (user.role == "admin" or slot.execution_agent_id in user.owned_agents)
        )

    def installation_authorizer(slot: OAuthAuthorizationSlotKey) -> bool:
        identity = identity_service.get_identity(slot.execution_agent_id)
        return permission_service.is_mcp_server_allowed(
            slot.installation_id,
            identity,
        )

    return McpOAuthAuthorizationFence(
        registry=registry,
        feature_enabled=lambda: (
            settings.TOUKA_MCP_OAUTH_USER_ENABLED or settings.TOUKA_MCP_OAUTH_DELEGATION_ENABLED
        ),
        current_generation=protocol_manager.get_installation_generation,
        human_authorizer=human_authorizer,
        installation_authorizer=installation_authorizer,
    )


def _current_workload_policy_snapshot() -> dict[str, Any]:
    """Return the current non-secret RBAC/identity/PaC material for ticket digests。"""

    def dump(model: Any | None) -> dict[str, Any]:
        return model.model_dump(mode="json") if model is not None else {}

    return {
        "identity_map": dump(permission_service.identity_map),
        "permissions": dump(permission_service.roles_config),
        "policies": dump(permission_service.policies),
    }


SYSTEM_DEGRADED = False
DEGRADED_REASON = ""
plugin_schemas, plugin_registry = [], {}
memory_tools_schema, memory_tool_registry = [], {}


# 嘗試動態載入外部記憶模組
def load_memory_system():
    """動態載入 ToukaMemory 模組 (整合模式)。"""
    global memory_tool_registry, memory_tools_schema, SYSTEM_DEGRADED, DEGRADED_REASON
    logger.info(f"🔍 Searching for MEMORY SYSTEM in PYTHONPATH: {sys.path}")
    try:
        # 使用動態匯入以獲得更詳細的錯誤資訊
        spec = importlib.util.find_spec("memory_server")
        if spec is None:
            raise ImportError("Module 'memory_server' not found in any search paths.")  # noqa: TRY003, TRY301

        # 使用 import_module 避開 IDE 的靜態匯入檢查 (Pylance/Ruff)
        memory_tools = importlib.import_module("memory_server.tools")

        # 防禦性讀取：確保整合模式入口存在
        m_registry = getattr(memory_tools, "memory_tool_registry", {})
        m_schema = getattr(memory_tools, "memory_tools_schema", [])

        if not m_registry:
            logger.warning("⚠️ Memory system loaded but 'memory_tool_registry' is empty or missing.")

        memory_tool_registry, memory_tools_schema = m_registry, m_schema
        logger.info(
            f"🧠 Successfully loaded {len(memory_tool_registry)} memory tools from {spec.origin}"
        )
    except ImportError as e:
        # 區分是找不到模組，還是模組內部的依賴出問題
        error_msg = str(e)
        if "memory_server" in error_msg:
            logger.warning(f"🚨 MEMORY SYSTEM missing: {error_msg}")
            DEGRADED_REASON = "Memory System module not found. "
        else:
            logger.error(f"❌ MEMORY SYSTEM dependency error: {error_msg}", exc_info=True)
            DEGRADED_REASON = f"Memory System dependency error: {error_msg} "

        SYSTEM_DEGRADED = True
        return False
    except Exception as e:
        logger.error(f"🔥 Unexpected error loading memory system: {e}", exc_info=True)
        SYSTEM_DEGRADED, DEGRADED_REASON = True, f"Memory System crash: {str(e)} "
        return False
    else:
        return True


load_memory_system()


def build_base_context() -> dict:
    """組裝 pipeline 的 base context（SSOT）。

    lifespan 存入 ``app.state.context``；計分卡 CLI（scripts/run_scorecard.py）亦呼叫此函式
    建構同一份 base context → 單一來源、與生產不漂移（ADR-0101）。含同步可得的部分
    （記憶工具、內建工具、protocol_manager、註冊表、降級旗標）；plugins 由 lifespan 於啟動時
    非同步載入後補上（CLI 無 plugins／未連線 MCP，當前計分卡情境不需要）。
    """
    return {
        "anthropic_client": anthropic_client,
        "protocol_manager": protocol_manager,
        "tool_registry": tool_registry,
        "builtin_tools_schema": builtin_tools_schema,
        "memory_tool_registry": memory_tool_registry,
        "memory_tools_schema": memory_tools_schema,
        "plugin_registry": plugin_registry,
        "plugin_schemas": plugin_schemas,
        "system_degraded": SYSTEM_DEGRADED,
        "degraded_reason": DEGRADED_REASON,
    }


def _prepare_memory_store(app: FastAPI) -> None:
    """啟動時建立共用事實庫；結構升級同步跑完，不留背景工作、不載入向量模組。"""
    if SYSTEM_DEGRADED:
        return
    try:
        app.state.context["memory_factual_store"] = FactualStore.shared()
    except Exception:
        logger.exception("[Startup] memory store could not be opened")
        app.state.context["memory_store_open_failed"] = True  # /memory 只在還沒建成時出提示


@dataclass
class _LifespanResources:
    """Proxy startup 已取得資源的精確清理狀態。"""

    oauth_bundle: McpOAuthRuntimeBundle | None = None
    workload_bundle: McpWorkloadOAuthRuntimeBundle | None = None
    dispatch_supervisor: DispatchSupervisor | None = None
    protocol_cleanup_required: bool = False
    protocol_services_bound: bool = False
    bot_cleanup_required: bool = False


@asynccontextmanager
async def _lifespan_runtime(
    app: FastAPI,
    resources: _LifespanResources,
) -> AsyncIterator[None]:
    """管理應用程式生命週期 (Startup & Shutdown)。"""
    global plugin_schemas, plugin_registry
    oauth_bundle = None
    workload_bundle = None

    # --- 自我修復與 SSOT 引導 ---
    if os.environ.get("TOUKA_BOOTSTRAP_ENABLED", "true").lower() == "true":
        from .config import verify_env_persistence_integrity
        from .utils.bootstrap import ensure_configs, validate_live_configs

        ensure_configs()
        # 自癒可能從範本補出 connections.json：重讀一次，隔離的封鎖埠不必等啟動探測。
        model_manager.reload_connections()
        # ADR-0096：啟動自我健檢——驗證 live 設定檔合法且符合 schema（fail-fast）。
        # 守 gitignore 的 live 真檔（範本由 Guard #34 靜態守衛）；缺檔已由上一步還原。
        validate_live_configs()
        # ADR-0151：密鑰持久化 .env『幽靈檔』分歧守衛——寫入的 .env 與 compose 載入的 .env 分歧
        # 時 loud log（不中止啟動：認證仍可用臨時密鑰，中止反更糟）。解耦後預設應恆健康。
        verify_env_persistence_integrity()
    else:
        logger.info("🛡️ 自我修復引導機制已由環境變數禁用")

    # 安全打印初始化狀態
    try:
        soul_root = resource_manager.get_workspace_root() / "data" / "soul"
        logger.info(f"🧬 SoulService ready (Data Root: {soul_root})")
    except Exception:
        logger.info("🧬 SoulService initialized")

    # --- Bootstrap Human Users & Migrate Agents ---
    try:
        from .services.agent_service import agent_service
        from .services.auth_service import AuthService

        all_agent_ids = [a["id"] for a in agent_service.get_all_agents()]
        AuthService().ensure_admin_user(all_agent_ids)
    except Exception as e:
        logger.error(f"❌ Failed to bootstrap admin user: {e}", exc_info=True)

    # 初始化 Context 並存入 app.state（SSOT：與計分卡 CLI 共用 build_base_context）
    app.state.context = build_base_context()
    # 記憶提示（只給畫面看、不影響模型）：啟動時只看檔名算一次，/memory 回傳
    app.state.context["memory_notices"] = collect_memory_notices()
    # 事實庫：建立共用實例，結構升級在這裡同步跑完（在任何 spawn_logged 之前）
    _prepare_memory_store(app)

    # ADR-0102：啟動遷移舊 MCP server key（curated id 殘留 → install 衍生 key）並重載 registry，
    # 讓技能宣告的 server 名對得上（修「舊裝 curated-mcp-docgen vs 技能要 mcp-docgen」無聲失敗）。
    try:
        from .utils.skill_mcp_wiring import migrate_stale_server_keys

        if migrate_stale_server_keys():
            protocol_manager.registry.load_config()
    except Exception as e:
        logger.error(f"⚠️ MCP server key 遷移失敗（略過，不影響啟動）：{e}", exc_info=True)

    try:
        oauth_bundle = await build_mcp_oauth_runtime_bundle(
            config=McpOAuthBootstrapConfig(
                enabled=settings.TOUKA_MCP_OAUTH_USER_ENABLED,
                delegation_enabled=settings.TOUKA_MCP_OAUTH_DELEGATION_ENABLED,
                config_dir=get_config_dir(),
                key_source=settings.TOUKA_MCP_OAUTH_KEY_SOURCE,
                current_key=settings.TOUKA_MCP_OAUTH_KEY,
                retiring_keys_json=settings.TOUKA_MCP_OAUTH_RETIRED_KEYS,
                callback_base_url=settings.TOUKA_MCP_OAUTH_CALLBACK_BASE_URL,
                callback_response_mode=(settings.TOUKA_MCP_OAUTH_CALLBACK_RESPONSE_MODE),
                query_callback_canary_verified=(
                    settings.TOUKA_MCP_OAUTH_QUERY_CALLBACK_CANARY_VERIFIED
                ),
                bootstrap_enabled=is_bootstrap_enabled(),
                rotation_phase=settings.TOUKA_MCP_OAUTH_ROTATION_PHASE,
                key_epoch=settings.TOUKA_MCP_OAUTH_KEY_EPOCH,
            ),
            registry=registry,
            authorization_fence=_build_mcp_oauth_authorization_fence(),
            persist_secret=persist_generated_secret,
            persist_secrets=persist_generated_secrets,
            read_secrets=lambda: read_persisted_secret_values(
                (
                    "TOUKA_MCP_OAUTH_KEY",
                    "TOUKA_MCP_OAUTH_RETIRED_KEYS",
                    "TOUKA_MCP_OAUTH_ROTATION_PHASE",
                    "TOUKA_MCP_OAUTH_KEY_EPOCH",
                )
            ),
        )
        resources.oauth_bundle = oauth_bundle
    except Exception as exc:
        logger.exception(
            "MCP OAuth runtime unavailable error_type=%s",
            type(exc).__name__,
        )

    workload_bundle = await build_mcp_workload_oauth_runtime_bundle(
        config=McpWorkloadOAuthBootstrapConfig(
            direct_enabled=settings.TOUKA_MCP_OAUTH_WORKLOAD_ENABLED,
            delegation_enabled=settings.TOUKA_MCP_OAUTH_DELEGATION_ENABLED,
            config_dir=get_config_dir(),
            filesystem_kind=(settings.TOUKA_MCP_OAUTH_WORKLOAD_FILESYSTEM_KIND),
            key_source=settings.TOUKA_MCP_OAUTH_WORKLOAD_KEY_SOURCE,
            current_key=settings.TOUKA_MCP_OAUTH_WORKLOAD_KEY,
            retiring_keys_json=(settings.TOUKA_MCP_OAUTH_WORKLOAD_RETIRED_KEYS),
            bootstrap_enabled=is_bootstrap_enabled(),
        ),
        registry=registry,
        installation_generation_lookup=(protocol_manager.get_installation_generation),
        owners=CurrentOwnerAuthority(auth_service=AuthService()),
        policy=CurrentWorkloadPolicyAuthority(
            identity_lookup=identity_service.get_identity,
            mcp_tool_allowed=permission_service.is_mcp_tool_allowed,
            policy_evaluate=permission_service.evaluate_policy,
            policy_snapshot=_current_workload_policy_snapshot,
        ),
        persist_secret=persist_generated_secret,
        persist_secrets=persist_generated_secrets,
        read_secrets=lambda: read_persisted_secret_values(
            (
                "TOUKA_MCP_OAUTH_WORKLOAD_KEY",
                "TOUKA_MCP_OAUTH_WORKLOAD_RETIRED_KEYS",
                "TOUKA_MCP_OAUTH_WORKLOAD_ROTATION_PHASE",
                "TOUKA_MCP_OAUTH_WORKLOAD_KEY_EPOCH",
            )
        ),
        audit_sink=audit_service.audit_service.emit_security_event,
        p8a_runtime=(oauth_bundle.runtime if oauth_bundle is not None else None),
        p8a_store=(oauth_bundle.store if oauth_bundle is not None else None),
        close_handles=protocol_manager.close_workload_authority_handles,
        close_all_handles=protocol_manager.close_all_workload_handles,
    )
    resources.workload_bundle = workload_bundle
    resources.protocol_services_bound = True
    protocol_manager.set_oauth_auth_service(
        oauth_bundle.runtime if oauth_bundle is not None else None
    )
    protocol_manager.set_workload_oauth_service(
        workload_bundle.runtime if workload_bundle is not None else None
    )
    if workload_bundle is not None and oauth_bundle is not None:
        oauth_bundle.runtime.set_delegation_disconnect_fence(
            workload_bundle.control.delegation_disconnect_guard
        )
    if workload_bundle is not None:
        registry.freeze_workload_oauth_config()
        app.state.context["mcp_workload_oauth_runtime"] = workload_bundle.runtime
        app.state.context["mcp_workload_oauth_control"] = workload_bundle.control
        app.state.context["mcp_workload_oauth_rotation"] = workload_bundle.rotation
        spawn_logged(
            workload_bundle.run_maintenance(),
            name="mcp_workload_oauth_maintenance",
        )
    if oauth_bundle is not None:
        app.state.context["mcp_oauth_runtime"] = oauth_bundle.runtime
        app.state.context["mcp_oauth_rotation"] = oauth_bundle.rotation
        spawn_logged(
            oauth_bundle.run_maintenance(),
            name="mcp_oauth_maintenance",
        )

    resources.protocol_cleanup_required = True
    await protocol_manager.initialize_all()
    app.state.context[MCP_MANUAL_START_STARTER] = protocol_manager.manual_starter

    # ADR-0102：技能↔MCP 接線健檢——技能宣告的 hub MCP 未註冊就大聲告警（非 fail-fast）。
    # live server_config CI 看不到，故在啟動真實環境檢查（比照 ADR-0096）。
    try:
        from .utils.skill_mcp_wiring import validate_skill_mcp_wiring

        _registered = set(protocol_manager.registry.get_all_servers().keys())
        for _w in validate_skill_mcp_wiring(_registered):
            logger.warning(f"⚠️ [Skill-MCP] {_w}")
    except Exception as e:
        logger.error(f"⚠️ 技能↔MCP 接線健檢失敗（略過）：{e}", exc_info=True)
    plugin_schemas, plugin_registry = await load_plugins(
        os.environ.get("PLUGIN_DIR", "src/plugins")
    )

    # 更新 Plugin 資訊到 Context
    app.state.context["plugin_schemas"] = plugin_schemas
    app.state.context["plugin_registry"] = plugin_registry

    async def initialize_and_verify_models() -> None:
        """初始化並驗證所有啟用的模型連線。"""
        logger.info("📡 Initializing model connections...")
        if config.ENABLE_DYNAMIC_MODELS:
            logger.info("🔭 Dynamic model discovery enabled.")
            # 供應商清單由 Provider Registry (SSOT) 驅動，與 /models/refresh 共用同一條路徑
            # （不再於此複製一份硬編碼 gather）。refresh 內已含自訂連線發現。
            await model_manager.refresh_all_dynamic_models()
        else:
            # 即使官方動態發現關閉，仍須從 connections.json 建立記憶體內的自訂路由快照；
            # endpoint＋key 不進 models.json，且 provider=proxy 的 Chat 控制面會在此被排除。
            await model_manager.fetch_custom_connections_models()

        await model_manager.check_models_health()
        logger.info(f"✅ {len(model_manager.get_models())} models available.")

    # P5d：fire-and-forget 模型初始化——以 spawn_logged 確保背景失敗會被記錄而非靜默吞掉
    spawn_logged(initialize_and_verify_models(), name="initialize_and_verify_models")
    await init_db_async()
    await session_service.recover_pending_restore()
    resource_manager.bind_ui_conversation_reader(session_service)
    # 可信活動事件：綁定主事件迴圈並登記給 Memory（之後才會記）
    start_recording()

    # P5c：當機復原可見化 — 掃描上次未跑完的背景心跳迴圈（偵測＋清 stale），供 /stats 顯示
    try:
        autonomy_service.scan_and_clear_interrupted()
    except Exception:
        logger.exception("[Startup] 未完成背景任務掃描失敗")

    # ADR-0082：dispatch 併入 proxy 容器——supervisor spawn/監督 localhost 子程序。
    # 預設關閉（settings.DISPATCH_AUTOSPAWN）；compose 於 proxy 服務顯式開啟。
    # 一律讀 settings 屬性（非 legacy 模組常數），保留測試 monkeypatch 切換能力。
    dispatch_supervisor = DispatchSupervisor()
    resources.dispatch_supervisor = dispatch_supervisor
    app.state.dispatch_supervisor = dispatch_supervisor
    if config.settings.DISPATCH_AUTOSPAWN:
        await dispatch_supervisor.start()

    # 啟動 Bot 服務 (排程引擎已遷移至外部 ToukaHeartbeat)
    resources.bot_cleanup_required = True
    sns_gateway.context = app.state.context
    await bot_service.start_bots()

    # ADR-0109：proxy 內自動備份排程器（每小時檢查；預設停用，
    # 由 config/backup_schedule.json 控制）。
    # 以 spawn_logged 啟動，cancel_all_background_tasks() 於關機時取消。
    spawn_logged(backup_scheduler.run_loop(), name="backup_scheduler")
    yield


async def _cleanup_proxy_runtime(app: FastAPI, resources: _LifespanResources) -> None:
    """嘗試所有 cleanup；任一步失敗仍繼續後續清理。"""
    cleanup_stack = AsyncExitStack()
    if resources.oauth_bundle is not None:
        cleanup_stack.push_async_callback(resources.oauth_bundle.aclose)
    if resources.workload_bundle is not None:
        cleanup_stack.push_async_callback(resources.workload_bundle.aclose)
    if resources.dispatch_supervisor is not None:
        cleanup_stack.push_async_callback(resources.dispatch_supervisor.stop)

    context: dict[str, Any] | None = getattr(app.state, "context", None)
    if context is not None:
        for key in (
            "mcp_workload_oauth_runtime",
            "mcp_workload_oauth_control",
            "mcp_workload_oauth_rotation",
            "mcp_oauth_runtime",
            "mcp_oauth_rotation",
        ):
            cleanup_stack.callback(context.pop, key, None)
    if resources.protocol_services_bound:
        cleanup_stack.callback(protocol_manager.set_oauth_auth_service, None)
        cleanup_stack.callback(protocol_manager.set_workload_oauth_service, None)
    if resources.protocol_cleanup_required:
        cleanup_stack.push_async_callback(protocol_manager.shutdown)
    if resources.bot_cleanup_required:
        cleanup_stack.push_async_callback(bot_service.stop_bots)

    # fire-and-forget tasks 仍可能依賴上述服務，故必須最先停止；AsyncExitStack
    # 即使中途 cleanup 失敗也會繼續。
    cleanup_stack.push_async_callback(cancel_all_background_tasks)
    cleanup_stack.callback(protocol_manager.begin_shutdown)
    cleanup_stack.callback(stop_recording)  # 最先執行：關機後不再排新的事件寫入
    await cleanup_stack.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """管理應用程式生命週期；任何退出路徑都完整清理。"""
    resources = _LifespanResources()
    try:
        async with _lifespan_runtime(app, resources):
            yield
    finally:
        await _cleanup_proxy_runtime(app, resources)


app = FastAPI(title="Touka Proxy", version="0.4.1", lifespan=lifespan)


# --- Sovereign Output Router (SSOT: 依照 Agent ID 隔離輸出服務) ---
@app.get("/v1/outputs/{agent_id}/{filename}")
async def get_sovereign_output(
    agent_id: str,
    filename: str,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
):
    """動態轉譯 Agent 的私有輸出目錄並提供檔案 (Sovereignty Reinforced)。

    驗證 identity.agent_id 是否與請求的 agent_id 一致。
    """
    if identity.agent_id != agent_id:
        logger.warning(f"🚨 Output Violation! Req: {agent_id}, Identity: {identity.agent_id}")
        raise ToukaException(ErrorCode.AUTH_UNAUTHORIZED_ACCESS)

    # 取得 Agent 專屬輸出路徑
    output_dir = resource_manager.get_outputs_path(identity)
    if not output_dir:
        raise ToukaException(ErrorCode.RESOURCE_NOT_FOUND)

    file_path = Path(output_dir) / filename

    # 安全檢查：防止路徑穿越 (Path Traversal)
    abs_file_path = file_path.resolve()
    abs_output_dir = Path(output_dir).resolve()

    if not abs_file_path.is_relative_to(abs_output_dir):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    if not abs_file_path.exists():
        raise ToukaException(ErrorCode.RESOURCE_NOT_FOUND)

    return FileResponse(abs_file_path)


@app.get("/v1/users/{user_id}/avatar")
async def get_user_avatar(user_id: str):
    """ADR-0106: 公開提供使用者頭像圖片（顯示內容、低敏感）。

    刻意不要求認證——bearer token 在記憶體、`<img src>` 無法帶 Authorization header；
    端點僅依 opaque user_id 回圖片位元組，路徑穿越由 avatar_service 以 is_relative_to 防護。
    """
    found = avatar_service.find_avatar(user_id)
    if found is None:
        raise ToukaException(ErrorCode.RESOURCE_NOT_FOUND)
    path, media = found
    return FileResponse(path, media_type=media)


_base_origins = ["http://localhost:3000", "http://localhost:3001"]
_extra_origins = [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]
# E#17：本機/私網/webview 一律經 regex 放行——精確字串比對只列 localhost:*，會把
# 127.0.0.1 與區網 IP（手機/第二台裝置連自架）全數 CORS 擋死；vscode-webview://* 這種
# 萬用字元在 allow_origins 也無效。公開網域仍以 ALLOWED_ORIGINS 精確列舉。
_LOCAL_ORIGIN_REGEX = (
    r"^(https?://("
    r"localhost|127\.0\.0\.1|\[::1\]|"  # 本機
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"  # 私網 10.0.0.0/8
    r"192\.168\.\d{1,3}\.\d{1,3}|"  # 私網 192.168.0.0/16
    r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"  # 私網 172.16.0.0/12
    r")(:\d+)?|vscode-webview://.*)$"  # 任意埠；含 VS Code webview
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_base_origins + _extra_origins,
    allow_origin_regex=_LOCAL_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-New-Auth-Token"],  # 確保瀏覽器能正確讀取領取的通行證
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestSizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)


@app.middleware("http")
async def add_new_auth_token_header(request: Request, call_next: Any) -> Any:
    """LDH 全域攔截器：若請求過程中重新簽發了 Token，則將其置於回應標頭。"""
    response = await call_next(request)
    new_token = getattr(request.state, "new_auth_token", None)
    if new_token:
        response.headers["X-New-Auth-Token"] = new_token
    return response


@app.get("/")
async def root_identity():
    """ToukaI 識別根目錄，用於自動解鎖全功能模式"""
    return {
        "service": "ToukaProxy",
        "version": "0.4.1",
        "status": "DEGRADED" if SYSTEM_DEGRADED else "HEALTHY",
        "features": ["soul", "memory", "telemetry", "emergence", "stats", "mcp", "connections"],
        "identity": "🛡️ Touka Core Engine",
        "system_degraded": SYSTEM_DEGRADED,
        "degraded_reason": DEGRADED_REASON,
    }


# 定義支援 /v1 前綴與原路徑的路由器
CORE_ROUTERS = [
    proxy_router,
    chat_upload_router,
    souls_router,
    stats_router,
    models_router,
    connections_router,
    mcp_router,
    mcp_oauth_router,
    mcp_oauth_workload_router,
    observability_router,
    agents_router,
    custom_models_router,
    debug_router,
    bots_router,
    permissions_router,
    identity_router,
    skills_router,
    creation_router,
    autonomy_router,
    auth_router,
    admin_router,
    heartbeat_router,
    config_router,
    system_router,
    ui_sessions_router,
    ui_interjections_router,
    setup_router,
    feedback_router,
    ask_user_router,
    tools_approval_router,
]

# 定義僅使用原路徑的業務路由器
APP_ROUTERS = [
    editor_router,
    terminal_router,
]

for router in CORE_ROUTERS:
    app.include_router(router, prefix="/v1")
    app.include_router(router)

for router in APP_ROUTERS:
    app.include_router(router)

app.include_router(group_settings_router, tags=["Group Chat Settings"])
app.include_router(group_settings_router, prefix="/v1", tags=["Group Chat Settings"])

# ADR-0051: 內部 Dispatch 回呼端點（ToukaDispatch → ToukaProxy，限 localhost）
app.include_router(dispatch_callback_router, prefix="/v1")

if __name__ == "__main__":
    from .config import HOST, PORT

    logger.info(f"🚀 ToukaProxy starting on http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, reload=False, access_log=False)
