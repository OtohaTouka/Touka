"""Request body size limit middleware (P19 fix)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds MAX_REQUEST_BODY_BYTES."""

    def __init__(self, app, max_bytes: int) -> None:
        """Initialise with maximum allowed body size in bytes."""
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Enforce body size limit before passing to the endpoint."""
        content_length = request.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"error": "Request body too large", "code": 4001},
                    )
            except ValueError:
                pass
        return await call_next(request)
