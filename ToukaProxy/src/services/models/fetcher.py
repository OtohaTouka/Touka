"""模型動態抓取器 (Model Fetcher) 模組.

負責從各大模型提供者的 API 動態抓取可用模型清單。
"""

import logging
import re
from collections.abc import Callable  # noqa: TC003
from typing import Any, NoReturn

import httpx

from src.config import ANTHROPIC_API_BASE_URL, DEEPSEEK_BASE_URL, GEMINI_BASE_URL
from src.utils.ssrf_guard import validate_outbound_url
from src.utils.url_redaction import sanitized_url_origin

logger = logging.getLogger(__name__)


class DiscoveryError(Exception):
    """供應商模型發現失敗，帶分類碼供「不靜默失敗」回報。

    code: "auth"（401/403）| "not_found"（404）| "rate_limit"（429）| "http_<status>"
          | "network"（連線/逾時）| "ssrf"（網址未過守衛）| "parse"（回應解析失敗）。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _classify_status(status: int) -> str:
    """HTTP 狀態碼 → 發現失敗分類碼。"""
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limit"
    return f"http_{status}"


def _raise_discovery_http(status: int, label: str) -> NoReturn:
    """以分類碼拋出 DiscoveryError（抽成函式，避免 try 內裸 raise／TRY301）。"""
    raise DiscoveryError(_classify_status(status), f"{label} 模型清單 HTTP {status}")


async def fetch_gemini_models(
    api_key: str,
    blacklist_checker: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """從 Google Gemini API 抓取最新模型清單（api_key 由呼叫端經 registry.get_key 提供）."""
    if not api_key:
        return []
    try:
        # 禁令 23: 金鑰走 x-goog-api-key header（非 URL query），避免 httpx 例外/日誌
        # （含 request.url）洩漏金鑰；禁令 22: follow_redirects=False。
        url = f"{GEMINI_BASE_URL}/models"
        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.get(
                url, headers={"x-goog-api-key": api_key}, timeout=10.0
            )
            if resp.status_code == 200:
                data = resp.json()
                new_models = []
                for m in data.get("models", []):
                    model_id = m.get("name", "").replace("models/", "")
                    supported = m.get("supportedGenerationMethods", [])
                    is_gen = (
                        "generateContent" in supported
                        or "streamGenerateContent" in supported
                    )
                    match = re.search(r"gemini-(\d+(?:\.\d+)?)", model_id)
                    if match:
                        version = float(match.group(1))
                        is_recent = version >= 2.0
                    else:
                        is_recent = False
                    if (
                        is_gen
                        and is_recent
                        and not blacklist_checker(model_id)
                    ):
                        new_models.append(
                            {
                                "id": model_id,
                                "object": "model",
                                "owned_by": "google",
                            }
                        )
                return new_models
            _raise_discovery_http(resp.status_code, "Gemini")
    except DiscoveryError:
        raise
    except Exception as e:
        raise DiscoveryError("network", "Gemini 連線失敗") from e


async def fetch_openai_models(
    api_key: str,
    blacklist_checker: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """從 OpenAI API 抓取最新模型清單（api_key 由呼叫端提供）."""
    if not api_key:
        return []
    try:
        url = "https://api.openai.com/v1/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.get(url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                new_models = []
                for m in data.get("data", []):
                    model_id = m.get("id", "")
                    is_chat = any(
                        x in model_id for x in ["gpt-", "o1-", "o3-", "o4-"]
                    )
                    if is_chat and not blacklist_checker(model_id):
                        new_models.append(
                            {
                                "id": model_id,
                                "object": "model",
                                "owned_by": "openai",
                            }
                        )
                return new_models
            _raise_discovery_http(resp.status_code, "OpenAI")
    except DiscoveryError:
        raise
    except Exception as e:
        raise DiscoveryError("network", "OpenAI 連線失敗") from e


async def fetch_grok_models(
    api_key: str,
    blacklist_checker: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """從 xAI API 抓取最新 Grok 模型清單（api_key 由呼叫端提供，已含 GROK/XAI 後備）."""
    if not api_key:
        return []
    try:
        url = "https://api.x.ai/v1/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.get(url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                new_models = []
                for m in data.get("data", []):
                    model_id = m.get("id", "")
                    if not blacklist_checker(model_id):
                        new_models.append(
                            {
                                "id": model_id,
                                "object": "model",
                                "owned_by": "xai",
                            }
                        )
                return new_models
            _raise_discovery_http(resp.status_code, "Grok")
    except DiscoveryError:
        raise
    except Exception as e:
        raise DiscoveryError("network", "Grok 連線失敗") from e


async def fetch_anthropic_models(
    api_key: str,
    blacklist_checker: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """從 Anthropic API 抓取最新模型清單（api_key 由呼叫端提供）."""
    if not api_key:
        return []
    try:
        url = f"{ANTHROPIC_API_BASE_URL}/v1/models"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.get(url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                new_models = []
                for m in data.get("data", []):
                    model_id = m.get("id", "")
                    if not blacklist_checker(model_id):
                        new_models.append(
                            {
                                "id": model_id,
                                "object": "model",
                                "owned_by": "anthropic",
                            }
                        )
                return new_models
            _raise_discovery_http(resp.status_code, "Anthropic")
    except DiscoveryError:
        raise
    except Exception as e:
        raise DiscoveryError("network", "Anthropic 連線失敗") from e


async def fetch_deepseek_models(
    api_key: str,
    blacklist_checker: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """從 DeepSeek API（OpenAI 相容）抓取模型清單。

    回傳純粹的 {id, object, owned_by} 條目（不帶 _source_url/_api_key），與其他官方供應商
    的發現結果同形——故路由仍走 get_provider_info（DEEPSEEK_BASE_URL），不觸發連線覆寫。
    api_key 由呼叫端經 registry.get_key 提供。
    """
    if not api_key:
        return []
    try:
        url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.get(url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                return [
                    {
                        "id": m.get("id", ""),
                        "object": "model",
                        "owned_by": "deepseek",
                    }
                    for m in data.get("data", [])
                    if m.get("id") and not blacklist_checker(m.get("id", ""))
                ]
            _raise_discovery_http(resp.status_code, "DeepSeek")
    except DiscoveryError:
        raise
    except Exception as e:
        raise DiscoveryError("network", "DeepSeek 連線失敗") from e


async def fetch_openai_compatible_models(
    base_url: str,
    api_key: str,
    provider: str,
    blacklist_checker: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """通用的 OpenAI 相容格式模型抓取器，針對 Ollama 有額外優化.

    SSRF 縱深防禦：連線網址在儲存時已驗證，此處再驗一次（防手改 connections.json 或舊資料），
    不安全則 raise UnsafeUrlError，由呼叫端（fetch_custom_connections_models）捕捉並沙化回報。
    """
    validate_outbound_url(base_url)
    clean_url = base_url.rstrip("/")
    log_origin = sanitized_url_origin(base_url)

    urls_to_try: list[str] = []
    if any(x in clean_url for x in ["/models", "/api/tags"]):
        urls_to_try.append(clean_url)

    urls_to_try.extend(
        [
            f"{clean_url}/v1/models",
            f"{clean_url}/models",
        ]
    )
    if provider.lower() == "ollama":
        urls_to_try.append(f"{clean_url}/api/tags")

    headers = {}
    if api_key and api_key.lower() not in ["ollama", "none", ""]:
        headers["Authorization"] = f"Bearer {api_key}"

    # N8 fix: disable redirect following to prevent open-redirect SSRF chains.
    async with httpx.AsyncClient(follow_redirects=False) as client:
        for fetch_url in urls_to_try:
            try:
                logger.info("📡 [Fetcher] Attempting model discovery for %s", log_origin)
                resp = await client.get(
                    fetch_url, headers=headers, timeout=5.0
                )
                if resp.status_code == 200:
                    data = resp.json()
                    new_models = _parse_model_list(
                        data, provider, blacklist_checker
                    )
                    if new_models:
                        logger.info(
                            "✅ Fetched %d models from %s",
                            len(new_models),
                            log_origin,
                        )
                        return new_models
            except Exception:
                logger.debug(
                    "Fetch attempt failed for %s, trying next.",
                    log_origin,
                )
                continue

    logger.warning(
        "❌ All model fetch attempts failed for %s", log_origin
    )
    return []


def _parse_model_list(
    data: Any,
    provider: str,
    blacklist_checker: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """解析多種格式的模型回應 (OpenAI / Ollama / 陣列)."""
    model_list: list[Any] = []
    if isinstance(data, dict):
        if "data" in data:
            model_list = data["data"]
        elif "models" in data:
            model_list = data["models"]
    elif isinstance(data, list):
        model_list = data

    new_models: list[dict[str, Any]] = []
    for m in model_list:
        mid = (
            m.get("id")
            or m.get("name")
            or (m if isinstance(m, str) else None)
        )
        if mid and not blacklist_checker(str(mid)):
            display_id = str(mid)
            if (
                provider.lower() == "ollama"
                and "/" not in display_id
                and ":" in display_id
            ):
                display_id = f"ollama/{display_id}"

            new_models.append(
                {
                    "id": display_id,
                    "object": "model",
                    "owned_by": provider.lower(),
                }
            )
    return new_models
