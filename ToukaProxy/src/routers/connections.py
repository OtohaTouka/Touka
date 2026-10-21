"""連線設定路由 (Connection Settings Router).

管理使用者自訂的外部 API 連線（OpenAI 相容端點等）。

N1/N2 fix: All endpoints require a verified identity with Owner/Debugger/admin role.
           The /status endpoint sanitizes error messages to prevent credential leakage.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.authorization_gateway import require_privileged
from src.services.model_manager import model_manager
from src.services.providers import registry
from src.services.resource_manager import resource_manager
from src.utils.secrets import is_masked, mask_secret
from src.utils.ssrf_guard import UnsafeUrlError, validate_outbound_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ui/connections", tags=["UI Connections"])

# P9 fix: serialization lock to prevent concurrent file corruption
_connections_lock = threading.Lock()


def get_connections_config_path() -> str:
    """獲取連線設定檔路徑 (SSOT)."""
    return str(get_config_dir() / "connections.json")


# 保持向後相容的常量
CONFIG_PATH = get_connections_config_path()


class ConnectionItem(BaseModel):
    """單一外部 API 連線設定模型。"""

    id: str
    name: str = "New Connection"
    url: str
    apiKey: str | None = None
    provider: str = "custom"
    protocol: str = "openai"
    enabled: bool = True


# Require Owner/Debugger/admin to manage connections (N1/N2 fix).
_require_connections_role = require_privileged


def _check_connection_conflicts(connections: list[ConnectionItem]) -> None:
    """Q2 守衛：角色分離＋重複偵測（落盤前；金鑰比對只在伺服器端，永不回前端）。

    - 角色分離：官方上游供應商且系統已設金鑰 → 擋（請改用 Provider Keys；連線頁僅供
      自架／代理／聚合端點）。
    - 重複端點：提交清單內 (provider, url) 相同 → 擋。
    - 重複金鑰：提交清單內同一把真值金鑰用於多個連線（遮罩/空值不比對）→ 擋。
    """
    model_provider_ids = {s.id for s in registry.discoverable_specs()}
    seen_endpoints: set[tuple[str, str]] = set()
    seen_key_hashes: set[str] = set()
    for c in connections:
        # provider 正規化（去空白＋小寫）→ 防大小寫繞過角色分離/重複偵測
        provider = (c.provider or "").strip().lower()
        if provider in model_provider_ids:
            spec = registry.spec_by_id(provider)
            if spec and registry.get_key(spec):
                raise ToukaException(
                    ErrorCode.VALIDATION_ERROR,
                    detail_override=(
                        f"供應商 '{provider}' 已在【系統金鑰】設定；請於該處管理。"
                        "連線頁僅供自架／代理／聚合端點使用。"
                    ),
                )
        endpoint = (provider, (c.url or "").strip().rstrip("/").lower())
        if endpoint in seen_endpoints:
            raise ToukaException(
                ErrorCode.VALIDATION_ERROR,
                detail_override=f"重複的連線（供應商與網址相同）：{c.provider} @ {c.url}",
            )
        seen_endpoints.add(endpoint)
        key = (c.apiKey or "").strip()
        if key and not is_masked(key):
            # 以雜湊比對，避免明文金鑰留在比對集合中
            key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
            if key_hash in seen_key_hashes:
                raise ToukaException(
                    ErrorCode.VALIDATION_ERROR,
                    detail_override="偵測到重複的 API 金鑰用於多個連線；請勿重複輸入同一把金鑰。",
                )
            seen_key_hashes.add(key_hash)


@router.get("", response_model=list[ConnectionItem])
async def list_connections(
    _identity: AgentIdentity = Depends(_require_connections_role),  # noqa: B008
) -> list[dict[str, Any]]:
    """列出所有已設定的外部 API 連線。(N1/N2 fix: requires Owner/admin)

    安全（遮罩 SSOT）：apiKey 真值**永不回前端**——已設定者回遮罩佔位、未設定者維持空。
    前端編輯時把遮罩佔位原樣送回 → POST 端據此保留既有真值（write-only）。
    """
    data = resource_manager.read_json(CONFIG_PATH)
    connections = data.get("connections", []) if data else []
    return [{**c, "apiKey": mask_secret(c.get("apiKey"))} for c in connections]


@router.post("")
async def save_connections(
    connections: list[ConnectionItem],
    _identity: AgentIdentity = Depends(_require_connections_role),  # noqa: B008
) -> dict[str, str]:
    """儲存外部 API 連線設定。(N1/N2 fix: requires Owner/admin)

    安全（write-only 金鑰）：收到遮罩佔位的 apiKey 代表「未變更」→ 以 id 對應、保留磁碟上
    既有真值，遮罩值絕不落盤。真值或空字串則照常寫入（空＝清除/無金鑰）。

    SSRF 守衛：先驗證每個連線網址（拒絕 metadata/link-local 等受封鎖目標；自架/區網
    localhost/私網仍放行），在落盤前擋下不安全設定，避免日後發現/路由對其發請求。
    """
    # 前置驗證（在鎖與寫檔之外）→ 不安全網址回 VALIDATION_ERROR（4xx），不被外層轉成 500
    for c in connections:
        url = (c.url or "").strip()
        if not url:
            # 明確拒絕空網址（避免存得下、刷新時才靜默失敗的不一致）
            raise ToukaException(
                ErrorCode.VALIDATION_ERROR,
                detail_override=f"連線 '{c.name or c.id}' 的網址不能為空",
            )
        try:
            validate_outbound_url(url)
        except UnsafeUrlError as exc:
            raise ToukaException(
                ErrorCode.VALIDATION_ERROR,
                detail_override=(
                    f"連線 '{c.name or c.id}' 的網址未通過安全檢查：{exc}"
                ),
            ) from exc

    _check_connection_conflicts(connections)  # Q2：角色分離＋重複偵測

    with _connections_lock:  # P9 fix: prevent concurrent file corruption
        try:
            existing = resource_manager.read_json(CONFIG_PATH) or {}
            existing_keys = {
                c.get("id"): c.get("apiKey")
                for c in existing.get("connections", [])
            }
            resolved: list[dict[str, Any]] = []
            for c in connections:
                item = c.model_dump()
                # 去空白後再判遮罩 → 防「遮罩值補空白」繞過 write-only 保護
                if is_masked((item.get("apiKey") or "").strip()):
                    item["apiKey"] = existing_keys.get(item.get("id"))
                resolved.append(item)

            resource_manager.make_dir(Path(CONFIG_PATH).parent)
            resource_manager.write_json(
                CONFIG_PATH,
                {"connections": resolved},
                indent=4,
            )
            model_manager.invalidate_custom_routes(resolved)
            logger.info("✅ Connections synchronized and saved to %s", CONFIG_PATH)
        except Exception as exc:
            logger.exception("Failed to save connections.json")
            raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(exc)) from exc
    return {"status": "success"}


@router.get("/status")
async def get_connections_status(
    _identity: AgentIdentity = Depends(_require_connections_role),  # noqa: B008
) -> dict[str, Any]:
    """獲取各個連線的掃描狀態。

    N2 fix: sanitize error messages to prevent API key / credential leakage.
    Raw exception strings are replaced with generic indicators.
    """
    errors = model_manager.get_connection_errors()
    sanitized: dict[str, str] = {}
    for conn_id, error_msg in errors.items():
        # Redact any value that looks like a bearer token, API key, or credential
        if error_msg:
            sanitized[conn_id] = "connection_error"
        else:
            sanitized[conn_id] = "ok"
    return sanitized
