"""自動化測試外部網路封鎖（tests/network_guard.py）的回歸測試。

純函式矩陣不碰 socket；裝上後的攔截測試只用 `.invalid`、TEST-NET-1（192.0.2.0/24）與
127.0.0.1，外部名稱一律在 DNS 解析前就被擋下。
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import threading
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from anthropic import APIConnectionError, AsyncAnthropic, DefaultAsyncHttpxClient
from src.config import ANTHROPIC_API_BASE_URL

from tests import conftest, network_guard

_LOCAL = "localhost LOCALHOST localhost. api.localhost 127.0.0.1 127.255.0.9 ::1 [::1] "
_LOCAL += "::ffff:127.0.0.1 0.0.0.0 ::"
_EXTERNAL = "api.anthropic.com 192.0.2.1 10.0.0.1 ::ffff:192.0.2.1 2001:db8::1 127.1 . "
_EXTERNAL += "localhost.example.com notlocalhost .localhost 169.254.169.254 fe80::1"


@pytest.mark.parametrize("host", [None, "", b"", b"localhost", b"127.0.0.1", *_LOCAL.split()])
def test_local_destinations_are_allowed(host: object) -> None:
    assert network_guard.is_local_destination(host) is True


@pytest.mark.parametrize(
    "host", [b"api.anthropic.com", " localhost", b"\xff", ("127.0.0.1", 80), *_EXTERNAL.split()]
)
def test_external_or_ambiguous_destinations_are_blocked(host: object) -> None:
    assert network_guard.is_local_destination(host) is False


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        (("127.0.0.1", 8000), True),
        (("::1", 8000, 0, 0), True),
        (("192.0.2.1", 443), False),
        ((), False),
        ("/run/touka.sock", True),  # AF_UNIX path
        (b"\0touka", True),  # Linux abstract AF_UNIX（asyncio socketpair）
        (None, False),
    ],
)
def test_socket_address_classification(address: object, expected: bool) -> None:
    assert network_guard.is_local_socket_address(address) is expected


@pytest.fixture
def drained() -> list[str]:
    """收下本測試故意觸發的違規；護欄沒裝上時直接失敗，不讓測試真的去連線。"""
    if not network_guard.is_installed():
        pytest.fail("network guard is not installed; refusing to attempt real connections")
    collected: list[str] = []
    yield collected
    collected.extend(network_guard.drain_violations())


def _assert_recorded(drained: list[str], fragment: str) -> None:
    drained.extend(network_guard.drain_violations())
    assert any(fragment in item for item in drained), drained


def test_install_is_idempotent_and_conftest_installed_first(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("network_guard_fresh", network_guard.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    hooks: list[object] = []
    monkeypatch.setattr(fresh, "sys", SimpleNamespace(addaudithook=hooks.append))

    assert (fresh.install(), fresh.install()) == (True, False)
    assert hooks == [fresh._audit_hook]
    assert network_guard.is_installed() is True
    assert network_guard.install() is False


@pytest.mark.parametrize(
    ("call", "fragment"),
    [
        (lambda: socket.getaddrinfo(b"example.invalid", 443), "b'example.invalid'"),
        (lambda: socket.gethostbyname("example.invalid"), "gethostbyname -> 'example"),
        (lambda: socket.gethostbyname_ex("example.invalid"), "gethostbyname -> 'example"),
        (lambda: socket.gethostbyaddr("192.0.2.1"), "gethostbyaddr"),
        (lambda: socket.create_connection(("192.0.2.1", 443), timeout=1), "'192.0.2.1'"),
        (lambda: socket.socket().connect(("192.0.2.1", 443)), "socket.connect"),
        (lambda: socket.socket(type=socket.SOCK_DGRAM).sendto(b"x", ("192.0.2.1", 53)), "sendto"),
    ],
)
def test_sync_resolution_and_connect_are_blocked(call, fragment: str, drained) -> None:
    with pytest.raises(network_guard.ExternalNetworkBlocked):
        call()
    _assert_recorded(drained, fragment)


@pytest.mark.parametrize("client_module", [httpx, httpx2])
async def test_http_clients_cannot_reach_provider(client_module, drained) -> None:
    async with client_module.AsyncClient(follow_redirects=False) as client:
        with pytest.raises(network_guard.ExternalNetworkBlocked):
            await client.get("https://api.anthropic.com/v1/models")
    with client_module.Client(follow_redirects=False) as client:  # noqa: SIM117
        with pytest.raises(network_guard.ExternalNetworkBlocked):
            client.get("https://api.anthropic.com/v1/models")
    _assert_recorded(drained, "'api.anthropic.com'")
    assert len(drained) == 2


def test_local_forward_proxy_cannot_tunnel_past_the_guard(monkeypatch, drained) -> None:
    with socket.create_server(("127.0.0.1", 0)) as proxy:
        for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.getsockname()[1]}")
        with httpx.Client(follow_redirects=False, timeout=1) as client:  # noqa: SIM117
            with pytest.raises(network_guard.ExternalNetworkBlocked):
                client.get("https://api.anthropic.com/v1/models")
        proxy.setblocking(False)
        with pytest.raises(BlockingIOError):
            proxy.accept()  # 假代理沒有收到任何連線
    _assert_recorded(drained, "'api.anthropic.com'")


async def test_sdk_that_wraps_errors_is_still_recorded(drained) -> None:
    client = AsyncAnthropic(
        api_key="sk-test-not-a-key", base_url=ANTHROPIC_API_BASE_URL, max_retries=0
    )
    with pytest.raises(Exception):  # noqa: B017 - SDK 會包成 APIConnectionError
        await client.models.list()
    await client.close()
    _assert_recorded(drained, "'api.anthropic.com'")


async def test_sdk_1x_httpx2_transport_with_injected_client_is_blocked(drained) -> None:
    """3a-U2：SDK 1.x 走 httpx2；照產品建構點注入 no-redirect client，送一次 messages.create。"""
    client = AsyncAnthropic(
        api_key="fake-key",
        base_url=ANTHROPIC_API_BASE_URL,
        max_retries=0,
        http_client=DefaultAsyncHttpxClient(follow_redirects=False),
    )
    assert isinstance(client._client, httpx2.AsyncClient)
    assert not isinstance(client._client, httpx.AsyncClient)
    with pytest.raises(APIConnectionError) as caught:
        await client.messages.create(
            model="claude-opus-5-5", max_tokens=16, messages=[{"role": "user", "content": "hi"}]
        )
    await client.close()
    chain, error = [], caught.value
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(item, network_guard.ExternalNetworkBlocked) for item in chain)
    _assert_recorded(drained, "'api.anthropic.com'")
    assert len(drained) == 1  # max_retries=0：只試一次


def test_violation_in_another_thread_is_recorded(drained) -> None:
    def worker() -> None:
        with pytest.raises(network_guard.ExternalNetworkBlocked):
            socket.getaddrinfo("thread.example.invalid", 443)

    thread = threading.Thread(target=worker, name="guard-probe-thread")
    thread.start()
    thread.join(timeout=10)
    _assert_recorded(drained, "'thread.example.invalid' (thread=guard-probe-thread)")


def test_loopback_servers_and_new_event_loops_still_work() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            peer, _ = server.accept()
            client.sendall(b"touka-echo")
            assert peer.recv(64) == b"touka-echo"
            peer.close()
        assert socket.getaddrinfo("localhost", port)

        async def open_and_close() -> None:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(open_and_close())
        finally:
            loop.close()
    assert network_guard.drain_violations() == []


def test_sentinel_fails_the_test_and_lists_blocked_destinations(drained) -> None:
    sentinel = conftest._external_network_sentinel.__wrapped__()
    next(sentinel)
    with pytest.raises(network_guard.ExternalNetworkBlocked):
        socket.getaddrinfo("sentinel.example.invalid", 443)

    with pytest.raises(pytest.fail.Exception, match=r"擋下[\s\S]*sentinel\.example\.invalid"):
        next(sentinel)
    assert network_guard.drain_violations() == []
