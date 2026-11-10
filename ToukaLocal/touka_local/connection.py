"""連回 Touka（ADR-0311）：只連本機，不走系統代理，不跟隨轉址。"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Final, Protocol

from touka_local.errors import Refused
from touka_local.protocol import (
    JOB_ID,
    ProtocolError,
    parse_poll_response,
    poll_request,
)

if TYPE_CHECKING:
    from touka_local.protocol import (
        PollResponse,
    )

TOUKA_HOST: Final = "127.0.0.1"
POLL_TIMEOUT_SECONDS: Final = 15.0
REQUEST_TIMEOUT_SECONDS: Final = 10.0
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024

_BASE_PATH: Final = "/v1/local-bridge"
_AUTH_HEADER: Final = "X-Touka-Local-Token"
_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]{20,128}")
_HTTP_OK: Final = 200
_HTTP_UNAUTHORIZED: Final = 401
_HTTP_CONFLICT: Final = 409
_CONNECTION_FAILURES: Final = (OSError, http.client.HTTPException)


class AuthLost(Exception):
    """配對失效了。"""


class ProtocolMismatch(Exception):
    """Touka 和小幫手的協議版本不同。"""


class ConnectionLost(Exception):
    """連不上 Touka。"""


class Opener(Protocol):
    """``urllib`` 的 opener，測試換成假的。"""

    def open(self, request: urllib.request.Request, timeout: float) -> Any:
        """送出請求。"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟隨轉址，權杖不會被帶到別的地方。"""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        """回傳 None 讓 urllib 把 3xx 當錯誤。"""
        return


def make_opener() -> urllib.request.OpenerDirector:
    """建立不走系統代理、不跟隨轉址的 opener。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def check_port(port: object) -> int:
    """連接埠要是 1 到 65535 的整數。"""
    if type(port) is not int or not 1 <= port <= 65535:
        raise Refused("bad_port")
    return port


class ToukaClient:
    """呼叫 Touka 的三個端點。"""

    def __init__(
        self,
        *,
        port: int,
        token: str,
        helper_version: str,
        opener: Opener | None = None,
    ) -> None:
        self._base = f"http://{TOUKA_HOST}:{check_port(port)}{_BASE_PATH}"
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            raise Refused("bad_token")
        self._token = token
        self._helper_version = helper_version
        self._opener = opener if opener is not None else make_opener()

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"{self._base}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", _AUTH_HEADER: self._token},
            method="POST",
        )
        with self._opener.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)

    def poll(self) -> PollResponse:
        """拿下一件工作。"""
        try:
            status, body = self._post(
                "/poll", poll_request(self._helper_version), POLL_TIMEOUT_SECONDS
            )
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == _HTTP_UNAUTHORIZED:
                raise AuthLost from exc
            if exc.code == _HTTP_CONFLICT:
                raise ProtocolMismatch from exc
            raise ConnectionLost from exc
        except _CONNECTION_FAILURES as exc:
            raise ConnectionLost from exc
        if status != _HTTP_OK:
            raise ConnectionLost
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProtocolError
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise ProtocolError from exc
        return parse_poll_response(payload)

    def _accepted(self, path: str, payload: dict[str, Any]) -> bool:
        try:
            status, _body = self._post(path, payload, REQUEST_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            exc.close()
            return False
        except _CONNECTION_FAILURES:
            return False
        return status == _HTTP_OK

    def confirm(self, job_id: str) -> bool:
        """人按了核准之後問 Touka：工作還在嗎？只有明確的 200 才算還在。"""
        if JOB_ID.fullmatch(job_id) is None:
            return False
        return self._accepted(f"/jobs/{job_id}/confirm", {})

    def result(self, job_id: str, payload: dict[str, Any]) -> bool:
        """回報結果。"""
        if JOB_ID.fullmatch(job_id) is None:
            return False
        return self._accepted(f"/jobs/{job_id}/result", payload)
