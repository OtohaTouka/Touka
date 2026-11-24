"""CSRF Origin Guard (M1 fix).

Provides a FastAPI dependency that validates the HTTP Origin header on
state-changing requests. If the Origin is present and not in the trusted
set, the request is rejected as a potential cross-site request forgery.

Browser security: non-browser callers (curl, API clients) do not send the
Origin header and are therefore not affected by this check.
"""

from __future__ import annotations

import logging
from typing import Any

from src.config import UI_DEFAULT_URL
from src.core.exceptions import ErrorCode, ToukaException

logger = logging.getLogger(__name__)

# Trusted origins (same-site browser requests from ToukaChat + localhost dev).
# The set is built at module load time from the configured UI URL.
_TRUSTED_ORIGINS: frozenset[str] = frozenset(
    filter(None, [
        "http://localhost:3001",
        "http://localhost:3000",
        "http://127.0.0.1:3001",
        UI_DEFAULT_URL.rstrip("/") if UI_DEFAULT_URL else None,
    ])
)


def verify_origin(request: Any) -> None:
    """Validate the HTTP Origin header to prevent CSRF attacks.

    Args:
        request: The incoming FastAPI Request object.

    Raises:
        ToukaException(AUTH_PERMISSION_DENIED): When the Origin header is
            present and does not match a trusted origin.
    """
    # Access via dict-like interface for both real Request and mock objects
    try:
        origin = request.headers.get("origin") or request.headers.get("Origin")
    except Exception:
        origin = None

    if not origin:
        # No Origin header — direct API call (not from a browser cross-origin request)
        return

    if origin not in _TRUSTED_ORIGINS:
        logger.warning(
            "🚫 [CSRF] Blocked request from untrusted origin: %s (trusted: %s)",
            origin,
            _TRUSTED_ORIGINS,
        )
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)


def require_trusted_origin(request: Any) -> None:
    """比 ``verify_origin`` 嚴：一定要帶 Origin，而且是信任的來源（ADR-0320）。

    給從設定頁發動、會讓本機小幫手跳視窗的端點，以及舊的 ``/terminal/approve|reject``
    （2026-11-24）用。不是瀏覽器的程式可以自己帶 Origin，
    所以這只是多一層防護；真正的關卡是人類管理員身分加上比對配對的人。
    """
    try:
        origin = request.headers.get("origin") or request.headers.get("Origin")
    except Exception:
        origin = None
    if not origin or origin not in _TRUSTED_ORIGINS:
        logger.warning("🚫 [CSRF] Missing or untrusted origin on a strict endpoint: %s", origin)
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
