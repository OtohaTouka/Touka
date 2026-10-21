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
