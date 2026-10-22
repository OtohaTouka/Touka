"""MCPHubService — MCP Hub 同步與安裝的唯一業務邏輯來源 (SSOT)

職責 (SRP):
  - 從外部 API 同步工具清單至 mcp_hub.json
  - 從 Hub 安裝工具至 server_config.json

設計原則:
  - 依賴注入路徑 (DIP)，便於測試時替換為 tmp_path
  - 不依賴任何 FastAPI 物件，可獨立在測試環境中運行
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx

from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.services.mcp_isolation_store import (
    McpIsolationStore,
    assign,
    bound_of,
    origin_category,
)
from src.services.resource_manager import resource_manager
from src.utils import mcp_tool_policy
from src.utils.mcp_official_map import official_hub_entry
from src.utils.mcp_registry_entry import registry_item_to_hub_entry
from src.utils.mcp_security import is_command_safe
from src.utils.mcp_sentinels import OUTPUT_DIR_SENTINEL, resolve_env_sentinel

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.services.mcp_isolation_store import IsolationRecord

logger = logging.getLogger(__name__)

HUB_OVER_SELF_BUILT = "mcp_hub_install_over_self_built"
"""Hub 安裝遇到同名、紀錄是自建的條目時拒絕（422 的 ``detail_override`` 代碼）。"""

GITHUB_API_URL = "https://api.github.com/repos/modelcontextprotocol/servers/contents/src"
# 社群來源＝官方 MCP Registry（ADR-0187；取代已失效的 Smithery CLI 路線）。
REGISTRY_API_URL = "https://registry.modelcontextprotocol.io/v0/servers"
_REGISTRY_PAGE_LIMIT = 100
_REGISTRY_MAX_PAGES = 5

# curated 條目 env 哨兵解析集中於 utils.mcp_sentinels（SSOT，install 與 connect 共用）。


def _read_json_or_raise(path: str) -> dict | None:
    """讀設定 JSON：不存在或壞檔回 None（同 read_json）；存在卻讀不到時拋 PERSISTENCE_ERROR。

    讀不到時不可當成空的整份寫回：會刪掉其他條目，並清掉停用與工具政策（變寬）。
    """
    data = resource_manager.read_json(path)
    if data is None and Path(path).exists():
        try:
            resource_manager.read_file_text(path)
        except OSError as exc:
            logger.warning("AUDIT: %s exists but cannot be read; refusing to rewrite it", path)
            raise ToukaException(ErrorCode.PERSISTENCE_ERROR) from exc
    return data


def _claim_for_hub(
    name: str, bound: str, *, replacing: bool
) -> Callable[[dict[str, IsolationRecord]], None]:
    """Hub 的紀錄：同名而且紀錄是自建類（含 ``bound`` 對不上的自建）時拒絕，不蓋掉代理人的條目。

    先寫 ``hub`` 紀錄、再寫設定的中途，舊的代理人條目會對不上任何紀錄而掉成第三方（變寬）；
    唯一會這樣的寫入點就是這裡，所以直接拒絕，要人先在設定頁刪掉那台（LOW-a）。
    """
    record = assign(name, "hub", bound, fresh=not replacing)

    def apply(records: dict[str, IsolationRecord]) -> None:
        old = records.get(name)
        if replacing and old is not None and origin_category(old.origin) == "self":
            raise ToukaException(ErrorCode.VALIDATION_ERROR, detail_override=HUB_OVER_SELF_BUILT)
        record(records)

    return apply


def derive_server_key(name: str) -> str:
    """從 Hub 條目 name 衍生 server_config 的 key（安裝命名 SSOT）。

    用 ``name``（非 ``id``）轉換、去空格，避免 ``curated-`` 等 id 前綴污染 server key——
    技能 .mcp.json 宣告的 server 名必須與此一致才連得上（ADR-0102）。install／啟動遷移／
    技能接線健檢與 Guard #37 皆以此為準。
    """
    return name.lower().replace(" ", "-")


class MCPHubService:
    """MCP Hub 同步與安裝服務（mcp_hub.json / server_config.json 的 SSOT）。"""

    def __init__(
        self,
        hub_path: str | None = None,
        config_path: str | None = None,
    ) -> None:
        self.hub_path = hub_path or str(get_config_dir() / "mcp_hub.json")
        self.config_path = config_path or str(get_config_dir() / "server_config.json")
        # 隔離設定檔和 server_config.json 放同一個目錄（ADR-0275）
        self._isolation = McpIsolationStore(Path(self.config_path).parent)

    # ------------------------------------------------------------------
    # 外部數據抓取（可獨立測試 / mock）
    # ------------------------------------------------------------------

    async def fetch_official_servers(self) -> list[dict]:
        """從 Anthropic GitHub 抓取官方 MCP 伺服器清單"""
        logger.info("Fetching official servers from Anthropic GitHub...")
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            try:
                response = await client.get(GITHUB_API_URL)
                response.raise_for_status()
                items = response.json()
                servers: list[dict] = []
                for item in items:
                    if item.get("type") != "dir":
                        continue
                    # 條目配方 SSOT＝official_hub_entry（ADR-0186 啟動規格＋ADR-0189
                    # 出廠種子共用同一配方；官方目錄非一律 npm——fetch/git/time 為
                    # PyPI（uvx）、filesystem 需帶允許目錄哨兵）。
                    entry, known = official_hub_entry(item["name"])
                    if not known:
                        logger.warning(
                            "官方 MCP 目錄 %r 不在啟動規格表，fallback npx 慣例"
                            "（套件未實證，安裝後可能連不上）。",
                            item["name"],
                        )
                    servers.append(entry)
                return servers
            except Exception:
                logger.exception("Failed to fetch official servers")
                return []

    async def fetch_registry_servers(self) -> list[dict]:
        """從官方 MCP Registry 抓取社群伺服器清單（ADR-0187，取代 Smithery）。

        依 ``metadata.nextCursor`` 分頁、上限 ``_REGISTRY_MAX_PAGES`` 頁（達上限仍有
        cursor 即記 WARNING＝截斷有聲）；條目映射 SSOT＝``registry_item_to_hub_entry``
        （remote-only／非最新／不可無人值守啟動者略過並統計、不入貨架＝誠實語意）。
        """
        logger.info("Fetching community servers from the official MCP Registry...")
        servers: list[dict] = []
        skipped = 0
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            try:
                cursor: str | None = None
                for _ in range(_REGISTRY_MAX_PAGES):
                    params: dict[str, str] = {"limit": str(_REGISTRY_PAGE_LIMIT)}
                    if cursor:
                        params["cursor"] = cursor
                    response = await client.get(REGISTRY_API_URL, params=params)
                    response.raise_for_status()
                    data = response.json()
                    for item in data.get("servers") or []:
                        entry = registry_item_to_hub_entry(item)
                        if entry is not None:
                            servers.append(entry)
                        else:
                            skipped += 1
                    cursor = (data.get("metadata") or {}).get("nextCursor")
                    if not cursor:
                        break
                else:
                    if cursor:
                        logger.warning(
                            "官方 Registry 清單超出掃描上限（%d 頁），已截斷（仍有 nextCursor）。",
                            _REGISTRY_MAX_PAGES,
                        )
            except Exception:
                logger.exception("Failed to fetch MCP Registry servers")
                return []
        logger.info(
            "MCP Registry fetch: %d installable, %d skipped (remote-only/non-latest/no-value args).",
            len(servers), skipped,
        )
        return servers

    # ------------------------------------------------------------------
    # 核心業務邏輯
    # ------------------------------------------------------------------

    async def sync(self) -> dict:
        """同步外部清單至 mcp_hub.json。

        規則：本地策展 (curated/first-party) 優先保留 → 官方 → 社群（官方 MCP
        Registry），依名稱去重；若外部來源全部失敗，保留現有 JSON 不動（含 curated）。
        """
        official = await self.fetch_official_servers()
        registry = await self.fetch_registry_servers()

        # 網路失敗保護：外部兩邊都空 → 不覆寫，保留現有檔（含 curated）
        if not official and not registry:
            logger.warning(
                "Both official and MCP Registry sources returned empty results. "
                "Keeping existing hub data."
            )
            return {"status": "no_update", "synced": 0}

        # 保留本地策展 (first-party) 條目，使其不被外部清單覆寫
        existing = _read_json_or_raise(self.hub_path) or {}
        curated = [s for s in existing.get("hub", []) if s.get("source") == "curated"]

        # 策展優先合併 (以 name.lower() 為去重 key)；缺 name 的損毀條目跳過不中斷整批
        seen: set[str] = set()
        combined: list[dict] = []
        for s in (*curated, *official, *registry):
            name = s.get("name")
            if not name:
                logger.warning(
                    "Skipping hub entry without name: %s", s.get("id", "<unknown>")
                )
                continue
            key = name.lower()
            if key not in seen:
                combined.append(s)
                seen.add(key)

        resource_manager.make_dir(Path(self.hub_path).parent)
        resource_manager.write_json(self.hub_path, {"hub": combined})

        logger.info(f"Sync complete! {len(combined)} servers written.")
        return {"status": "success", "synced": len(combined)}

    async def install(self, hub_id: str, env_vars: dict[str, str]) -> dict:
        """從 mcp_hub.json 中取得指定工具配置，寫入 server_config.json，回傳操作結果。"""
        # 1. 讀取 Hub (SSOT)
        hub_data: list[dict] = []
        data = resource_manager.read_json(self.hub_path)
        if data:
            hub_data = data.get("hub", [])

        # 2. 尋找目標工具
        target = next((item for item in hub_data if item.get("id") == hub_id), None)
        if not target:
            return {"status": "not_found", "hub_id": hub_id}

        # 2.5 防禦：條目缺必要欄位（手動編輯/損毀）時拒絕安裝而非 KeyError 500。
        # remote 條目（ADR-0192）以 url 取代 command/args，故兩種形狀分別檢查。
        is_remote = bool(target.get("url"))
        shape_ok = (
            bool(target.get("url"))
            if is_remote
            else (bool(target.get("command")) and target.get("args") is not None)
        )
        if not target.get("name") or not shape_ok:
            logger.warning(
                "Hub entry %s is missing required fields (name/command/args or url).",
                hub_id,
            )
            return {"status": "invalid_entry", "hub_id": hub_id}

        # 3. 讀取現有伺服器配置
        config: dict = {"mcpServers": {}}
        data = _read_json_or_raise(self.config_path)
        if data:
            config = data

        if "mcpServers" not in config:
            config["mcpServers"] = {}

        # 4. 組裝並寫入配置
        # Key 由 name 衍生（去空格），避免 ID 前綴污染（SSOT：derive_server_key）。
        server_name = derive_server_key(target["name"])
        replacing = server_name in config["mcpServers"]

        env_config: dict[str, str] = {}
        for ev in target.get("env_requirements", []):
            env_config[ev] = env_vars.get(ev, f"FILL_ME_IN_{ev}")
        # curated 條目可帶預設 env（如 mcp-docgen 輸出夾），含哨兵解析
        for key, value in (target.get("env") or {}).items():
            env_config[key] = self._resolve_env_sentinel(value)

        if is_remote:
            # remote（ADR-0192）：url＋header 模板落盤；憑證實值留在 env，由 connect
            # 端以 ${VAR} 解析——與 stdio 條目共用同一套 env 遮罩保護。
            config["mcpServers"][server_name] = {
                "url": target["url"],
                "headers": dict(target.get("headers") or {}),
                "env": env_config,
            }
        else:
            config["mcpServers"][server_name] = {
                "command": target["command"],
                # args 哨兵與 env 同語意（ADR-0186）：install 時解析落盤絕對路徑；
                # connect 端亦會再解析（磁碟手寫哨兵同樣還原）。
                "args": [self._resolve_env_sentinel(a) for a in target["args"]],
                "env": env_config,
            }
        # vault-jail 政策（per-agent 隔離）隨 curated 條目傳遞，重裝不致靜默關閉隔離。
        if target.get("vault_jail_arg"):
            config["mcpServers"][server_name]["vault_jail_arg"] = target["vault_jail_arg"]

        # 先寫隔離設定檔（hub＋新條目的 bound）再寫設定（ADR-0275）
        claim = _claim_for_hub(
            server_name, bound_of(config["mcpServers"][server_name]), replacing=replacing
        )
        await self._isolation.amutate(claim)
        resource_manager.write_json(self.config_path, config)

        logger.info(f"Installed MCP server: {server_name}")
        return {"status": "success", "server_name": server_name}

    def _resolve_env_sentinel(self, value: str) -> str:
        """解析 curated env 哨兵（委派 SSOT ``resolve_env_sentinel``）；輸出夾另確保存在。"""
        resolved = resolve_env_sentinel(value)
        if value == OUTPUT_DIR_SENTINEL:
            resource_manager.make_dir(resolved)
        return resolved

    # ------------------------------------------------------------------
    # 直接創建/註冊（CreationEngine 用，ADR-0069）
    # ------------------------------------------------------------------

    def get_server_names(self) -> set[str]:
        """回傳 server_config.json 目前已註冊的 server 名稱集合。"""
        data = _read_json_or_raise(self.config_path)
        if not data:
            return set()
        servers = data.get("mcpServers", {})
        return set(servers.keys()) if isinstance(servers, dict) else set()

    def register_server(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        *,
        origin: Literal["creation", "scaffold"] = "creation",
    ) -> dict:
        """直接註冊單一 MCP server 至 server_config.json（創建用，非 hub 安裝）。

        與 ``install()`` 不同：不需 hub_id，由呼叫端提供 command/args/env，
        以 upsert 寫入單筆條目並保留既有條目。命令過 shell 注入防護
        (ADR-0033 M4 / mcp_security SSOT)。``origin`` 由呼叫端的程式路徑決定（創建或
        scaffold），沒帶時當自建；先寫隔離設定檔再寫設定（ADR-0275）。
        """
        if not is_command_safe(command):
            raise ToukaException(ErrorCode.VALIDATION_ERROR)

        config: dict = {"mcpServers": {}}
        data = _read_json_or_raise(self.config_path)
        if data:
            config = data
        if "mcpServers" not in config:
            config["mcpServers"] = {}

        # 整條目重寫：同名重註冊不得抹掉該台既有的政策欄位（ADR-0195）。
        existing = config["mcpServers"].get(name)
        entry = mcp_tool_policy.carry_over_policy(
            {
                "command": command,
                "args": args,
                "env": env or {},
                "enabled": True,
            },
            existing,
        )
        self._isolation.mutate(assign(name, origin, bound_of(entry), fresh=existing is None))
        config["mcpServers"][name] = entry
        resource_manager.write_json(self.config_path, config)
        logger.info("Registered MCP server: %s", name)
        return {"status": "success", "server_name": name}
