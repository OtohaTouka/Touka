"""MCP 伺服器註冊表 (Protocol Registry).

負責從物理設定檔 (server_config.json) 載入並管理可用 MCP 伺服器的啟動命令與環境配置。
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, Field, field_validator, model_validator

from src.protocols.mcp.oauth.workload.target import (
    McpOAuthTargetBindingError,
    canonical_mcp_transport_url,
)
from src.utils import mcp_tool_policy
from src.utils.mcp_remote import validate_remote_url

logger = logging.getLogger(__name__)


class ProtocolRegistryReloadError(RuntimeError):
    """A process-frozen workload OAuth installation changed on disk。"""

    def __init__(self) -> None:
        super().__init__("Workload OAuth MCP configuration is frozen for this process.")


class ProtocolRegistrySourceGraphError(ValueError):
    """A delegated workload target has no exact interactive OAuth source。"""


def _raise_frozen_workload_config_error() -> NoReturn:
    raise ProtocolRegistryReloadError()


class ProtocolServerConfig(BaseModel):
    """單個 MCP 伺服器的執行配置（stdio 或 remote，兩者互斥）。

    **stdio**（本地子進程）：``command`` ＋ ``args`` ＋ ``env``。
    **remote**（streamable HTTP，ADR-0192）：``url`` ＋ ``headers``；header 內以
    ``${VAR}`` 引用 ``env`` 的憑證，讓 remote 條目沿用既有的 env 遮罩保護。

    ``vault_jail_arg``（選配）：若設，代表此伺服器的某個工具參數是「輸入根目錄」，派發前由
    ``mcp_vault_jail`` 依當下身分覆寫成該 agent 自己的 vault 根（per-agent 隔離；如 mcp-docgen
    的 ``base_dir``）。未設＝不受 vault 隔離（一般伺服器）。

    ``enabled``（預設 True）：使用者停用開關（前端設定頁 toggle）。``False`` 者於
    ``load_config`` 即被排除、不進 runtime registry（ADR-0188 咽喉執法；remote 條目
    一視同仁——沒有子進程可殺，排除即代表不建立連線）。

    ``enabled_tools``／``disabled_tools``（ADR-0195，軸 A per-installation）：把粒度從
    「整台」降到「單一工具」。語意是 owner 對自己 agent 的自我約束，**所有身分一律適用、
    Owner 也不例外**——故執法點在供給面（``ProtocolManager.get_all_tools`` 廣告面 ＋
    ``call_tool_on_server`` 執行面），不走 ``permission_service``（後者對 Owner 短路）。
    ``enabled_tools=None`` ＝未設＝全開；``[]`` ＝明示全關。比對規則見 ``mcp_tool_policy``。
    """

    command: str | None = None
    args: list[str] = []
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    vault_jail_arg: str | None = None
    enabled: bool = True
    enabled_tools: list[str] | None = None
    disabled_tools: list[str] = []
    # ADR-0221：所有 peer 必須完成 modern discover，不再提供舊協定重連。
    protocol_mode: Literal["2026-07-28"] = "2026-07-28"
    auth_mode: Literal[
        "none",
        "static_headers",
        "oauth_user",
        "oauth_client_credentials",
        "enterprise",
    ] = "none"
    auth_profile_id: str | None = None
    workload_authority_kind: Literal["direct", "owner_delegation"] | None = None
    workload_delegation_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$",
    )
    workload_source_installation_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$",
    )
    capability_policy: dict[str, Any] = Field(default_factory=dict)
    # P6：SDK response cache 是唯一 cache owner；可逐 installation 關閉以回退每次讀取。
    cache_enabled: bool = True
    cache_max_entries: int = Field(default=1024, ge=0, le=8192)
    cache_default_ttl_ms: int = Field(default=0, ge=0, le=86_400_000)
    # P6：modern change stream supervisor；關閉只停 freshness 通知，不影響主連線。
    subscriptions_enabled: bool = True
    subscription_max_backoff_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    # P6：敏感 x-mcp-header 預設隔離；owner 必須逐裸工具名核准。
    approved_header_tools: list[str] = []
    icons_enabled: bool = True
    # 預設 off；啟用時仍只允許 payload-free attributes，不接受網路 exporter endpoint。
    telemetry_enabled: bool = False

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_static_header_auth(cls, data: Any) -> Any:
        """舊 remote 條目沒有 auth_mode；帶 headers 者保守推導為 installation static auth。"""
        if isinstance(data, dict) and "auth_mode" not in data:
            normalized = dict(data)
            normalized["auth_mode"] = "static_headers" if normalized.get("headers") else "none"
            return normalized
        return data

    @field_validator("url")
    @classmethod
    def url_must_be_safe(cls, v: str | None) -> str | None:
        """Remote 端點安全閘（明文 http 僅限 loopback）——壞條目不得活到連線階段。"""
        if v is not None:
            validate_remote_url(v)
        return v

    @model_validator(mode="after")
    def exactly_one_transport(self) -> "ProtocolServerConfig":
        """``command`` 與 ``url`` 恰須其一：形狀不明的條目整筆拒絕，不靜默挑一個。"""
        if bool(self.command) == bool(self.url):
            msg = (
                "MCP server 設定必須且只能擇一指定 `command`（本地 stdio）"
                "或 `url`（remote streamable HTTP）。"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_workload_oauth_shape(self) -> "ProtocolServerConfig":
        """P8-B workload OAuth 僅允許明確綁定 profile 的 remote installation。"""
        if self.auth_mode != "oauth_client_credentials":
            if (
                self.workload_authority_kind is not None
                or self.workload_delegation_id is not None
                or self.workload_source_installation_id is not None
            ):
                msg = "workload authority fields require oauth_client_credentials"
                raise ValueError(msg)
            return self
        if not self.is_remote:
            msg = "oauth_client_credentials 僅支援 remote MCP transport"
            raise ValueError(msg)
        if not self.auth_profile_id:
            msg = "oauth_client_credentials 必須指定 auth_profile_id"
            raise ValueError(msg)
        if self.workload_authority_kind is None:
            msg = "oauth_client_credentials 必須指定 workload_authority_kind"
            raise ValueError(msg)
        if self.workload_authority_kind == "owner_delegation" and (
            self.workload_delegation_id is None or self.workload_source_installation_id is None
        ):
            msg = (
                "owner_delegation 必須指定 workload_delegation_id 與 "
                "workload_source_installation_id"
            )
            raise ValueError(msg)
        if self.workload_authority_kind == "direct" and (
            self.workload_delegation_id is not None
            or self.workload_source_installation_id is not None
        ):
            msg = (
                "direct workload 不得指定 workload_delegation_id 或 workload_source_installation_id"
            )
            raise ValueError(msg)
        return self

    @property
    def is_remote(self) -> bool:
        """此條目是否為 remote（streamable HTTP）傳輸。"""
        return bool(self.url)

    @property
    def is_principal_scoped(self) -> bool:
        """是否必須依 human/delegation principal 建立獨立、request-time handle。"""
        return self.auth_mode in {
            "oauth_user",
            "oauth_client_credentials",
            "enterprise",
        }

    @property
    def requires_deferred_auth(self) -> bool:
        """是否不得在 lifespan 階段建立 unauthenticated transport。"""
        return self.auth_mode not in {"none", "static_headers"}

    def allows_tool(self, tool_name: str) -> bool:
        """軸 A（ADR-0195）：此次安裝是否開放該裸工具名。委派 ``mcp_tool_policy`` SSOT。"""
        return mcp_tool_policy.is_tool_enabled(tool_name, self.enabled_tools, self.disabled_tools)


class ProtocolRegistry:
    """負責讀取並管理 Server 設定"""

    def __init__(self, config_path: str) -> None:
        """初始化註冊表.

        Args:
            config_path (str): 伺服器設定檔 (如 server_config.json) 的實體路徑。
        """
        self.config_path = config_path
        self.servers: dict[str, ProtocolServerConfig] = {}
        self._workload_oauth_snapshot = self._snapshot_workload_oauth({})
        self._frozen_workload_oauth_snapshot: str | None = None
        self.load_config()

    @staticmethod
    def _snapshot_workload_oauth(
        servers: dict[str, ProtocolServerConfig],
    ) -> str:
        workload = {
            name: config.model_dump(mode="json")
            for name, config in sorted(servers.items())
            if config.auth_mode == "oauth_client_credentials"
        }
        canonical = json.dumps(
            workload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    def freeze_workload_oauth_config(self) -> None:
        """Forbid process-lifetime reload of every parsed P8-B installation。"""
        self._frozen_workload_oauth_snapshot = self._workload_oauth_snapshot

    @staticmethod
    def _validate_workload_source_graph(
        servers: dict[str, ProtocolServerConfig],
    ) -> None:
        """Require every delegated target to name one exact P8-A installation。"""
        for target_name, target in servers.items():
            if not (
                target.auth_mode == "oauth_client_credentials"
                and target.workload_authority_kind == "owner_delegation"
            ):
                continue
            source_name = target.workload_source_installation_id
            source = servers.get(source_name or "")
            if (
                source_name is None
                or source_name == target_name
                or source is None
                or not source.enabled
                or not source.is_remote
                or source.auth_mode != "oauth_user"
                or source.auth_profile_id != target.auth_profile_id
            ):
                raise ProtocolRegistrySourceGraphError()
            try:
                source_url = canonical_mcp_transport_url(source.url or "")
                target_url = canonical_mcp_transport_url(target.url or "")
            except McpOAuthTargetBindingError as exc:
                raise ProtocolRegistrySourceGraphError() from exc
            if source_url != target_url:
                raise ProtocolRegistrySourceGraphError()

    def load_config(self) -> None:
        """從物理設定檔載入並解析伺服器配置。"""
        try:
            parsed_servers: dict[str, ProtocolServerConfig] = {}
            parsed_all: dict[str, ProtocolServerConfig] = {}
            if not Path(self.config_path).exists():
                logger.warning(f"設定檔 {self.config_path} 不存在，跳過載入。")
                data: dict[str, Any] = {}
            else:
                with Path(self.config_path).open(encoding="utf-8") as f:
                    data = json.load(f)

            servers_data = data.get("mcpServers", {})  # 保留 key 值以便相容
            for name, cfg in servers_data.items():
                parsed = ProtocolServerConfig(**cfg)
                parsed_all[name] = parsed
                # enabled:false ＝ 停用閘唯一執法點（ADR-0188）：不進 runtime registry
                # → initialize_all 不 spawn、ensure_connected／resolve_tool 一律 fail-closed。
                if not parsed.enabled:
                    logger.info(f"MCP server '{name}' 已停用（enabled=false），跳過載入。")
                    continue
                parsed_servers[name] = parsed

            self._validate_workload_source_graph(parsed_all)

            workload_snapshot = self._snapshot_workload_oauth(parsed_all)
            if (
                self._frozen_workload_oauth_snapshot is not None
                and workload_snapshot != self._frozen_workload_oauth_snapshot
            ):
                _raise_frozen_workload_config_error()
            self.servers = parsed_servers
            self._workload_oauth_snapshot = workload_snapshot

            logger.info(f"成功載入 {len(self.servers)} 個 Server 設定")
        except ProtocolRegistryReloadError:
            raise
        except Exception:
            self.servers = {}
            self._workload_oauth_snapshot = self._snapshot_workload_oauth({})
            logger.exception("讀取設定檔 %s 失敗", self.config_path)

    def get_server(self, name: str) -> ProtocolServerConfig | None:
        """獲取指定名稱的伺服器配置.

        Args:
            name (str): 伺服器識別名稱。

        Returns:
            ProtocolServerConfig | None: 伺服器配置實例，若不存在則回傳 None。
        """
        return self.servers.get(name)

    def get_all_servers(self) -> dict[str, ProtocolServerConfig]:
        """獲取所有註冊的伺服器配置.

        Returns:
            dict[str, ProtocolServerConfig]: 所有伺服器名稱與配置的映射字典。
        """
        return self.servers

    def find_server_by_tool_name(self, tool_name: str) -> str | None:
        """嘗試根據工具名稱尋找對應的伺服器 ID。

        優先級：
        1. 完全匹配伺服器 ID
        2. 匹配雙底線前綴 (如 grok_imagine__draw -> grok_imagine)
        """
        # 1. 完全匹配
        if tool_name in self.servers:
            return tool_name

        # 2. 匹配雙底線前綴
        if "__" in tool_name:
            server_part = tool_name.split("__")[0]
            if server_part in self.servers:
                return server_part

        return None
