"""Heartbeat Proxy Client module."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

if __package__:
    from .config import settings
    from .errors import HeartbeatError
else:
    from config import settings
    from errors import HeartbeatError

if TYPE_CHECKING:
    from .token_provider import TokenProvider

logger = logging.getLogger("ToukaHeartbeat.ProxyClient")
_OUTBOUND_TIMEOUT_SECONDS = 10.0


class ProxyClient:
    """與 ToukaProxy 通訊的用戶端代理。"""

    def __init__(self, token_provider: TokenProvider, api_url: str | None = None) -> None:
        """初始化代理用戶端，設定 API 位址與認證憑證。"""
        self.api_url = api_url or settings.awaken_url
        self.token_provider = token_provider

    async def trigger_heartbeat(
        self,
        agent_id: str,
        pulse_count: int,
        pulse_slots: int | None,
        init_prompt: str | None = None,
        model_override: str | None = None,
        security_level: str = "safe",
        session_mode: str = "home",
        session_id: str = "home",
    ) -> int:
        """發送喚醒指令至 Proxy。"""
        try:
            credential = self.token_provider.resolve(require_shared=True)
        except HeartbeatError as error:
            logger.warning("Heartbeat outbound unavailable: %s", error.public_detail()["code"])
            return error.status_code
        final_prompt = init_prompt or "這是妳的「自主心跳時間」，請檢查是否有需要處理的事項？"

        payload = {
            "token": credential.value,
            "agent_id": agent_id,
            "pulse_count": pulse_count,
            "pulse_slots": pulse_slots,
            "init_prompt": final_prompt,
            "model_override": model_override,
            "security_level": security_level,
            "session_mode": session_mode,
            "session_id": session_id,
        }

        try:
            async with (
                asyncio.timeout(_OUTBOUND_TIMEOUT_SECONDS),
                httpx.AsyncClient(follow_redirects=False) as client,
            ):
                resp = await client.post(
                    self.api_url, json=payload, timeout=_OUTBOUND_TIMEOUT_SECONDS
                )
                logger.info(
                    "💓 [ProxyClient] Triggered %s (Pulses: %d). Status: %d",
                    agent_id,
                    pulse_count,
                    resp.status_code,
                )
                return resp.status_code
        except Exception:
            logger.warning("Heartbeat outbound failed; no retry performed")
            return 500
