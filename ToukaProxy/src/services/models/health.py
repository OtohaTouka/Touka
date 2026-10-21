"""模型健康檢查 (Model Health Validator) 模組.

負責對快取中的模型進行非同步健康檢查，回傳各模型的布林狀態。
"""

import asyncio
import logging
from typing import Any

import httpx

from src import config

logger = logging.getLogger(__name__)


async def validate_model_health(
    models: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[bool]]:
    """非同步檢查模型清單中每個模型的健康狀態.

    Returns:
        (原始模型清單, 各模型對應的健康布林值清單)
    """
    if not models:
        return [], []

    semaphore = asyncio.Semaphore(5)

    async def check_single(model: dict[str, Any]) -> bool:
        model_id = model.get("id")
        owned_by = model.get("owned_by")
        if owned_by in ("touka", "proxy"):
            return True
        async with semaphore:
            try:
                if owned_by == "google":
                    api_key = config.GEMINI_API_KEY
                    if not api_key or not api_key.strip():
                        return False
                    # 禁令 23: 金鑰走 x-goog-api-key header（非 URL query），避免
                    # httpx 例外/日誌洩漏；禁令 22: follow_redirects=False。
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}"
                    async with httpx.AsyncClient(follow_redirects=False) as client:
                        return (
                            await client.get(
                                url,
                                headers={"x-goog-api-key": api_key},
                                timeout=5.0,
                            )
                        ).status_code == 200
                if owned_by == "xai":
                    api_key = (config.GROK_API_KEY or config.XAI_API_KEY or "").strip()
                    if not api_key:
                        return False
                    url = f"https://api.x.ai/v1/models/{model_id}"
                    headers = {"Authorization": f"Bearer {api_key}"}
                    async with httpx.AsyncClient(follow_redirects=False) as client:
                        return (
                            await client.get(url, headers=headers, timeout=5.0)
                        ).status_code == 200
                if owned_by == "anthropic":
                    api_key = config.ANTHROPIC_API_KEY
                    return bool(api_key)
            except Exception:
                return False
            else:
                return False

    tasks = [check_single(m) for m in models]
    results = await asyncio.gather(*tasks)

    return models, list(results)
