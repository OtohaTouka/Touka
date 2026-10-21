"""技能 ↔ MCP 接線：啟動遷移舊 server key ＋ 接線健檢（ADR-0102）。

補上「技能宣告的 MCP server key ≠ 實機 registry 註冊 key」這類無聲失敗：

- ``migrate_stale_server_keys``：把 ``server_config.json`` 中「key == 某 curated 條目 ``id``」
  的舊殘留（如 ``curated-mcp-docgen``）改名為 install 衍生 key（``name`` → ``derive_server_key``，
  如 ``mcp-docgen``）。冪等、僅在「目標 key 尚未存在」時改名，絕不覆蓋既有/自訂 server。
- ``validate_skill_mcp_wiring``：對 first-party 技能 ``.mcp.json`` 宣告的 **hub-installable**
  MCP server（有 ``command`` 者），檢查是否已在 registry 註冊；缺 → 回 WARNING 訊息清單
  （**非 fail-fast**——MCP 安裝是使用者選項，缺不該擋啟動，只把無聲變有聲）。

CI 看不到 live ``server_config.json``（gitignored），故本檢查**在啟動的真實環境跑**（比照
ADR-0096 的 live 健檢判斷）；機制由單元測試守。
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.config import get_config_dir
from src.services.mcp_hub_service import derive_server_key
from src.services.mcp_isolation_store import McpIsolationStore, copy_to, delete
from src.services.resource_manager import resource_manager

logger = logging.getLogger(__name__)

_MCP_FILE = ".mcp.json"


def _curated_id_to_key(config_dir: Path) -> dict[str, str]:
    """讀 ``mcp_hub.json``，回傳 curated 條目 ``id`` → install 衍生 key 的對照表。"""
    hub = resource_manager.read_json(config_dir / "mcp_hub.json") or {}
    out: dict[str, str] = {}
    for e in hub.get("hub", []) or []:
        if e.get("source") == "curated" and e.get("id") and e.get("name"):
            out[e["id"]] = derive_server_key(e["name"])
    return out


def migrate_stale_server_keys(config_dir: Path | str | None = None) -> list[tuple[str, str]]:
    """把 ``server_config.json`` 中以 curated 條目 ``id`` 命名的舊 key 改回 install 衍生 key。

    冪等：僅在「舊 key 存在、衍生 key 與舊 key 不同、且衍生 key 尚未存在」時改名（避免覆蓋
    既有/自訂 server）。回傳 ``[(old, new), ...]``；無變更回 ``[]``。隔離設定檔的紀錄先複製到
    新名稱、改名寫完後才刪舊名稱；紀錄讀不到時丟 ``PERSISTENCE_ERROR``、不改名（ADR-0275）。
    """
    cfg_dir = Path(config_dir) if config_dir else get_config_dir()
    server_path = cfg_dir / "server_config.json"
    config = resource_manager.read_json(server_path) or {}
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    renames: list[tuple[str, str]] = []
    for old_key, target_key in _curated_id_to_key(cfg_dir).items():
        if old_key in servers and target_key != old_key and target_key not in servers:
            renames.append((old_key, target_key))
    if renames:
        store = McpIsolationStore(cfg_dir)
        store.mutate(copy_to(renames))
        for old, new in renames:
            servers[new] = servers.pop(old)
        resource_manager.write_json(server_path, config)
        store.mutate(delete(old for old, _new in renames))
        for old, new in renames:
            logger.info("🔧 [MCP] 正規化 server key：%s → %s（ADR-0102）", old, new)
    return renames


def validate_skill_mcp_wiring(
    registered: set[str], config_dir: Path | str | None = None
) -> list[str]:
    """檢查 first-party 技能宣告的 hub-installable MCP server 是否已註冊。

    回傳 WARNING 訊息清單（非 fail）。``registered``＝目前已註冊的 server key 集合。
    僅檢有 ``command`` 的 server（可 hub 安裝者）；SSE/url 類內部 server（如 touka-dispatch）
    連線方式不同，不在範圍。
    """
    cfg_dir = Path(config_dir) if config_dir else get_config_dir()
    skills_dir = cfg_dir / "skills"
    if not resource_manager.exists(skills_dir):
        return []
    warnings: list[str] = []
    for rel in resource_manager.glob_search(skills_dir, _MCP_FILE):
        data = resource_manager.read_json(skills_dir / rel)
        if not isinstance(data, dict):
            continue
        skill_id = rel.split("/")[0]
        for server_key, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            # 可安裝形態＝本地 stdio 或 streamable HTTP remote（ADR-0192 起後者亦可裝）。
            # SSE 與形狀不明者非 Touka 支援的傳輸，不納入接線健檢。
            installable = bool(cfg.get("command")) or (
                bool(cfg.get("url")) and cfg.get("type") != "sse"
            )
            if not installable:
                continue
            if server_key not in registered:
                warnings.append(
                    f"技能 `{skill_id}` 需要 MCP server `{server_key}`，但未在 registry 註冊"
                    f"（請至 設定→MCP Hub 安裝，或確認 server key 一致）。"
                )
    return warnings
