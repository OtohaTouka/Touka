"""協議解析與連線（ADR-0311）：未知欄位拒絕、只連本機、不跟隨轉址。"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import pytest
from touka_local.connection import (
    POLL_TIMEOUT_SECONDS,
    AuthLost,
    ConnectionLost,
    NoRedirect,
    ProtocolMismatch,
    ToukaClient,
    make_opener,
)
from touka_local.errors import Refused
from touka_local.protocol import (
    MAX_ERROR_MESSAGE_CHARS,
    MAX_JOB_RESULT_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    parse_poll_response,
    poll_request,
    result_payload,
)

TOKEN = "a" * 43


def _job(**changes: Any) -> dict[str, Any]:
    job = {
        "id": "abc123",
        "action": "read",
        "args": {"path": "Open/a.txt"},
        "requester": "soul",
        "decide_within": 30.0,
    }
    job.update(changes)
    return job


def test_parse_empty_response() -> None:
    parsed = parse_poll_response({"job": None, "cancelled": [], "host_mount_roots": []})
    assert parsed.job is None and parsed.cancelled == () and parsed.host_mount_roots == ()


def test_parse_job() -> None:
    parsed = parse_poll_response(
        {"job": _job(), "cancelled": ["x"], "host_mount_roots": ["D:/Work"]}
    )
    assert parsed.job is not None
    assert parsed.job.id == "abc123"
    assert parsed.job.action == "read"
    assert parsed.job.args == {"path": "Open/a.txt"}
    assert parsed.cancelled == ("x",)
    assert parsed.host_mount_roots == ("D:/Work",)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"job": None, "cancelled": []},
        {"job": None, "cancelled": [], "host_mount_roots": [], "extra": 1},
        {"job": None, "cancelled": "x", "host_mount_roots": []},
        {"job": None, "cancelled": [1], "host_mount_roots": []},
        {"job": None, "cancelled": [], "host_mount_roots": [1]},
        {"job": _job(extra=1), "cancelled": [], "host_mount_roots": []},
        {"job": _job(action="shell"), "cancelled": [], "host_mount_roots": []},
        {"job": _job(args=[]), "cancelled": [], "host_mount_roots": []},
        {"job": _job(id="../x"), "cancelled": [], "host_mount_roots": []},
        {"job": _job(id=""), "cancelled": [], "host_mount_roots": []},
        {"job": _job(requester=1), "cancelled": [], "host_mount_roots": []},
        {"job": _job(decide_within=0), "cancelled": [], "host_mount_roots": []},
        {"job": _job(decide_within=-1), "cancelled": [], "host_mount_roots": []},
        {"job": _job(decide_within=True), "cancelled": [], "host_mount_roots": []},
        {"job": _job(decide_within=float("nan")), "cancelled": [], "host_mount_roots": []},
        {"job": _job(decide_within=float("inf")), "cancelled": [], "host_mount_roots": []},
        {"job": _job(decide_within="30"), "cancelled": [], "host_mount_roots": []},
    ],
)
def test_parse_refuses_unexpected_shapes(payload: object) -> None:
    with pytest.raises(ProtocolError):
        parse_poll_response(payload)


def test_poll_request_shape() -> None:
    assert poll_request("0.1.0") == {"protocol": PROTOCOL_VERSION, "helper_version": "0.1.0"}


def test_result_payload_keeps_only_known_fields() -> None:
    assert result_payload("ok", data={"a": 1}) == {
        "status": "ok",
        "code": None,
        "message": None,
        "data": {"a": 1},
    }


def test_result_payload_trims_long_text() -> None:
    payload = result_payload("refused", code="x" * 100, message="y" * (MAX_ERROR_MESSAGE_CHARS + 5))
    assert len(payload["code"]) == 64
    assert len(payload["message"]) == MAX_ERROR_MESSAGE_CHARS


def test_result_payload_replaces_oversized_data() -> None:
    payload = result_payload("ok", data={"text": "x" * (MAX_JOB_RESULT_BYTES + 1)})
    assert payload["status"] == "error"
    assert payload["code"] == "result_too_large"
    assert payload["data"] is None


# --- 連線 ---


@dataclass
class _Response:
    status: int = 200
    body: bytes = b"{}"

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]

    def close(self) -> None:
        return None

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@dataclass
class _Opener:
    outcome: object = field(default_factory=_Response)
    calls: list[tuple[urllib.request.Request, float]] = field(default_factory=list)

    def open(self, request: urllib.request.Request, timeout: float) -> object:
        self.calls.append((request, timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "x", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]


def _client(opener: _Opener, port: int = 8000) -> ToukaClient:
    return ToukaClient(port=port, token=TOKEN, helper_version="0.1.0", opener=opener)


def _poll_body() -> bytes:
    return json.dumps({"job": None, "cancelled": [], "host_mount_roots": []}).encode()


def test_poll_sends_to_loopback_with_token() -> None:
    opener = _Opener(_Response(body=_poll_body()))
    parsed = _client(opener, port=8123).poll()
    request, timeout = opener.calls[0]
    assert request.full_url == "http://127.0.0.1:8123/v1/local-bridge/poll"
    assert request.get_method() == "POST"
    assert request.get_header("X-touka-local-token") == TOKEN
    assert json.loads(request.data) == {"protocol": PROTOCOL_VERSION, "helper_version": "0.1.0"}
    assert timeout == POLL_TIMEOUT_SECONDS
    assert parsed.job is None


@pytest.mark.parametrize("port", [0, -1, 65536, "8000", True, None])
def test_bad_port_is_refused(port: object) -> None:
    with pytest.raises(Refused) as caught:
        ToukaClient(port=port, token=TOKEN, helper_version="0.1.0", opener=_Opener())  # type: ignore[arg-type]
    assert caught.value.code == "bad_port"


@pytest.mark.parametrize("token", ["", "short", "a" * 42 + "\r", "a b" + "c" * 40, None, "é" * 43])
def test_bad_token_is_refused(token: object) -> None:
    with pytest.raises(Refused) as caught:
        ToukaClient(port=8000, token=token, helper_version="0.1.0", opener=_Opener())  # type: ignore[arg-type]
    assert caught.value.code == "bad_token"


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (_http_error(401), AuthLost),
        (_http_error(409), ProtocolMismatch),
        (_http_error(500), ConnectionLost),
        (_http_error(302), ConnectionLost),
        (urllib.error.URLError("refused"), ConnectionLost),
        (TimeoutError(), ConnectionLost),
        (OSError("reset"), ConnectionLost),
    ],
)
def test_poll_failures(outcome: Exception, error: type[Exception]) -> None:
    with pytest.raises(error):
        _client(_Opener(outcome)).poll()


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"job": null}'])
def test_poll_bad_body(body: bytes) -> None:
    with pytest.raises(ProtocolError):
        _client(_Opener(_Response(body=body))).poll()


def test_poll_refuses_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("touka_local.connection.MAX_RESPONSE_BYTES", 4)
    with pytest.raises(ProtocolError):
        _client(_Opener(_Response(body=_poll_body()))).poll()


def test_confirm_only_true_on_200() -> None:
    opener = _Opener(_Response())
    assert _client(opener).confirm("job1") is True
    assert opener.calls[0][0].full_url == "http://127.0.0.1:8000/v1/local-bridge/jobs/job1/confirm"


@pytest.mark.parametrize(
    "outcome",
    [
        _Response(status=204),
        _http_error(410),
        _http_error(401),
        _http_error(500),
        urllib.error.URLError("x"),
        TimeoutError(),
    ],
)
def test_confirm_false_on_anything_else(outcome: object) -> None:
    assert _client(_Opener(outcome)).confirm("job1") is False


def test_result_posts_payload() -> None:
    opener = _Opener(_Response())
    payload = result_payload("ok", data={"a": 1})
    assert _client(opener).result("job1", payload) is True
    request = opener.calls[0][0]
    assert request.full_url == "http://127.0.0.1:8000/v1/local-bridge/jobs/job1/result"
    assert json.loads(request.data) == payload


@pytest.mark.parametrize(
    "outcome", [_http_error(410), _http_error(413), urllib.error.URLError("x")]
)
def test_result_false_when_not_delivered(outcome: object) -> None:
    assert _client(_Opener(outcome)).result("job1", result_payload("ok")) is False


def test_job_id_cannot_change_the_url() -> None:
    opener = _Opener(_Response())
    assert _client(opener).confirm("../../evil") is False
    assert opener.calls == []


def test_opener_ignores_system_proxies_and_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:1")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:1")
    opener = make_opener()
    assert [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)] == []
    assert [h for h in opener.handlers if isinstance(h, NoRedirect)]
    plain = [h for h in opener.handlers if type(h) is urllib.request.HTTPRedirectHandler]
    assert plain == []


def test_no_redirect_refuses_to_build_a_request() -> None:
    request = urllib.request.Request("http://127.0.0.1:8000/x")
    assert NoRedirect().redirect_request(request, None, 302, "x", {}, "http://evil/") is None  # type: ignore[arg-type]
