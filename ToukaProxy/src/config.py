"""Touka 全域配置管理 (Env SSOT)."""

import logging
import os
import secrets
import tempfile
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values, load_dotenv, set_key
from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# --- 路徑 SSOT 錨點 ---
PROXY_ROOT = Path(__file__).parent.parent.resolve()
WORKSPACE_ROOT = PROXY_ROOT.parent.resolve()


def get_workspace_root() -> Path:
    """獲取 Touka 系統工作區根目錄 (SSOT)."""
    return WORKSPACE_ROOT


def is_container_runtime() -> bool:
    """是否在 Docker 容器內執行（`/.dockerenv`；Podman 不認得，當成原生）。"""
    return Path("/.dockerenv").exists()


def resolve_env_file_path() -> Path:
    """根據執行模式動態解析 .env 檔案路徑 (SSOT).

    優先序：``TOUKA_ENV_FILE`` 顯式覆寫 > 整合/獨立自動偵測（禁令 38）。

    ``TOUKA_ENV_FILE`` 供部署顯式指定「持久化且被真正載入」的 .env——當該路徑無法由
    ``PROXY_ROOT`` / ``WORKSPACE_ROOT`` 推導時（如 docker：宿主 .env 經 bind mount 於容器內、
    且由 compose ``env_file`` 讀取，而容器內 ``WORKSPACE_ROOT`` 為 ``/app`` 指不到它）。
    ADR-0151：compose 設 ``/app/host_root/.env``——``./:/app/host_root`` 恆掛 compose 目錄
    （＝env_file 載入的同一 .env），與 ``WORKSPACE_ROOT`` 解耦（舊制綁 ``/app/workspace/.env``，
    使用者把 ``WORKSPACE_ROOT`` 指向 repo 以外即寫進沒人讀的幽靈 .env）。缺此覆寫時，寫入會落
    在容器暫存層 ``/app/ToukaProxy/.env``，宿主 .env 永不更新、容器重建即金鑰蒸發（root-cause-A）。
    """
    explicit = os.environ.get("TOUKA_ENV_FILE")
    if explicit:
        return Path(explicit)

    cwd = Path.cwd().resolve()
    is_proxy_dir = (cwd == PROXY_ROOT) or (PROXY_ROOT in cwd.parents)

    # 整合條件：(1) CWD 不在 Proxy 目錄下 且 根目錄 .env 存在；
    # (2) 或 TOUKA_INTEGRATED 環境變數為 true
    is_integrated = (not is_proxy_dir and (WORKSPACE_ROOT / ".env").exists()) or (
        os.environ.get("TOUKA_INTEGRATED") == "true"
    )

    if is_integrated:
        return WORKSPACE_ROOT / ".env"
    return PROXY_ROOT / ".env"


# --- 配置模型定義 ---


class Settings(BaseSettings):
    """Touka 全域配置模型 (Env SSOT).

    使用 Pydantic BaseSettings 實施強型別驗證與環境變數注入。
    優先級：環境變數 > .env 檔案 > 預設值。
    """

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """初始化設定物件，注入環境變數檔案路徑並讀取配置值。"""
        if "_env_file" not in kwargs:
            kwargs["_env_file"] = str(resolve_env_file_path())
        super().__init__(*args, **kwargs)

    # Server Settings
    PORT: int = 8000
    HOST: str = "0.0.0.0"  # noqa: S104

    # Provider API Keys
    GEMINI_API_KEY: str | None = None
    GEMINI_API_VERSION: str = "v1beta"

    GROK_API_KEY: str | None = None
    XAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    OPENAI_API_KEY: str | None = None
    DEEPSEEK_API_KEY: str | None = None
    LITELLM_API_KEY: str | None = None
    TAVILY_API_KEY: str | None = None

    # Provider URLs
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    LITELLM_BASE_URL: str = "http://localhost:4000"
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"

    # Research / Web Search Tuning（Tavily 調校，ADR-0090）
    TAVILY_SEARCH_DEPTH: str = "advanced"  # basic | advanced（advanced 較深、耗 2 API credits）
    TAVILY_MAX_RESULTS: int = 5  # 回傳結果數上限
    TAVILY_INCLUDE_ANSWER: bool = True  # 由 Tavily 生成直接答案摘要（對標大廠 AI Overview）
    # 搜尋備援鏈（ADR-0091，Web Search Phase C）：自架 SearXNG 端點（免金鑰、查詢不外洩）。
    # 空字串＝停用 SearXNG（auto 鏈跳過此供應者）。例：http://touka-searxng:8080
    SEARXNG_BASE_URL: str = ""

    # Feature Flags
    ENABLE_DYNAMIC_MODELS: bool = False
    # MCP P8-A interactive user OAuth remains opt-in until rollout verification completes.
    TOUKA_MCP_OAUTH_USER_ENABLED: bool = False
    # MCP P8-B direct workload and owner-delegation paths are separately opt-in.
    TOUKA_MCP_OAUTH_WORKLOAD_ENABLED: bool = False
    TOUKA_MCP_OAUTH_DELEGATION_ENABLED: bool = False
    TOUKA_MCP_OAUTH_WORKLOAD_FILESYSTEM_KIND: Literal[
        "local",
        "remote",
        "unknown",
    ] = "unknown"

    # MCP P8-A OAuth credential/key SSOT. Empty key is fail-closed; no ephemeral fallback.
    TOUKA_MCP_OAUTH_KEY_SOURCE: str = "managed"
    TOUKA_MCP_OAUTH_KEY: str = ""
    TOUKA_MCP_OAUTH_RETIRED_KEYS: str = "[]"
    TOUKA_MCP_OAUTH_ROTATION_PHASE: str = "stable"
    TOUKA_MCP_OAUTH_KEY_EPOCH: int = 1
    TOUKA_MCP_OAUTH_CALLBACK_BASE_URL: str = ""
    TOUKA_MCP_OAUTH_CALLBACK_RESPONSE_MODE: Literal["form_post", "query"] = "form_post"
    TOUKA_MCP_OAUTH_QUERY_CALLBACK_CANARY_VERIFIED: bool = False

    # P8-B uses an independent encryption-key namespace and store.
    TOUKA_MCP_OAUTH_WORKLOAD_KEY_SOURCE: str = "managed"
    TOUKA_MCP_OAUTH_WORKLOAD_KEY: str = ""
    TOUKA_MCP_OAUTH_WORKLOAD_RETIRED_KEYS: str = "[]"
    TOUKA_MCP_OAUTH_WORKLOAD_ROTATION_PHASE: str = "stable"
    TOUKA_MCP_OAUTH_WORKLOAD_KEY_EPOCH: int = 1

    # MCP P8-A deterministic limits/timers.
    OAUTH_STATE_TTL_SECONDS: int = 600
    OAUTH_ACCESS_REFRESH_LEEWAY_SECONDS: int = 60
    OAUTH_REFRESH_LEASE_SECONDS: int = 30
    OAUTH_DCR_LEASE_SECONDS: int = 30
    OAUTH_POPUP_POLL_INTERVAL_MS: int = 1000
    OAUTH_POPUP_TIMEOUT_MS: int = 120000
    OAUTH_CLEANUP_INTERVAL_SECONDS: int = 300
    OAUTH_WORKER_LIVENESS_SECONDS: int = 900
    OAUTH_KEY_ROTATION_BATCH_SIZE: int = 100
    OAUTH_MAX_CLOCK_ROLLBACK_SECONDS: int = 5
    OAUTH_RETRY_MAX_ATTEMPTS: int = 3
    OAUTH_RETRY_BASE_MS: int = 250
    OAUTH_RETRY_MAX_MS: int = 5000
    OAUTH_DISCONNECT_REVOCATION_TIMEOUT_SECONDS: int = 5
    OAUTH_CALLBACK_MAX_BODY_BYTES: int = 16 * 1024
    OAUTH_CALLBACK_MAX_FIELD_BYTES: int = 8 * 1024
    OAUTH_CALLBACK_MAX_FIELDS: int = 16
    OAUTH_HTTP_CONNECT_TIMEOUT_SECONDS: float = 10.0
    OAUTH_HTTP_READ_TIMEOUT_SECONDS: float = 15.0
    OAUTH_HTTP_WRITE_TIMEOUT_SECONDS: float = 10.0
    OAUTH_HTTP_POOL_TIMEOUT_SECONDS: float = 10.0
    OAUTH_HTTP_MAX_RESPONSE_BYTES: int = 1024 * 1024
    OAUTH_HTTP_MAX_JSON_DEPTH: int = 16
    OAUTH_HTTP_MAX_JSON_ITEMS: int = 4096

    # Identity & Security
    HOST_WORKSPACE_ROOT: str = str(WORKSPACE_ROOT)
    TOUKA_AUTH_SECRET: str = ""  # Leave empty → AuthSecretManager auto-generates a random key
    # H1 fix: defaults True (production-safe); override to False for local HTTP dev
    SECURE_COOKIES: bool = True
    # H2 fix: rate limiting for auth endpoints (sliding window)
    AUTH_RATE_LIMIT_MAX: int = 10  # max failed attempts per window
    AUTH_RATE_LIMIT_WINDOW_SEC: int = 900  # 15-minute window
    # Grace window for concurrent multi-tab refresh token rotation (Race Condition fix)
    AUTH_REFRESH_GRACE_PERIOD_SEC: int = 30
    # P19 fix: maximum request body size (10 MB default)
    MAX_REQUEST_BODY_BYTES: int = 10 * 1024 * 1024
    # ADR-0106: max avatar image size (2 MB; web-appropriate, well under request cap)
    AVATAR_MAX_BYTES: int = 2 * 1024 * 1024
    # Chat upload (Phase 2): max non-image document size (8 MB; under the 10 MiB request cap)
    UPLOAD_MAX_BYTES: int = 8 * 1024 * 1024
    # Chat upload (Phase 2): per-agent cumulative uploads quota (bounds disk-fill)
    UPLOAD_AGENT_QUOTA_BYTES: int = 200 * 1024 * 1024
    # Cloud deployment: disables LDH auto-token and uses X-Real-IP for trust detection
    CLOUD_MODE: bool = False
    # Comma-separated extra origins allowed by CORS (e.g., "https://chat.example.com")
    ALLOWED_ORIGINS: str = ""

    # Heartbeat Settings
    HEARTBEAT_EXTERNAL_URL: str | None = None
    # H5 fix: no hardcoded default; token is auto-generated by ToukaHeartbeat's
    # StorageService on first boot and stored in config.json (M6 fix).
    # Set HEARTBEAT_TOKEN env var to override (e.g., in Docker/production).
    HEARTBEAT_TOKEN: str = ""

    # Dispatch Settings（ADR-0082：dispatch 併入 proxy 容器，由 DispatchSupervisor spawn）
    # 預設不自動 spawn——測試（1500+ TestClient lifespan）與 native 模組化模式不受影響；
    # docker compose 於 proxy 服務顯式設 true。執行路徑一律讀 settings 屬性（可測試）。
    DISPATCH_AUTOSPAWN: bool = False
    DISPATCH_PORT: int = 8083
    # 綁定位址 secure-by-default：同容器 localhost IPC 即足；需對外發布 8083（主機原生
    # 外部 AI 連入）時由 compose 以環境變數顯式升開 0.0.0.0。
    DISPATCH_BIND_HOST: str = "127.0.0.1"
    DISPATCH_TOKEN: str = ""
    # dispatch server.py 路徑覆寫；空值 → <workspace 根>/ToukaDispatch/server.py
    # （native 與單一映像容器佈局適用同一條規則）
    DISPATCH_SERVER_PATH: str = ""
    # Tool & UI Settings
    PLUGIN_DIR: Path | None = None
    TOOL_BASE_URL: str | None = None
    UI_DEFAULT_URL: str = "http://localhost:3001"

    # App Data Directory
    MEMORY_APP_DATA_DIR: str = Field(default="data")

    # 隔離執行環境：容器內由 compose 設定；空值的意義見 services/isolation
    TOUKA_SANDBOX_STATE_DIR: str = ""
    TOUKA_SANDBOX_KERNEL_DIR: str = ""
    TOUKA_EXECUTOR_DIR: str = ""
    # compose 對外埠（`.env` 可改）；字串避免空值或打錯讓啟動崩潰，由封鎖埠集合解析
    PROXY_PORT: str = ""
    HEARTBEAT_PORT: str = ""
    CHAT_PORT: str = ""

    @computed_field
    @property
    def CONFIG_DIR(self) -> Path:
        """獲取設定檔目錄 (SSOT)"""
        env_path = os.environ.get("TOUKA_CONFIG_DIR")
        if env_path:
            return Path(env_path)
        return PROXY_ROOT / "config"

    @computed_field
    @property
    def GEMINI_BASE_URL(self) -> str:
        """獲取 Gemini API 基礎 URL。"""
        return f"https://generativelanguage.googleapis.com/{self.GEMINI_API_VERSION}"

    def get_app_data_dir(self) -> str:
        """獲取 App 資料目錄，並保留顯式絕對路徑的沙盒主權。"""
        configured_dir = Path(os.path.normpath(self.MEMORY_APP_DATA_DIR))
        if is_container_runtime() and not configured_dir.is_absolute():
            return "/app/data"
        return str(configured_dir)

    def get_full_env(self) -> dict[str, str]:
        """獲取目前進程的完整環境變數複本 (SSOT)."""
        return os.environ.copy()


# --- 實體化與引導 (Bootstrap Logic) ---

# 實體化全域配置物件 (SSOT 唯一出口)
settings = Settings()


def is_bootstrap_enabled() -> bool:
    """Bootstrap/自癒是否啟用（測試/沙盒設 TOUKA_BOOTSTRAP_ENABLED=false 時為 False）.

    與 main.py 的 live 設定啟動驗證同一個閘（ADR-0096）：關閉時嚴禁自動寫入系統檔
    （含首次生成密鑰的持久化），避免污染真實 .env。
    """
    return os.environ.get("TOUKA_BOOTSTRAP_ENABLED", "true").lower() == "true"


# 延遲匯入以避免循環依賴
def _trigger_bootstrap() -> None:
    if not is_bootstrap_enabled():
        return

    from .core.bootstrap import ensure_system_bootstrap  # noqa: LOCAL_IMPORT_BAN

    ensure_system_bootstrap(WORKSPACE_ROOT)


_trigger_bootstrap()


# --- SSOT 橋接函式 (Bridge Functions) ---


def get_config_dir() -> Path:
    """獲取設定檔目錄 (SSOT 橋接)."""
    return settings.CONFIG_DIR


def get_initial_admin_password_file() -> Path:
    """SSOT 路徑：首裝 bootstrap admin 密碼的一次性檔 (DA21).

    首次啟動時隨機 admin 密碼寫到此檔（而非明文寫進輪替/可搜尋的持久日誌），
    供操作者首次登入取用；首次改密後由 user_service 自動刪除、啟動時亦會清理殘留。
    """
    return get_config_dir() / "INITIAL_ADMIN_PASSWORD.txt"


def get_app_data_dir() -> str:
    """獲取 App 資料目錄 (SSOT 橋接)."""
    return settings.get_app_data_dir()


def get_full_env() -> dict[str, str]:
    """獲取目前進程的完整環境變數複本 (SSOT 橋接)."""
    return settings.get_full_env()


# --- 向後相容導出 (Legacy Exports) ---

CONFIG_DIR = settings.CONFIG_DIR
PORT = settings.PORT
HOST = settings.HOST

# API Keys
GEMINI_API_KEY = settings.GEMINI_API_KEY
ANTHROPIC_API_KEY = settings.ANTHROPIC_API_KEY
OPENAI_API_KEY = settings.OPENAI_API_KEY
DEEPSEEK_API_KEY = settings.DEEPSEEK_API_KEY
GROK_API_KEY = settings.GROK_API_KEY
XAI_API_KEY = settings.XAI_API_KEY
LITELLM_API_KEY = settings.LITELLM_API_KEY
TAVILY_API_KEY = settings.TAVILY_API_KEY
TAVILY_SEARCH_DEPTH = settings.TAVILY_SEARCH_DEPTH
TAVILY_MAX_RESULTS = settings.TAVILY_MAX_RESULTS
TAVILY_INCLUDE_ANSWER = settings.TAVILY_INCLUDE_ANSWER
SEARXNG_BASE_URL = settings.SEARXNG_BASE_URL

# Base URLs
OPENAI_BASE_URL = settings.OPENAI_BASE_URL
OLLAMA_BASE_URL = settings.OLLAMA_BASE_URL
LITELLM_BASE_URL = settings.LITELLM_BASE_URL
DEEPSEEK_BASE_URL = settings.DEEPSEEK_BASE_URL
GEMINI_BASE_URL = settings.GEMINI_BASE_URL

# Default Backend URLs（OLLAMA/LITELLM/OPENAI 的 *_DEFAULT_URL 為死常數已移除——
# 無任何匯入端；實際路由用 *_BASE_URL。僅 GROK 仍被 dispatchers/utils.py 使用。）
GROK_DEFAULT_URL = "https://api.x.ai/v1/chat/completions"
# Claude API 目的地（ADR-0247）：刻意用固定常數、不用 Settings 欄位（欄位會被同名環境變數覆寫）。
# anthropic SDK 沒拿到 base_url 時改連 ANTHROPIC_BASE_URL、金鑰一起送；每個建構點都顯式傳本值。
ANTHROPIC_API_BASE_URL = "https://api.anthropic.com"
# 官方 OpenAI Responses 的目的地（ADR-0248 D4／附註 37）：比照上一行刻意用固定常數；
# OPENAI_BASE_URL 是 Settings 欄位，會被同名環境變數覆寫，Responses 路由不經它。
OPENAI_RESPONSES_BASE_URL = "https://api.openai.com/v1"
# anthropic SDK 1.11 會讀、且能改寫請求目的地／憑證／標頭／日誌的環境變數（ADR-0247）：
# Touka 實際建的一般 client（AsyncAnthropic）會讀的全部，另加各平台 base URL（縱深防禦）。
# 沒清的：一般 client 讀的 WEBHOOK_SIGNING_KEY 只用來驗 webhook 簽章、不影響送出的請求；
# FOUNDRY_*、GOOGLE_CLOUD_*、VERTEX_PROJECT_ID、AWS_API_KEY 只有平台 client 讀，WORK_*、
# SESSION_ID 只有 environment worker 讀，Touka 不建這些（禁令 22 守衛另要求 SDK 建構子
# 明寫 base_url）。ANTHROPIC_API_KEY 是 Touka 的金鑰，保留。
ANTHROPIC_SDK_ENV_OVERRIDES: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CUSTOM_HEADERS",  # 建構時讀，能蓋掉 x-api-key
    "ANTHROPIC_LOG",  # import anthropic 時讀，debug 會把完整請求寫進日誌
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR",
    # 身分聯合（workload identity federation）
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_WORKSPACE_ID",
    "ANTHROPIC_SCOPE",
    # 各平台 client 的目的地
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_GOOGLE_CLOUD_BASE_URL",
    "ANTHROPIC_AWS_BASE_URL",
)
# openai SDK 3.24 建構時會讀、且能改寫送出的標頭／憑證／日誌的環境變數（ADR-0248 D4）。
# OPENAI_API_KEY／OPENAI_BASE_URL 是 Touka 自己的設定名，保留；建構點一律顯式傳這兩個值，SDK 不讀。
OPENAI_SDK_ENV_OVERRIDES: tuple[str, ...] = (
    "OPENAI_ORG_ID",  # → OpenAI-Organization 標頭
    "OPENAI_PROJECT_ID",  # → OpenAI-Project 標頭
    "OPENAI_ADMIN_KEY",
    "OPENAI_CUSTOM_HEADERS",  # 任意標頭（含 Authorization）
    "OPENAI_WEBHOOK_SECRET",
    "OPENAI_LOG",  # import openai 時讀，debug 會把請求寫進日誌
)
# 每個名字只記一次 info（ToukaMemory 會反覆把 .env 灌回環境）
_announced_sdk_env_overrides: set[str] = set()
# SDK 依 ANTHROPIC_LOG 調整層級的 logger（anthropic._utils._logs）；清除時還原。
_ANTHROPIC_SDK_LOGGERS = ("anthropic",)
# D4：openai 與 httpx2 的 logger 一律釘在 WARNING（debug 會記下請求內容與完整 URL）。
SDK_WARNING_LOGGERS = ("openai", "httpx2")


def pin_sdk_loggers() -> None:
    """把 SDK_WARNING_LOGGERS 釘在 WARNING（logging 設定與兩個清除函式都會呼叫）。"""
    for name in SDK_WARNING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def strip_openai_sdk_env_overrides(
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """從環境移除 ``OPENAI_SDK_ENV_OVERRIDES``，回傳實際移除的名稱（ADR-0248 D4）。

    config 載入時與每個 openai SDK 建構點各呼叫一次（同 Anthropic 的理由）；順便重釘日誌層級。
    """
    target = os.environ if environ is None else environ
    removed = tuple(name for name in OPENAI_SDK_ENV_OVERRIDES if name in target)
    for name in removed:
        target.pop(name)
    pin_sdk_loggers()
    if fresh := [name for name in removed if name not in _announced_sdk_env_overrides]:
        _announced_sdk_env_overrides.update(fresh)
        logger.info("Ignored openai SDK environment overrides: %s", ", ".join(fresh))
    return removed


def strip_anthropic_sdk_env_overrides(
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """從環境移除 ``ANTHROPIC_SDK_ENV_OVERRIDES``，回傳實際移除的名稱（ADR-0247）。

    config 載入時呼叫一次；SDK 建構點（主迴圈、factory 換金鑰、complete_once）送出前再呼叫，
    因為 ToukaMemory 匯入時會以 load_dotenv 把 .env 再灌回 os.environ。用 pop、不設空字串：
    SDK 會把空字串的 ANTHROPIC_BASE_URL 當成 base_url。ANTHROPIC_LOG 只在 import anthropic
    時讀；SDK 若已先載入並據此調高日誌層級，一併還原成 NOTSET。
    """
    target = os.environ if environ is None else environ
    removed = tuple(name for name in ANTHROPIC_SDK_ENV_OVERRIDES if name in target)
    for name in removed:
        target.pop(name)
    if "ANTHROPIC_LOG" in removed:
        for name in _ANTHROPIC_SDK_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
        pin_sdk_loggers()  # httpx2 不還原成 NOTSET，維持 WARNING（ADR-0248 D4）
    if fresh := [name for name in removed if name not in _announced_sdk_env_overrides]:
        _announced_sdk_env_overrides.update(fresh)
        logger.info("Ignored anthropic SDK environment overrides: %s", ", ".join(fresh))
    return removed

# Settings & Flags
GEMINI_API_VERSION = settings.GEMINI_API_VERSION
ENABLE_DYNAMIC_MODELS = settings.ENABLE_DYNAMIC_MODELS
TOUKA_MCP_OAUTH_USER_ENABLED = settings.TOUKA_MCP_OAUTH_USER_ENABLED
TOUKA_MCP_OAUTH_WORKLOAD_ENABLED = settings.TOUKA_MCP_OAUTH_WORKLOAD_ENABLED
TOUKA_MCP_OAUTH_DELEGATION_ENABLED = settings.TOUKA_MCP_OAUTH_DELEGATION_ENABLED
TOUKA_MCP_OAUTH_WORKLOAD_FILESYSTEM_KIND = settings.TOUKA_MCP_OAUTH_WORKLOAD_FILESYSTEM_KIND
TOUKA_MCP_OAUTH_KEY_SOURCE = settings.TOUKA_MCP_OAUTH_KEY_SOURCE
TOUKA_MCP_OAUTH_KEY = settings.TOUKA_MCP_OAUTH_KEY
TOUKA_MCP_OAUTH_RETIRED_KEYS = settings.TOUKA_MCP_OAUTH_RETIRED_KEYS
TOUKA_MCP_OAUTH_ROTATION_PHASE = settings.TOUKA_MCP_OAUTH_ROTATION_PHASE
TOUKA_MCP_OAUTH_KEY_EPOCH = settings.TOUKA_MCP_OAUTH_KEY_EPOCH
TOUKA_MCP_OAUTH_CALLBACK_BASE_URL = settings.TOUKA_MCP_OAUTH_CALLBACK_BASE_URL
TOUKA_MCP_OAUTH_CALLBACK_RESPONSE_MODE = settings.TOUKA_MCP_OAUTH_CALLBACK_RESPONSE_MODE
TOUKA_MCP_OAUTH_QUERY_CALLBACK_CANARY_VERIFIED = (
    settings.TOUKA_MCP_OAUTH_QUERY_CALLBACK_CANARY_VERIFIED
)
TOUKA_MCP_OAUTH_WORKLOAD_KEY_SOURCE = settings.TOUKA_MCP_OAUTH_WORKLOAD_KEY_SOURCE
TOUKA_MCP_OAUTH_WORKLOAD_KEY = settings.TOUKA_MCP_OAUTH_WORKLOAD_KEY
TOUKA_MCP_OAUTH_WORKLOAD_RETIRED_KEYS = settings.TOUKA_MCP_OAUTH_WORKLOAD_RETIRED_KEYS
TOUKA_MCP_OAUTH_WORKLOAD_ROTATION_PHASE = settings.TOUKA_MCP_OAUTH_WORKLOAD_ROTATION_PHASE
TOUKA_MCP_OAUTH_WORKLOAD_KEY_EPOCH = settings.TOUKA_MCP_OAUTH_WORKLOAD_KEY_EPOCH
OAUTH_STATE_TTL_SECONDS = settings.OAUTH_STATE_TTL_SECONDS
OAUTH_ACCESS_REFRESH_LEEWAY_SECONDS = settings.OAUTH_ACCESS_REFRESH_LEEWAY_SECONDS
OAUTH_REFRESH_LEASE_SECONDS = settings.OAUTH_REFRESH_LEASE_SECONDS
OAUTH_DCR_LEASE_SECONDS = settings.OAUTH_DCR_LEASE_SECONDS
OAUTH_POPUP_POLL_INTERVAL_MS = settings.OAUTH_POPUP_POLL_INTERVAL_MS
OAUTH_POPUP_TIMEOUT_MS = settings.OAUTH_POPUP_TIMEOUT_MS
OAUTH_CLEANUP_INTERVAL_SECONDS = settings.OAUTH_CLEANUP_INTERVAL_SECONDS
OAUTH_WORKER_LIVENESS_SECONDS = settings.OAUTH_WORKER_LIVENESS_SECONDS
OAUTH_KEY_ROTATION_BATCH_SIZE = settings.OAUTH_KEY_ROTATION_BATCH_SIZE
OAUTH_MAX_CLOCK_ROLLBACK_SECONDS = settings.OAUTH_MAX_CLOCK_ROLLBACK_SECONDS
OAUTH_RETRY_MAX_ATTEMPTS = settings.OAUTH_RETRY_MAX_ATTEMPTS
OAUTH_RETRY_BASE_MS = settings.OAUTH_RETRY_BASE_MS
OAUTH_RETRY_MAX_MS = settings.OAUTH_RETRY_MAX_MS
OAUTH_DISCONNECT_REVOCATION_TIMEOUT_SECONDS = settings.OAUTH_DISCONNECT_REVOCATION_TIMEOUT_SECONDS
OAUTH_CALLBACK_MAX_BODY_BYTES = settings.OAUTH_CALLBACK_MAX_BODY_BYTES
OAUTH_CALLBACK_MAX_FIELD_BYTES = settings.OAUTH_CALLBACK_MAX_FIELD_BYTES
OAUTH_CALLBACK_MAX_FIELDS = settings.OAUTH_CALLBACK_MAX_FIELDS
OAUTH_HTTP_CONNECT_TIMEOUT_SECONDS = settings.OAUTH_HTTP_CONNECT_TIMEOUT_SECONDS
OAUTH_HTTP_READ_TIMEOUT_SECONDS = settings.OAUTH_HTTP_READ_TIMEOUT_SECONDS
OAUTH_HTTP_WRITE_TIMEOUT_SECONDS = settings.OAUTH_HTTP_WRITE_TIMEOUT_SECONDS
OAUTH_HTTP_POOL_TIMEOUT_SECONDS = settings.OAUTH_HTTP_POOL_TIMEOUT_SECONDS
OAUTH_HTTP_MAX_RESPONSE_BYTES = settings.OAUTH_HTTP_MAX_RESPONSE_BYTES
OAUTH_HTTP_MAX_JSON_DEPTH = settings.OAUTH_HTTP_MAX_JSON_DEPTH
OAUTH_HTTP_MAX_JSON_ITEMS = settings.OAUTH_HTTP_MAX_JSON_ITEMS
TOUKA_AUTH_SECRET = settings.TOUKA_AUTH_SECRET
SECURE_COOKIES = settings.SECURE_COOKIES
AUTH_RATE_LIMIT_MAX = settings.AUTH_RATE_LIMIT_MAX
AUTH_RATE_LIMIT_WINDOW_SEC = settings.AUTH_RATE_LIMIT_WINDOW_SEC
AUTH_REFRESH_GRACE_PERIOD_SEC = settings.AUTH_REFRESH_GRACE_PERIOD_SEC
MAX_REQUEST_BODY_BYTES = settings.MAX_REQUEST_BODY_BYTES
AVATAR_MAX_BYTES = settings.AVATAR_MAX_BYTES
UPLOAD_MAX_BYTES = settings.UPLOAD_MAX_BYTES
UPLOAD_AGENT_QUOTA_BYTES = settings.UPLOAD_AGENT_QUOTA_BYTES
CLOUD_MODE = settings.CLOUD_MODE
ALLOWED_ORIGINS = settings.ALLOWED_ORIGINS
MEMORY_APP_DATA_DIR = settings.MEMORY_APP_DATA_DIR
UI_DEFAULT_URL = settings.UI_DEFAULT_URL
HOST_WORKSPACE_ROOT = settings.HOST_WORKSPACE_ROOT
DEFAULT_WORKSPACE_ROOT = HOST_WORKSPACE_ROOT
HEARTBEAT_EXTERNAL_URL = settings.HEARTBEAT_EXTERNAL_URL
HEARTBEAT_TOKEN = settings.HEARTBEAT_TOKEN
PLUGIN_DIR = settings.PLUGIN_DIR
TOOL_BASE_URL = settings.TOOL_BASE_URL


def apply_runtime_env(updates: dict[str, str]) -> None:
    """將已寫入 .env 的設定即時套用到執行中進程（免重啟）。

    同步更新三處，使呼叫端（如 `registry.get_key` 讀本模組常數）立即讀到新值：
      1. `os.environ`（供 `get_full_env` / 環境稽核）
      2. `settings` 物件屬性（若存在）
      3. 本模組的同名匯出常數（若存在）

    主要用於 provider 金鑰於 `POST /config/env` 寫檔後即時生效，免重啟容器。值為空字串
    時即代表清除該設定（呼叫端以 falsy 判定未設定）。
    """
    module_globals = globals()
    for key, value in updates.items():
        os.environ[key] = value
        # Settings 未啟用 validate_assignment，指派不觸發驗證；provider 金鑰皆為 str|None。
        if hasattr(settings, key):
            setattr(settings, key, value)
        if key in module_globals:
            module_globals[key] = value


def persist_generated_secret(key: str, value: str) -> bool:
    """把首次隨機生成的持久密鑰寫入 .env 並即時套用（禁令 15）.

    比照 ToukaHeartbeat ``StorageService`` 對 HEARTBEAT_TOKEN 的「生成即持久化」，讓
    TOUKA_AUTH_SECRET / DISPATCH_TOKEN 首次啟動自動生成後跨重啟穩定——免精靈、免手動。
    僅在 bootstrap 啟用時寫（測試/沙盒回 False、no-op）；寫入經 ``resolve_env_file_path()``
    SSOT（禁令 38），成功後 ``apply_runtime_env`` 即時同步進程。寫檔失敗只記 log、回
    False（絕不擋認證/派工）。

    Returns:
        bool: 是否成功持久化（False＝bootstrap 關閉或寫檔失敗）。
    """
    if not is_bootstrap_enabled():
        return False
    try:
        set_key(str(resolve_env_file_path()), key, value, quote_mode="never")
    except OSError:
        logger.exception("Failed to persist generated secret %s to .env", key)
        return False
    apply_runtime_env({key: value})
    logger.info("🔑 %s 未設定——已自動生成並持久化至 .env（跨重啟穩定）。", key)
    return True


def persist_generated_secrets(updates: dict[str, str]) -> bool:
    """Atomically persist a related secret set in one authoritative .env replace."""
    if not is_bootstrap_enabled() or not updates:
        return False
    if any(
        not key or "=" in key or "\n" in key or "\r" in key or "\n" in value or "\r" in value
        for key, value in updates.items()
    ):
        return False
    env_path = resolve_env_file_path()
    temporary_path: Path | None = None
    try:
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        output: list[str] = []
        written: set[str] = set()
        for line in existing.splitlines():
            stripped = line.lstrip()
            candidate = stripped.removeprefix("export ")
            key = candidate.split("=", 1)[0].strip() if "=" in candidate else None
            if key not in updates:
                output.append(line)
                continue
            if key not in written:
                output.append(f"{key}={updates[key]}")
                written.add(key)
        output.extend(f"{key}={value}" for key, value in updates.items() if key not in written)
        content = "\n".join(output) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=env_path.parent,
            prefix=f".{env_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        if env_path.exists():
            temporary_path.chmod(env_path.stat().st_mode)
        temporary_path.replace(env_path)
        temporary_path = None
    except OSError:
        logger.exception("Failed to atomically persist generated secret set")
        return False
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    apply_runtime_env(updates)
    logger.info("🔑 Atomically persisted secret set: %s", ", ".join(sorted(updates)))
    return True


def read_persisted_secret_values(keys: tuple[str, ...]) -> dict[str, str]:
    """Read selected values from the authoritative .env without logging secrets."""
    values = dotenv_values(resolve_env_file_path())
    return {
        key: value if isinstance(value, str) else ""
        for key in keys
        if (value := values.get(key)) is not None
    }


# 三把自動持久化密鑰歷來公開過的值（出貨預設、範本佔位、文件範例；禁令 15）。比對一定看
# 「鍵名＋值」：別的鍵（如 SEARXNG_SECRET）用到同一個佔位字串不算。三個解析點與設定精靈共用。
PUBLIC_SECRET_VALUES: dict[str, frozenset[str]] = {
    "HEARTBEAT_TOKEN": frozenset(
        {"touka_heartbeat_secret_2026", "<your-generated-heartbeat-token>", "your_secure_token"}
    ),
    "TOUKA_AUTH_SECRET": frozenset(
        {
            "TOUKA_INSECURE_DEFAULT_SECRET_FOR_DEV",
            "touka_default_secret_2026",
            "<your-random-secret>",
        }
    ),
    "DISPATCH_TOKEN": frozenset(
        {"<your-generated-dispatch-token>", "touka_dispatch_secret_2026", "your_secret"}
    ),
}


def is_public_secret(key: str, value: str) -> bool:
    """去掉前後空白後，``value`` 是否為 ``key`` 歷來公開過的值。"""
    return value.strip() in PUBLIC_SECRET_VALUES.get(key, frozenset())


def usable_secret(key: str, current: str) -> str:
    """公開值一律不採用；回傳可用的值，空字串＝交給呼叫端「產生並寫回」.

    ``.env`` 已存非公開值時沿用它並同步到執行中的程序、不寫檔：容器只重啟、沒重建時程序環境
    仍是公開值，不能每次重啟都換一個值。log 只寫鍵名、不寫值。
    """
    if not is_public_secret(key, current):
        return current
    try:
        stored = read_persisted_secret_values((key,)).get(key, "").strip()
    except (OSError, UnicodeDecodeError):
        stored = ""
    if stored and not is_public_secret(key, stored):
        apply_runtime_env({key: stored})
        logger.warning("%s 是公開過的值，改用 .env 已存的值。", key)
        return stored
    logger.warning("%s 是公開過的值，不採用。", key)
    return ""


# ADR-0151：偵測「寫入的 .env」與「compose 實際載入的 .env」分歧（幽靈檔）用的探針鍵。
# 皆為「只會出現在 .env（env_file）、不會由 proxy 的 compose `environment:` 注入」的設定鍵
# ——故若目前生效（os.environ 非空）卻一個都不在持久化 .env 內，代表持久化目標是另一個檔。
_ENV_PERSISTENCE_PROBE_KEYS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GROK_API_KEY",
    "XAI_API_KEY",
    "LITELLM_API_KEY",
    "TAVILY_API_KEY",
    "WORKSPACE_ROOT",
    "SANCTUARY_DATA_PATH",
    "EMBEDDING_MODEL",
)


# 自動持久化的三把密鑰（禁令 15）。守衛以「值相符」豁免：這些鍵若同時（a）在持久化 .env 內
# （b）os.environ 有同值——證明該檔確實被 compose env_file 載入過 → 絕非幽靈檔。
_AUTO_PERSISTED_TOKEN_KEYS: tuple[str, ...] = (
    "TOUKA_AUTH_SECRET",
    "DISPATCH_TOKEN",
    "HEARTBEAT_TOKEN",
)


def _read_env_keys(path: Path) -> set[str]:
    """讀取 .env 的鍵名集合（不解析值、不載入；缺檔/讀不到回空集）。"""
    keys: set[str] = set()
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return keys
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        key = key.removeprefix("export ").strip()
        if key:
            keys.add(key)
    return keys


def verify_env_persistence_integrity(
    env_path: Path | None = None,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """啟動 fail-loud 守衛（ADR-0151）：偵測密鑰持久化 .env 是否為『幽靈檔』.

    持久化目標（``resolve_env_file_path()`` / ``TOUKA_ENV_FILE``）理應就是 compose ``env_file``
    載入的那份 .env。若「目前生效的設定鍵」（探針：API 金鑰／WORKSPACE_ROOT 等，只來自 .env）
    在持久化檔內**一個都找不到**，代表寫入的是另一個檔（如 WORKSPACE_ROOT 誤設導致 /app/workspace
    指到磁碟根、寫進幽靈 .env）——自動生成的 token/密鑰不會被讀到、每次重啟重生成。此時 **loud
    log**（不中止啟動：認證仍可用臨時密鑰，中止反而更糟）並回傳缺失鍵清單。

    回空清單＝健康或不適用（無生效探針鍵、部分鍵已在檔內、或檔案缺失無法比對）。
    """
    live_environ = os.environ if environ is None else environ
    path = resolve_env_file_path() if env_path is None else Path(env_path)

    live = [k for k in _ENV_PERSISTENCE_PROBE_KEYS if str(live_environ.get(k, "")).strip()]
    if not live:
        return []  # 無生效探針鍵（全新未設）→ 無可分歧
    persisted = _read_env_keys(path)
    if not persisted:
        return []  # 持久化檔缺失/空 → 無法比對，不誤報（全新首跑）

    # 值相符豁免（對抗式審查 #2 修）：自動持久化的 token 若「檔內值 == 目前生效值（非空）」，
    # 證明持久化檔確實被載入過 → 絕非幽靈檔。堵真實誤報：operator 以 compose `environment:` /
    # `-e` 注入 API 金鑰（探針鍵不進 .env）、.env 只有自動生成的 token —— 健康部署卻被
    # 「探針鍵全缺」誤判。僅比對鍵值於進程內、不落日誌。
    try:
        persisted_values = dotenv_values(path)
    except (OSError, UnicodeDecodeError):
        persisted_values = {}
    for token_key in _AUTO_PERSISTED_TOKEN_KEYS:
        file_value = (persisted_values.get(token_key) or "").strip()
        if file_value and file_value == str(live_environ.get(token_key, "")).strip():
            return []  # 檔已被載入（token 往返一致）→ 非幽靈

    missing = [k for k in live if k not in persisted]
    # 分歧訊號：生效鍵「全數」不在持久化檔 → 幾可確定是幽靈檔（部分重疊不算，避免誤報）
    if missing and len(missing) == len(live):
        logger.error(
            "🛑 密鑰持久化 .env（%s）不含任何目前生效的設定鍵（例：%s）——很可能寫入的是與 "
            "compose 實際載入的 .env 不同的『幽靈檔』；自動生成的 token/密鑰不會被讀到（每次重啟"
            "重生成）。請確認 TOUKA_ENV_FILE 指向 compose 目錄的 .env（ADR-0151 解耦後為 "
            "/app/host_root/.env，與 WORKSPACE_ROOT 無關）。",
            path,
            ", ".join(live[:3]),
        )
        return missing
    return []


def load_env_file_into_environ() -> None:
    """把已解析的 .env 灌進 ``os.environ``（native 模式與 docker ``env_file`` 平價）.

    docker 由 compose ``env_file`` 把 .env 注入成進程環境變數 → ``os.environ`` 齊全；
    native 直接跑 ``uvicorn src.main:app`` 時無人載入，導致「走 ``os.environ``」的讀取
    （``EMBEDDING_MODEL`` 等非 Settings 欄位、setup 精靈狀態）永遠讀不到 .env、退回預設。
    此處補上；``override=False`` 保持「真實環境變數 > .env 檔」的既有優先序（與 pydantic
    一致）。gated by :func:`is_bootstrap_enabled`——測試/沙盒為 False → no-op，杜絕真實
    金鑰污染 ``os.environ``（比照 conftest 的沙盒隔離）。
    """
    if not is_bootstrap_enabled():
        return
    load_dotenv(resolve_env_file_path(), override=False)


def _resolve_heartbeat_token(current: str) -> str:
    """HEARTBEAT_TOKEN 未設定時自動生成並持久化（禁令 15）；回傳最終要匯出的 token.

    比照 DISPATCH_TOKEN / TOUKA_AUTH_SECRET 的「生成即持久化」，讓首次啟動即跨重啟穩定、
    免精靈、免手動貼佔位字串。持久化失敗（bootstrap 關或寫檔失敗）→ 維持空值，交由既有
    「未設定」錯誤路徑處理——**不硬塞臨時值**，因 proxy 伺服端與 heartbeat 客戶端兩條
    解析路徑只共享 .env，臨時值會造成兩端不對稱。必須於模組載入尾聲呼叫：consumer 以
    ``from src.config import HEARTBEAT_TOKEN`` 綁定常數，晚於此點生成則綁到舊空值。
    歷來公開過的值當成沒設定（:func:`usable_secret`）。
    """
    current = usable_secret("HEARTBEAT_TOKEN", current.strip())
    if current or not is_bootstrap_enabled():
        return current
    generated = secrets.token_urlsafe(32)
    if persist_generated_secret("HEARTBEAT_TOKEN", generated):
        return generated
    return current


# --- 模組載入尾聲：native .env→os.environ 平價（bug②）＋ HEARTBEAT_TOKEN 自癒（問題①）。
#     兩者皆 gated by is_bootstrap_enabled（測試/沙盒 no-op、不污染真實 .env）。
#     HEARTBEAT_TOKEN 須在此重新綁定——晚於消費端 import 則綁到舊空值。---
load_env_file_into_environ()
strip_anthropic_sdk_env_overrides()  # .env 灌進環境之後才清（ADR-0247）
strip_openai_sdk_env_overrides()  # 同上（ADR-0248 D4）
HEARTBEAT_TOKEN = _resolve_heartbeat_token(HEARTBEAT_TOKEN)
