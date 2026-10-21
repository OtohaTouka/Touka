"""供應商註冊表 (Provider Registry) —— 「系統認得哪些 AI 供應商」的單一事實來源 (SSOT)。

過去這份事實散落多處硬編碼且彼此不一致：
  - 路由：`get_provider_info` 的 if 鏈（grok/ollama/litellm/deepseek/openai）。
  - 動態發現：`refresh_all_dynamic_models` 的 `asyncio.gather` 清單，且 `main.py` 啟動時
    又複製一份。
  - 健康檢查：`get_models` 內 `owned_by in (google, anthropic, xai, openai)` 白名單。
新增一家供應商得同時改好幾處，漏一處就「能路由不能發現」或反之（DeepSeek 即一例）。

本模組把這份事實收斂成一份宣告式 `PROVIDER_SPECS`；上述三處全部從此衍生。新增供應商＝
新增一筆 `ProviderSpec`（＋若是新金鑰，`config.py` 加一個 Settings 欄位）。

安全：
  - 金鑰一律經 `get_key()` 讀取（單一稽核 chokepoint）——金鑰永不寫死於此、不進回傳的
    URL、不進 log。目前讀自 `src.config` 模組常數（與重構前等價）；未來（Phase 3）只需改
    `get_key` 內部即可改為即時讀取，免動其餘呼叫端。
  - `base_url` 取自 config（官方文件標準值），非由請求方動態指定。
"""

from __future__ import annotations

from dataclasses import dataclass

import src.config as _config


@dataclass(frozen=True)
class ProviderSpec:
    """單一 AI 供應商的宣告式描述。"""

    id: str
    """內部識別（亦為 connections provider 鍵的小寫值）。"""
    label: str
    """路由回傳的供應商顯示名（"OpenAI" / "DeepSeek" / "Grok"…）。"""
    owned_by: str
    """模型清單 `owned_by` 標籤（健康檢查與分組用）。"""

    # --- 金鑰解析 ---
    key_attrs: tuple[str, ...] = ()
    """`src.config` 中的金鑰常數名，依序取第一個非空（如 ("GROK_API_KEY","XAI_API_KEY")）。"""
    literal_key: str | None = None
    """固定金鑰（如 Ollama 用字面 "ollama"）；設定時優先於 key_attrs。"""

    # --- 路由（get_provider_info）---
    route_keywords: tuple[str, ...] = ()
    """模型名含任一關鍵字即路由到此供應商；空＝不經 get_provider_info（由原生協定處理器負責）。"""
    route_url_attr: str | None = None
    """路由回傳 URL 取自 `src.config` 此常數名（Grok 為完整 chat 端點、其餘為 base，保形）。"""

    # --- 動態發現（discovery）---
    discoverable: bool = False
    """是否參與金鑰驅動的動態發現（Ollama/LiteLLM 經連線發現，故為 False）。"""
    discovery_format: str | None = None
    """發現格式 → 對應 fetcher："gemini"/"openai"/"grok"/"anthropic"/"deepseek"。"""

    # --- 取樣參數能力（ADR-0111 Phase 2：ModelsTab provider-aware UI 衍生此處）---
    supports_top_k: bool = False
    """此供應商的路由協定是否接受 top_k。Anthropic 原生協定與 Gemini generationConfig 支援；
    OpenAI 相容路徑（openai_handler）會 pop 掉 top_k（→ 400），故 OpenAI/Grok/DeepSeek/
    Ollama/LiteLLM 皆為 False。"""
    temperature_max: float = 2.0
    """temperature 合法上限。Anthropic 為 1.0（逾此會 400，handler 亦會夾），其餘 2.0。"""


# 順序即路由優先序（具體先於泛用：gpt/openai 放最後）。
# gemini/anthropic 無 route_keywords——其路由由 factory 的原生協定處理器負責，此處僅供
# 動態發現與健康檢查使用。
PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="grok",
        label="Grok",
        owned_by="xai",
        key_attrs=("GROK_API_KEY", "XAI_API_KEY"),
        route_keywords=("grok", "xai"),
        route_url_attr="GROK_DEFAULT_URL",
        discoverable=True,
        discovery_format="grok",
    ),
    ProviderSpec(
        id="ollama",
        label="Ollama",
        owned_by="ollama",
        literal_key="ollama",
        route_keywords=("ollama",),
        route_url_attr="OLLAMA_BASE_URL",
    ),
    ProviderSpec(
        id="litellm",
        label="LiteLLM",
        owned_by="litellm",
        key_attrs=("LITELLM_API_KEY",),
        route_keywords=("litellm",),
        route_url_attr="LITELLM_BASE_URL",
    ),
    ProviderSpec(
        id="deepseek",
        label="DeepSeek",
        owned_by="deepseek",
        key_attrs=("DEEPSEEK_API_KEY",),
        route_keywords=("deepseek",),
        route_url_attr="DEEPSEEK_BASE_URL",
        discoverable=True,
        discovery_format="deepseek",
    ),
    ProviderSpec(
        id="openai",
        label="OpenAI",
        owned_by="openai",
        key_attrs=("OPENAI_API_KEY",),
        route_keywords=("gpt", "openai"),
        route_url_attr="OPENAI_BASE_URL",
        discoverable=True,
        discovery_format="openai",
    ),
    ProviderSpec(
        id="gemini",
        label="Gemini",
        owned_by="google",
        key_attrs=("GEMINI_API_KEY",),
        discoverable=True,
        discovery_format="gemini",
        supports_top_k=True,  # generationConfig.topK
    ),
    ProviderSpec(
        id="anthropic",
        label="Anthropic",
        owned_by="anthropic",
        key_attrs=("ANTHROPIC_API_KEY",),
        discoverable=True,
        discovery_format="anthropic",
        supports_top_k=True,  # 原生協定支援
        temperature_max=1.0,  # Anthropic temperature 上限
    ),
)

# owned_by → spec（owned_by 在 SPECS 中唯一）。
_BY_OWNED_BY: dict[str, ProviderSpec] = {s.owned_by: s for s in PROVIDER_SPECS}
# id → spec。
_BY_ID: dict[str, ProviderSpec] = {s.id: s for s in PROVIDER_SPECS}


def get_key(spec: ProviderSpec) -> str:
    """取得供應商金鑰（單一稽核 chokepoint）。

    固定金鑰優先；否則依序取第一個非空的 config 常數，回傳已去除前後空白的字串；皆無則
    回空字串（與重構前 `(KEY or "").strip()` / `KEY or ""` 等價）。以 `getattr` 即時讀取，
    未來（Phase 3）改為免重啟的即時來源時只需動此處。
    """
    if spec.literal_key is not None:
        return spec.literal_key
    for attr in spec.key_attrs:
        val = getattr(_config, attr, None)
        if val:
            return str(val).strip()
    return ""


def get_key_by_id(provider_id: str) -> str:
    """以 provider id 取即時金鑰（SSOT 便捷入口）；找不到 spec 回空字串。

    供呼叫端（如 dispatcher factory 解析 gemini/anthropic 金鑰）以即時來源取金鑰，
    取代「進程啟動時快照」的 from-import 綁定或 baked-key client（免重啟即時生效）。
    """
    spec = _BY_ID.get(provider_id)
    return get_key(spec) if spec else ""


def get_route_url(spec: ProviderSpec) -> str:
    """取得路由回傳的 URL（取自 config 常數，保持各供應商原本的 URL 形狀）。"""
    if spec.route_url_attr:
        return getattr(_config, spec.route_url_attr, "") or ""
    return ""


def normalize_chat_url(url: str) -> str:
    """把供應商 base／route URL 正規化為「OpenAI 相容 chat completions 端點」(SSOT)。

    各供應商在 config 的 URL 形狀不一：Grok 為完整端點、DeepSeek/OpenAI 為 ``.../v1`` 基底、
    LiteLLM/Ollama 為裸 host。handler 直接 POST 到此 URL，故必須補成可直接打的 chat 端點，
    否則會「打到基底」（少 /chat/completions）或「重複 /v1」。

    規則：已是完整端點（含 ``chat/completions`` 或 Ollama 的 ``api/chat``）→ 原樣回；
    以 ``/v1`` 結尾 → 補 ``/chat/completions``；其餘（裸 host）→ 補 ``/v1/chat/completions``。
    """
    u = (url or "").rstrip("/")
    if not u:
        return u
    if "chat/completions" in u or "api/chat" in u:
        return u
    if u.endswith("/v1"):
        return u + "/chat/completions"
    return u + "/v1/chat/completions"


def chat_completions_url(spec: ProviderSpec) -> str:
    """供應商的完整 chat completions 端點（SSOT）：route URL 經 normalize_chat_url 正規化。

    修正：過去路由直接回 ``get_route_url``（DeepSeek/OpenAI 為 ``.../v1`` 基底），handler 便
    POST 到基底而非 chat 端點 → 供應商回 API Error。此函式統一補出正確端點。
    """
    return normalize_chat_url(get_route_url(spec))


def resolve_provider(model_name: str) -> ProviderSpec | None:
    """依模型名關鍵字找出路由供應商（具體先於泛用，依 PROVIDER_SPECS 順序）。

    無 route_keywords 的供應商（gemini/anthropic）不參與；找不到回 None。
    """
    m = (model_name or "").lower()
    for spec in PROVIDER_SPECS:
        if spec.route_keywords and any(kw in m for kw in spec.route_keywords):
            return spec
    return None


def spec_by_owned_by(owned_by: str) -> ProviderSpec | None:
    """以 owned_by 標籤查供應商。"""
    return _BY_OWNED_BY.get(owned_by)


def is_native_owner(owned_by: str) -> bool:
    """此 owned_by 是否為已知原生供應商（有 ProviderSpec）。

    ADR-0111：取樣參數「該注入／可持久化／可編輯」的**單一判準（SSOT）**——ModelsTab UI
    （`model_param_caps`）、`NativeModelLayer` 注入、`patch_model_settings` 持久化三處共用，
    杜絕 allowlist／blocklist 兩套定義漂移。custom/proxy/touka（自訂模型／代理／靈魂）及
    未知或推斷的 owned_by（openrouter/cohere/unknown…）皆無 spec → 非原生（其參數另由
    ModelStudio/SoulStudio 管，或因協定能力未知而不適用）。
    """
    return owned_by in _BY_OWNED_BY


def model_param_caps(owned_by: str) -> tuple[bool, bool, float]:
    """ADR-0111：某 owned_by 的取樣參數能力 → (可編輯, 支援 top_k, temperature 上限)。

    「可編輯」＝原生模型（見 `is_native_owner`）。供 ModelsTab provider-aware UI 衍生：
    依此隱藏不支援的 top_k、夾住 Anthropic 的 temperature 上限。
    """
    spec = _BY_OWNED_BY.get(owned_by)
    if spec is None:
        return (False, False, 2.0)
    return (True, spec.supports_top_k, spec.temperature_max)


def spec_by_id(provider_id: str) -> ProviderSpec | None:
    """以 id 查供應商。"""
    return _BY_ID.get(provider_id)


def discoverable_specs() -> tuple[ProviderSpec, ...]:
    """金鑰驅動、可動態發現的供應商（gemini/openai/grok/anthropic/deepseek）。"""
    return tuple(s for s in PROVIDER_SPECS if s.discoverable)


# 已知但「非模型供應商」的金鑰（不應被「未知金鑰」稽核誤報）。Tavily 為網路搜尋金鑰。
_NON_PROVIDER_KEY_NAMES = frozenset({"TAVILY_API_KEY"})


def known_provider_key_names() -> set[str]:
    """回傳 registry 中所有供應商金鑰的環境變數名集合。"""
    return {attr for spec in PROVIDER_SPECS for attr in spec.key_attrs}


def provider_key_status() -> dict[str, bool]:
    """可發現供應商的金鑰設定狀態：{id: 是否已設定}。"""
    return {spec.id: bool(get_key(spec)) for spec in discoverable_specs()}


def unknown_provider_key_names(present_names: object) -> list[str]:
    """從一組已存在的環境變數名中，挑出「*_API_KEY 但非任何已知供應商/搜尋金鑰」者。

    用於「不靜默失敗」稽核：使用者設了 Touka 不認得的金鑰（如 FOOBAR_API_KEY）時回報其
    鍵名（**僅鍵名、不含值**），而非無聲忽略。
    """
    known = known_provider_key_names() | _NON_PROVIDER_KEY_NAMES
    return sorted(
        name
        for name in set(present_names)  # type: ignore[arg-type]
        if name.endswith("_API_KEY") and name not in known
    )


def health_key_missing(owned_by: str) -> bool:
    """此 owned_by 是否為金鑰驅動官方供應商、且金鑰缺失（→ 模型應強制離線）。

    僅涵蓋 discoverable 且有 key_attrs 的官方供應商（google/openai/xai/anthropic/deepseek）；
    Ollama/LiteLLM 等端點型供應商不在此列（與重構前白名單一致，並擴及 DeepSeek）。
    """
    spec = spec_by_owned_by(owned_by)
    return bool(spec and spec.discoverable and spec.key_attrs and not get_key(spec))
