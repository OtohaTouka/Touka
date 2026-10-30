"""模型管理員 (ModelManager) 模組。

負責系統中所有模型清單的載入、儲存、動態抓取與健康管理。
"""
import asyncio
import json
import logging
import re
from typing import Any

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.agent_service import agent_service
from src.services.models.fetcher import (
    DiscoveryError,
    fetch_anthropic_models,
    fetch_deepseek_models,
    fetch_gemini_models,
    fetch_grok_models,
    fetch_openai_compatible_models,
    fetch_openai_models,
)
from src.services.models.health import validate_model_health
from src.services.models.routes import (
    CustomModelRoute,
    CustomRouteDisposition,
    CustomRouteResolution,
    ModelRouteDiagnostic,
    ModelRouteSnapshot,
    ModelRouteSnapshotState,
)
from src.services.providers import registry
from src.utils import config_utils
from src.utils.opaque_identifier import opaque_identifier
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)

# N7/N9 fix: patterns that indicate credential material in error messages.
_CREDENTIAL_PATTERNS = re.compile(
    r"(?i)"
    r"(?:Bearer|Basic|Token|Authorization)\s+\S+"  # Auth header values
    r"|sk-[A-Za-z0-9\-_]+"                         # OpenAI-style API keys
    r"|key=[A-Za-z0-9\-_]{8,}"                     # URL key params
    r"|api[-_]?key\s*[=:]\s*\S+"                   # api_key=... patterns
    r"|[Pp]assword\s*[=:]\s*\S+"                   # password=...
    r"|//[^/\s:@]+:[^/\s@]+@"                       # URL userinfo (user:pass@host)
)

_CONTROL_PLANE_PROVIDERS = frozenset({"proxy"})
_LEGACY_ROUTE_FIELDS = frozenset({"_source_url", "_api_key"})
_MAX_ROUTE_REFRESH_ATTEMPTS = 3


def _sanitize_connection_error(error_msg: str) -> str:
    """Redact credential-like strings from connection error messages.

    N7/N9 fix: prevents API keys and bearer tokens that appear in exception
    messages (e.g., HTTP error responses, URL strings) from being stored
    in _connection_errors where they might later be exposed via the API.
    """
    return _CREDENTIAL_PATTERNS.sub("[REDACTED]", error_msg)


def _sanitize_model_descriptor(model: dict[str, Any]) -> dict[str, Any]:
    """公開／持久化模型 descriptor 不得包含任何 runtime-only 底線欄位。"""
    return {key: value for key, value in model.items() if not key.startswith("_")}


def _connections_fingerprint(data: dict[str, Any]) -> str:
    """以 canonical JSON 產生不公開的設定版本指紋。"""
    canonical = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return opaque_identifier(canonical, namespace="connections")


class ModelManager:
    """模型管理員服務。

    負責管理系統中的所有模型（Level 0 基礎模型與 Level 1/2 代理人模型），
    處理模型清單的持久化、動態抓取與健康檢查。
    """

    def __init__(self) -> None:
        """初始化模型管理員.

        加載初始模型快取與黑名單。
        """
        self._models_cache: list[dict[str, Any]] = []
        self._blacklist: list[str] = []
        self._custom_refresh_lock = asyncio.Lock()
        self._route_snapshot = ModelRouteSnapshot.create(
            generation=0,
            config_fingerprint="",
        )
        # 官方供應商發現的分類錯誤（provider_id → {code, message}）；不靜默失敗用。
        self._discovery_errors: dict[str, dict[str, str]] = {}
        self._remember_connections(self._read_connections_snapshot()[0]["connections"])
        self.load_initial_config()

    def reset_for_test(self) -> None:
        """重置測試狀態，清空快取。"""
        self._models_cache = []
        self._blacklist = []
        self._custom_refresh_lock = asyncio.Lock()
        self._route_snapshot = ModelRouteSnapshot.create(
            generation=0,
            config_fingerprint="",
        )
        self._discovery_errors = {}
        self._remember_connections(self._read_connections_snapshot()[0]["connections"])
        self._load_blacklist()
        self.load_initial_config()
        logger.info("🛡️ ModelManager test state reset.")

    def _get_config_path(self, filename: str) -> str:
        """此方法已廢棄，請直接傳送檔名至 config_utils。"""
        return filename

    def _load_blacklist(self) -> None:
        filename = "models_blacklist.json"
        if config_utils.config_exists(filename):
            data = config_utils.read_config_json(filename)
            if data:
                self._blacklist = data.get("blacklist", [])
                logger.info(
                    "🚫 Loaded %d blacklisted models.",
                    len(self._blacklist),
                )

    def is_blacklisted(self, model_name: str) -> bool:
        """檢查模型是否在黑名單中。"""
        return any(b.lower() in model_name.lower() for b in self._blacklist)

    def _save_config(self) -> None:
        filename = "models.json"
        try:
            # 執行期路由已由 `_custom_routes` 獨立保存；此處仍剝除全部底線欄位作縱深防禦，
            # 防止舊 plugin／滾動升級資料把端點或金鑰冗餘落地 models.json。
            sanitized = [_sanitize_model_descriptor(m) for m in self._models_cache]
            data = {"models": sanitized}
            config_utils.save_config_json(filename, data)
        except Exception:
            logger.exception("Error saving models.json")
            raise

    def load_initial_config(self) -> None:
        """從 models.json 載入初始模型設定。"""
        filename = "models.json"
        models = []
        if config_utils.config_exists(filename):
            data = config_utils.read_config_json(filename)
            if data and isinstance(data, list):
                models = data
            elif data and isinstance(data, dict) and "models" in data:
                models = data["models"]

        sanitized_models: list[dict[str, Any]] = []
        legacy_pollution_found = False
        for model in models if isinstance(models, list) else []:
            if not isinstance(model, dict):
                continue
            sanitized = _sanitize_model_descriptor(model)
            legacy_pollution_found = legacy_pollution_found or any(
                field in model for field in _LEGACY_ROUTE_FIELDS
            )
            sanitized_models.append(sanitized)

        self._models_cache = sanitized_models
        known_custom = frozenset(
            str(model.get("id"))
            for model in sanitized_models
            if model.get("id") and model.get("owned_by") == "custom"
        )
        if known_custom:
            self._route_snapshot = ModelRouteSnapshot.create(
                generation=self._route_snapshot.generation + 1,
                config_fingerprint=self._route_snapshot.config_fingerprint,
                known_custom_model_ids=known_custom,
            )
        if legacy_pollution_found:
            self._save_config()
        logger.info("Loaded %d models from config.", len(self._models_cache))

    # --- 核心合併邏輯 ---

    def _merge_models(self, new_models: list[dict[str, Any]]) -> None:
        """將新抓取的模型合併至快取。

        - 新模型：加入並設定預設值。
        - 既有模型：更新 last_seen，保留 enabled/visibility。
        - 黑名單：自動過濾。
        """
        now = get_current_epoch()
        existing_map = {m["id"]: m for m in self._models_cache}

        for raw_model in new_models:
            # 舊制把路由 URL／key 混入模型 descriptor；路由現已移至 `_custom_routes` SSOT。
            # 此處仍做縱深清除，避免測試替身、舊 plugin 或滾動升級期間重新注入秘密。
            model = _sanitize_model_descriptor(raw_model)
            model_id = model.get("id", "")
            if not model_id or self.is_blacklisted(model_id):
                continue

            if model_id in existing_map:
                existing = existing_map[model_id]
                existing["last_seen"] = now
                for k, v in model.items():
                    if k.startswith("_"):
                        existing[k] = v
            else:
                # 新模型：設定預設值
                model.setdefault("enabled", True)
                model.setdefault("visibility", "visible")
                model["last_seen"] = now
                self._models_cache.append(model)
                existing_map[model_id] = model

        try:
            self._save_config()
        except Exception:
            logger.warning("Models merged in memory but failed to persist to disk.")

    # --- 動態抓取方法 ---

    async def _run_discovery(
        self, provider_id: str, label: str, models_coro: Any
    ) -> None:
        """執行單一供應商發現：合併結果，並記錄/清除分類錯誤（不靜默失敗）。

        成功（含「金鑰存在但回 0 個模型」）→ 清除舊錯誤；DiscoveryError → 記分類碼；其餘
        例外 → 記為 'error'。錯誤訊息一律經 `_sanitize_connection_error` 沙化，防金鑰外洩。
        """
        try:
            models = await models_coro
            if models:
                self._merge_models(models)
                logger.info("📡 Fetched %d %s models.", len(models), label)
            self._discovery_errors.pop(provider_id, None)
        except DiscoveryError as e:
            self._discovery_errors[provider_id] = {
                "code": e.code,
                "message": _sanitize_connection_error(e.message),
            }
            logger.warning("Discovery failed for %s [%s]", label, e.code)
        except Exception as e:
            self._discovery_errors[provider_id] = {
                "code": "error",
                "message": _sanitize_connection_error(str(e)),
            }
            logger.exception("Failed to fetch %s models", label)

    def _provider_key(self, provider_id: str) -> str:
        """經 registry（單一 chokepoint、即時讀取）取得供應商金鑰。"""
        spec = registry.spec_by_id(provider_id)
        return registry.get_key(spec) if spec else ""

    async def fetch_latest_gemini_models(self) -> None:
        """從 Google 抓取最新 Gemini 模型清單。"""
        key = self._provider_key("gemini")
        await self._run_discovery("gemini", "Gemini", fetch_gemini_models(key, self.is_blacklisted))

    async def fetch_latest_openai_models(self) -> None:
        """從 OpenAI 抓取最新模型清單。"""
        key = self._provider_key("openai")
        await self._run_discovery("openai", "OpenAI", fetch_openai_models(key, self.is_blacklisted))

    async def fetch_latest_grok_models(self) -> None:
        """從 xAI 抓取最新 Grok 模型清單。"""
        key = self._provider_key("grok")
        await self._run_discovery("grok", "Grok", fetch_grok_models(key, self.is_blacklisted))

    async def fetch_latest_anthropic_models(self) -> None:
        """從 Anthropic API 抓取最新 Claude 模型清單。"""
        key = self._provider_key("anthropic")
        await self._run_discovery(
            "anthropic", "Anthropic", fetch_anthropic_models(key, self.is_blacklisted)
        )

    async def fetch_latest_deepseek_models(self) -> None:
        """從 DeepSeek API（OpenAI 相容）抓取最新模型清單。"""
        key = self._provider_key("deepseek")
        await self._run_discovery(
            "deepseek", "DeepSeek", fetch_deepseek_models(key, self.is_blacklisted)
        )

    # 發現格式 → 對應的具名抓取方法（registry SSOT 驅動）。
    _DISCOVERY_METHODS = {
        "gemini": "fetch_latest_gemini_models",
        "openai": "fetch_latest_openai_models",
        "grok": "fetch_latest_grok_models",
        "anthropic": "fetch_latest_anthropic_models",
        "deepseek": "fetch_latest_deepseek_models",
    }

    async def refresh_all_dynamic_models(self) -> int:
        """觸發所有動態模型來源的刷新（registry SSOT 驅動）。回傳新發現模型數量。

        對「金鑰已設定」的每個可發現供應商，依其 discovery_format 呼叫對應抓取方法；外加
        自訂連線發現。供應商清單由 Provider Registry 衍生——不再於此（及 main.py 啟動）各自
        硬編碼一份 gather 清單。
        """
        before = len(self._models_cache)
        self._discovery_errors = {}  # 本輪重新累計（清除上輪殘留）
        await asyncio.gather(*self._build_discovery_tasks())
        return max(0, len(self._models_cache) - before)

    def get_discovery_errors(self) -> dict[str, dict[str, str]]:
        """回傳本輪官方供應商發現的分類錯誤（已沙化）複本。"""
        return {k: dict(v) for k, v in self._discovery_errors.items()}

    async def probe_provider_keys(self, env_names: set[str]) -> dict[str, str]:
        """探測「金鑰剛變動」的官方供應商：合法則發現其模型（自動上架），回探測結果。

        回傳 {env_name: "valid" | "invalid:<code>" | "cleared"}。僅針對受影響的供應商
        （不對全部 fan-out），且皆為官方固定端點（無 SSRF 疑慮）。清除（金鑰空）→ "cleared"，
        其模型由 get_models 的 health 邏輯自動標離線（反向生命週期，免在此刪除）。
        """
        result: dict[str, str] = {}
        for spec in registry.discoverable_specs():
            affected = set(spec.key_attrs) & env_names
            if not affected:
                continue
            if not registry.get_key(spec):
                for env in affected:
                    result[env] = "cleared"
                continue
            method_name = self._DISCOVERY_METHODS.get(spec.discovery_format or "")
            if method_name:
                await getattr(self, method_name)()  # 經 _run_discovery，記錄 _discovery_errors
            err = self._discovery_errors.get(spec.id)
            verdict = f"invalid:{err['code']}" if err else "valid"
            for env in affected:
                result[env] = verdict
        return result

    def _build_discovery_tasks(self) -> list:
        """組出本次發現要執行的協程清單（金鑰存在才納入）＋自訂連線。"""
        tasks = []
        for spec in registry.discoverable_specs():
            if not registry.get_key(spec):
                continue  # 金鑰未設定 → 跳過（不對外發請求）
            method_name = self._DISCOVERY_METHODS.get(spec.discovery_format or "")
            if method_name:
                tasks.append(getattr(self, method_name)())
        tasks.append(self.fetch_custom_connections_models())
        return tasks

    def _read_connections_snapshot(self) -> tuple[dict[str, Any], str]:
        """讀取連線設定與 canonical fingerprint；缺檔／空檔視為空集合。"""
        filename = "connections.json"
        data = (
            config_utils.read_config_json(filename)
            if config_utils.config_exists(filename)
            else None
        )
        normalized = data if isinstance(data, dict) else {"connections": []}
        connections = normalized.get("connections")
        if not isinstance(connections, list):
            normalized = {**normalized, "connections": []}
        return normalized, _connections_fingerprint(normalized)

    def _publish_route_snapshot(
        self,
        *,
        config_fingerprint: str,
        active_routes: dict[str, CustomModelRoute] | None = None,
        known_custom_model_ids: frozenset[str] | None = None,
        diagnostics: tuple[ModelRouteDiagnostic, ...] = (),
        state: ModelRouteSnapshotState,
    ) -> None:
        """以單一 reference swap 發布完整路由狀態。"""
        previous = self._route_snapshot
        self._route_snapshot = ModelRouteSnapshot.create(
            generation=previous.generation + 1,
            config_fingerprint=config_fingerprint,
            active_routes=active_routes,
            known_custom_model_ids=(
                known_custom_model_ids
                if known_custom_model_ids is not None
                else previous.known_custom_model_ids
            ),
            diagnostics=diagnostics,
            state=state,
        )

    def _remember_connections(self, connections: list[Any]) -> None:
        """記下設定裡每條連線的 URL（停用、還沒探測、探測失敗的都算），給 ``route_endpoints``。"""
        self._configured_urls = frozenset(
            url
            for conn in connections
            if isinstance(conn, dict) and (url := str(conn.get("url", "") or "").strip())
        )

    def route_endpoints(self) -> frozenset[str]:
        """唯讀：已設定的全部連線 URL 加上目前路由的 endpoint（不可變的副本）。

        隔離封鎖埠的上游來源：存檔後立刻包含新連線，不等探測；探測失敗、停用的也在。
        """
        routes = self._route_snapshot.active_routes.values()
        return self._configured_urls | {route.endpoint for route in routes}

    def reload_connections(self) -> None:
        """``connections.json`` 被整份寫入後（設定快照匯入、啟動自癒補檔）照磁碟內容關閉舊路由。

        ``route_endpoints`` 立刻跟上；和連線設定存檔同一套（``invalidate_custom_routes``）。
        """
        self.invalidate_custom_routes(self._read_connections_snapshot()[0]["connections"])

    def invalidate_custom_routes(self, connections: list[dict[str, Any]]) -> None:
        """Owner mutation 落盤後、任何 remote await 前先關閉舊路由。"""
        self._remember_connections(connections)
        normalized = {"connections": connections}
        known = self._route_snapshot.known_custom_model_ids | frozenset(
            self._route_snapshot.active_routes
        )
        self._publish_route_snapshot(
            config_fingerprint=_connections_fingerprint(normalized),
            known_custom_model_ids=known,
            state=ModelRouteSnapshotState.INITIALIZING,
        )

    async def fetch_custom_connections_models(self) -> None:
        """序列化刷新，設定版本漂移時丟棄舊結果並保持 fail-closed。"""
        async with self._custom_refresh_lock:
            await self._fetch_custom_connections_models_locked()

    async def _fetch_custom_connections_models_locked(self) -> None:
        """持有 refresh lock 時建立並發布單一 immutable snapshot。"""
        for _attempt in range(_MAX_ROUTE_REFRESH_ATTEMPTS):
            data, fingerprint = self._read_connections_snapshot()
            connections = data["connections"]
            self._remember_connections(connections)
            previous = self._route_snapshot
            known_custom_ids = previous.known_custom_model_ids | frozenset(
                previous.active_routes
            )

            # 一開始即撤下舊 route，避免 endpoint/key mutation 的 discovery 視窗仍可送出舊憑證。
            self._publish_route_snapshot(
                config_fingerprint=fingerprint,
                known_custom_model_ids=known_custom_ids,
                state=ModelRouteSnapshotState.INITIALIZING,
            )

            next_routes: dict[str, CustomModelRoute] = {}
            diagnostics: list[ModelRouteDiagnostic] = []
            discovered_models: list[dict[str, Any]] = []

            # 滾動升級縱深清理：舊 plugin 可能已把路由欄位混入快取。
            self._models_cache = [
                _sanitize_model_descriptor(model) for model in self._models_cache
            ]

            for conn in connections:
                if not isinstance(conn, dict) or not conn.get("enabled", True):
                    continue

                conn_id = str(conn.get("id", "unknown"))
                connection_handle = opaque_identifier(conn_id, namespace="connection")
                provider = str(conn.get("provider", "custom") or "custom").strip().lower()
                if provider in _CONTROL_PLANE_PROVIDERS:
                    logger.debug(
                        "Skipping control-plane connection %s during model discovery.",
                        connection_handle,
                    )
                    continue

                source_url = str(conn.get("url", "") or "").strip()
                api_key = str(conn.get("apiKey", "") or "")
                try:
                    models = await fetch_openai_compatible_models(
                        base_url=source_url,
                        api_key=api_key,
                        provider=provider,
                        blacklist_checker=self.is_blacklisted,
                    )
                    if models:
                        endpoint = registry.normalize_chat_url(source_url)
                        provider_spec = registry.spec_by_id(provider)
                        provider_label = provider_spec.label if provider_spec else "custom"
                        accepted = 0
                        for model in models:
                            model_id = str(model.get("id", "") or "")
                            if not model_id:
                                continue
                            known_custom_ids = known_custom_ids | {model_id}
                            if model_id in next_routes:
                                diagnostics.append(
                                    ModelRouteDiagnostic(
                                        connection_handle=connection_handle,
                                        code="route_conflict",
                                    )
                                )
                                continue
                            next_routes[model_id] = CustomModelRoute(
                                connection_id=conn_id,
                                endpoint=endpoint,
                                api_key=api_key,
                                provider=provider_label,
                            )
                            discovered_models.append(_sanitize_model_descriptor(model))
                            accepted += 1
                        logger.info(
                            "📡 Fetched %d models from connection %s.",
                            accepted,
                            connection_handle,
                        )
                except Exception as exc:
                    diagnostics.append(
                        ModelRouteDiagnostic(
                            connection_handle=connection_handle,
                            code="discovery_failed",
                        )
                    )
                    logger.warning(
                        "❌ Model discovery failed for connection %s (%s).",
                        connection_handle,
                        type(exc).__name__,
                    )

            _latest_data, latest_fingerprint = self._read_connections_snapshot()
            if latest_fingerprint != fingerprint:
                logger.warning("Discarded stale custom model discovery result.")
                continue

            if discovered_models:
                self._merge_models(discovered_models)
            self._publish_route_snapshot(
                config_fingerprint=fingerprint,
                active_routes=next_routes,
                known_custom_model_ids=frozenset(known_custom_ids),
                diagnostics=tuple(diagnostics),
                state=(
                    ModelRouteSnapshotState.DEGRADED
                    if diagnostics
                    else ModelRouteSnapshotState.READY
                ),
            )
            return

        _latest_data, latest_fingerprint = self._read_connections_snapshot()
        self._publish_route_snapshot(
            config_fingerprint=latest_fingerprint,
            known_custom_model_ids=(
                self._route_snapshot.known_custom_model_ids
                | frozenset(self._route_snapshot.active_routes)
            ),
            diagnostics=(
                ModelRouteDiagnostic(
                    connection_handle="connection-global",
                    code="config_changed_during_discovery",
                ),
            ),
            state=ModelRouteSnapshotState.DEGRADED,
        )

    def resolve_custom_route(self, model_name: str) -> CustomRouteResolution:
        """從同一 immutable snapshot 判定 active／known-inactive／unknown（ADR-0250 A5）。

        先找寫法完全相同的；沒有才在啟用＋已知裡不分大小寫找，只有一個符合才用（帶原名），
        兩個以上丟 409 ``MODEL_NAME_AMBIGUOUS``、不挑。
        """
        snapshot = self._route_snapshot
        names = snapshot.known_custom_model_ids | frozenset(snapshot.active_routes)
        if model_name not in names:
            folded = model_name.casefold()
            matches = [name for name in names if name.casefold() == folded]
            if len(matches) > 1:
                raise ToukaException(ErrorCode.MODEL_NAME_AMBIGUOUS)
            model_name = matches[0] if matches else model_name
        route = snapshot.active_routes.get(model_name)
        if route is not None:
            return CustomRouteResolution(
                disposition=CustomRouteDisposition.ACTIVE_CUSTOM,
                route=route,
                original_name=model_name,
            )
        if model_name in snapshot.known_custom_model_ids:
            return CustomRouteResolution(
                disposition=CustomRouteDisposition.KNOWN_CUSTOM_INACTIVE,
                original_name=model_name,
            )
        return CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN)

    def request_model_name(self, model_name: str) -> str:
        """靈魂存的模型名 → 送出用的名字：自訂連線回原名，其他轉小寫；模糊丟 409（ADR-0250 A5）。"""
        return self.resolve_custom_route(model_name).original_name or model_name.lower()

    def get_custom_route(self, model_name: str) -> CustomModelRoute | None:
        """相容入口；只回傳 active custom route。"""
        return self.resolve_custom_route(model_name).route

    def get_connection_errors(self) -> dict[str, str]:
        """回傳只含 opaque handle 與 typed code 的診斷投影。"""
        return {
            diagnostic.connection_handle: diagnostic.code
            for diagnostic in self._route_snapshot.diagnostics
        }

    async def check_models_health(self) -> None:
        """檢查所有模型的健康狀態，更新健康模型的 last_seen。"""
        if not self._models_cache:
            return

        try:
            _models, results = await validate_model_health(
                self._models_cache
            )
            now = get_current_epoch()
            updated = False
            for model, is_healthy in zip(
                self._models_cache, results, strict=False
            ):
                if is_healthy:
                    model["last_seen"] = now
                    updated = True
            if updated:
                self._save_config()
        except Exception:
            logger.exception("Failed to run health check")


    def prune_offline_models(self, threshold_seconds: int = 86400 * 7) -> None:
        """清理長時間不見的離線模型。
        
        實施 L1 保護：只要物理靈魂目錄存在，即受保護而不被清理。
        """
        now_epoch = get_current_epoch()
        original_count = len(self._models_cache)
        
        def should_keep(m: dict[str, Any]) -> bool:
            """判斷模型是否應該保留在快取中.

            Args:
                m (dict[str, Any]): 模型資訊字典。

            Returns:
                bool: 若模型應被保留則回傳 True，否則回傳 False。
            """
            if m.get("enabled"):
                return True
            if m.get("owned_by") in ("touka", "proxy"):
                return True
            
            # 硬執法：保護已發現的靈魂代理人 (L1 Protection)
            agent_ids = {a["id"] for a in agent_service.get_all_agents()}
            m_id = m.get("id", "")
            if m_id in agent_ids:
                return True
            # 兼容舊版前綴 ID 的保護
            if m_id.startswith("ToukaSoul") and m_id[9:] in agent_ids:
                return True

            if "last_seen" in m:
                last_seen = m["last_seen"]
                if isinstance(last_seen, (int, float)):
                    if now_epoch - last_seen < threshold_seconds:
                        return True
                elif isinstance(last_seen, str):
                    try:
                        from datetime import UTC, datetime
                        ls_dt = datetime.fromisoformat(last_seen)
                        if ls_dt.tzinfo is None:
                            ls_dt = ls_dt.replace(tzinfo=UTC)
                        if now_epoch - ls_dt.timestamp() < threshold_seconds:
                            return True
                    except Exception:
                        logger.debug("Failed to parse last_seen ISO format, ignoring.")
                return False
            return False

        self._models_cache = [m for m in self._models_cache if should_keep(m)]
        
        pruned_count = original_count - len(self._models_cache)
        if pruned_count > 0:
            logger.info(f"🧹 Pruned {pruned_count} obsolete offline models from cache.")
            self._save_config()

    def set_models(self, models: list[dict[str, Any]]) -> None:
        """設定模型快取。"""
        self._models_cache = [_sanitize_model_descriptor(model) for model in models]
        known_custom = frozenset(
            str(model.get("id"))
            for model in self._models_cache
            if model.get("id") and model.get("owned_by") == "custom"
        )
        if known_custom:
            self._publish_route_snapshot(
                config_fingerprint=self._route_snapshot.config_fingerprint,
                active_routes=dict(self._route_snapshot.active_routes),
                known_custom_model_ids=(
                    self._route_snapshot.known_custom_model_ids | known_custom
                ),
                diagnostics=self._route_snapshot.diagnostics,
                state=self._route_snapshot.state,
            )

    def register_soul_model(self, soul_id: str) -> None:  # noqa: IDENTITY_SSOT
        """註冊靈魂路由模型到快取＋磁碟 (SSOT：_models_cache 與 models.json 一致；ADR-0145)。

        取代 souls 路由過去對 models.json 的「直接寫檔」——那條路徑不更新本單例的
        記憶體快取，任一後續 _save_config（check_models_health/mark_model_seen 等）便會
        以過時快取整包覆蓋，靜默抹掉剛註冊的 ToukaSoul<id>（隨機次序 flake／正式資料流失
        的根因）。改由本方法讓快取為權威、寫檔與記憶體同步，杜絕回寫覆蓋。

        裸插入 {"id","owned_by":"touka"} 與舊直寫等價；enabled/visibility 由 get_models
        讀取時補預設，此處不寫入以免行為漂移。已存在則為冪等 no-op（仍落盤保證一致）。
        """
        model_id = f"ToukaSoul{soul_id}"
        if not any(m.get("id") == model_id for m in self._models_cache):
            self._models_cache.insert(0, {"id": model_id, "owned_by": "touka"})
        self._save_config()

    def unregister_soul_model(self, soul_id: str) -> None:  # noqa: IDENTITY_SSOT
        """自快取＋磁碟移除靈魂路由模型（冪等）。

        雙比對正式 ID 與 ToukaSoul 前綴 ID（對齊舊 remove 語意）；僅在確有變更時落盤，
        故 create 回滾／delete 對不存在項為零寫入的 no-op。
        """
        before = len(self._models_cache)
        self._models_cache = [
            m
            for m in self._models_cache
            if m.get("id") != soul_id and m.get("id") != f"ToukaSoul{soul_id}"
        ]
        if len(self._models_cache) != before:
            self._save_config()

    def get_models(self, _include_offline: bool = True) -> list[dict[str, Any]]:
        """獲取合併了代理人 (Agents) 的統一模型清單。
        
        實施 ID 糾偏與配置動態合併：自動將 models.json 中的設定套用至發現的代理人。
        """
        # 1. 獲取掃描到的動態模型 ID (用於判定 online/offline)
        now_epoch = get_current_epoch()

        # 2. 獲取所有代理人
        try:
            agents = agent_service.get_all_agents()
        except Exception:
            logger.exception("Failed to get agents from agent_service")
            agents = []
            
        # 3. 轉換代理人為模型格式
        agent_models = []
        for a in agents:
            a_id = a.get("id")
            # 修正：Soul 類型的 Agent owned_by 應為 "proxy" 以符合前端與路由預期
            owned_by = "proxy" if a.get("type") != "custom" else "custom"
            
            # 從 models.json 尋找是否有持久化設定 (SSOT)
            # 支援精確 ID 或 帶有前綴的舊 ID
            m_config = {}
            for m in self._models_cache:
                if m.get("id") == a_id or m.get("id") == f"ToukaSoul{a_id}":
                    m_config = m
                    break

            agent_models.append({
                "id": a_id,
                "name": m_config.get("name", a.get("name", a_id)),
                "owned_by": m_config.get("owned_by", owned_by),
                "object": "model",
                "is_agent": True,
                "enabled": m_config.get("enabled", True),
                "visibility": m_config.get("visibility", "visible"),
                "status": "online",
                **{k: v for k, v in a.items() if k not in ["id", "name", "type"]}
            })
            
        # 4. 處理基礎模型
        agent_ids = {am["id"] for am in agent_models}
        final_base = []
        for m in self._models_cache:
            m_id = m.get("id")
            # 若此配置條目已對應到動態發現的 Agent，則跳過基礎模型處理，避免重複
            if m_id in agent_ids:
                continue
            # 兼容：若帶有前綴的舊 ID 已對應到正式 ID，也跳過
            if m_id.startswith("ToukaSoul") and m_id[9:] in agent_ids:
                continue
                
            status = "online"
            # 基礎模型除非長達一週沒見，否則不隨易標記為 offline，避免 API Key 偵測間隙導致模型消失
            if "last_seen" in m:
                last_seen = m["last_seen"]
                last_seen_ts = 0.0
                if isinstance(last_seen, (int, float)):
                    last_seen_ts = last_seen
                elif isinstance(last_seen, str):
                    try:
                        from datetime import UTC, datetime
                        ls_dt = datetime.fromisoformat(last_seen)
                        if ls_dt.tzinfo is None:
                            ls_dt = ls_dt.replace(tzinfo=UTC)
                        last_seen_ts = ls_dt.timestamp()
                    except Exception:
                        last_seen_ts = now_epoch - 31536000 # 舊資料當作過期
                
                # 放寬門檻至 7 天 (原為 1 小時太過激進)
                if now_epoch - last_seen_ts > 604800:
                    status = "offline"
            
            # 進階檢查：金鑰驅動官方供應商若無 API Key 則強制離線（白名單由 registry SSOT 衍生）
            if status == "online" and registry.health_key_missing(m.get("owned_by", "")):
                status = "offline"
            
            m_display = _sanitize_model_descriptor(m)
            m_display["status"] = status
            final_base.append(m_display)
            
        return agent_models + final_base

    def patch_model_settings(self, identity: AgentIdentity, settings: dict[str, Any]) -> bool:
        """更新模型的持久化設定 (Identity SSOT)"""
        updated = False
        # ADR-0111：取樣參數一併持久化（顯式 None＝清除）。僅白名單欄位，杜絕任意鍵落地。
        allowed = ("enabled", "visibility", "temperature", "top_p", "top_k")
        _sampling = ("temperature", "top_p", "top_k")
        for m in self._models_cache:
            if m["id"] == identity.agent_id:
                # ADR-0111：取樣參數僅對「原生」模型有效；custom/proxy/touka(soul) 與未知/推斷
                # owned_by 的參數另由各自 config.json 管或不適用，存進 models.json 只會變成被忽略
                # 的 dead data → 不持久化。原生判準走 registry SSOT（與 NativeModelLayer 注入、
                # ModelsTab `params_editable` 同源，杜絕定義漂移）。
                is_native = registry.is_native_owner(m.get("owned_by", ""))
                for key in allowed:
                    if key in settings:
                        if key in _sampling and not is_native:
                            continue
                        m[key] = settings[key]
                updated = True
                break
        if updated:
            self._save_config()
            return True
        return False

    def mark_model_seen(self, identity: AgentIdentity) -> None:
        """更新模型的最後見面時間，標記為在線 (Identity SSOT)。"""
        now_epoch = get_current_epoch()
        updated = False
        for m in self._models_cache:
            if m["id"] == identity.agent_id:
                m["last_seen"] = now_epoch
                updated = True
                break
        if updated:
            self._save_config()

    def delete_model(self, identity: AgentIdentity) -> bool:
        """從名冊中刪除模型 (Identity SSOT)；自訂模型的資料夾由路由先刪（ADR-0293 R11）。"""
        deleted_any = False
        model_id = identity.agent_id

        # 1. 嘗試從註冊表中移除 (Level 0)
        if agent_service.delete_agent_registration(identity):
            deleted_any = True
            
        # 2. 從基礎模型快取中移除
        original_len = len(self._models_cache)
        self._models_cache = [m for m in self._models_cache if m["id"] != model_id]
        
        if len(self._models_cache) < original_len:
            self._save_config()
            deleted_any = True
            
        return deleted_any

    def get_model_ids(self) -> list[str]:
        """僅回傳開啟且在線的模型供對話使用。"""
        return [
            m["id"] for m in self.get_models() 
            if m.get("enabled") and m.get("status") == "online"
        ]

# 移除了全域實體以避免單例衝突，請透過 src.services.models.model_manager 存取
