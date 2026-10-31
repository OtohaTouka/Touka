"""proxy 端的執行器健康檢查用戶端（iso-c2 C2-2 U2d）.

假的 stream 扮演執行器：每次連線重讀權杖、HELLO 的內容、READY 的格式驗證、各種錯誤對應的
原因，以及 READY 和預期的核對（版本、kernel、伺服器程式、封鎖埠、roots、自測、ABI）。
Windows 也能跑（不需要 AF_UNIX）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest
from src.sandbox_kernel import framing as fr
from src.services.isolation import executor_client as ec

TOKEN_A = "a" * 64
TOKEN_B = "b" * 64
BOOT = "c" * 32
DIGEST = "d" * 64
ROOTS = [
    {"path": "/srv/data/personal_drive", "access": "rw"},
    {"path": "/srv/data/mcp_servers/managed", "access": "rw"},
    {"path": "/srv/config/skills", "access": "ro"},
]


def _ready_doc(**overrides: object) -> dict:
    doc = {
        "v": fr.PROTOCOL_VERSION,
        "abi": 6,
        "enforced": True,
        "net": None,
        "features": ["fs", "tcp", "ioctl", "signal", "abstract_unix"],
        "roots": [dict(r) for r in ROOTS],
        "state_root": "/srv/executor_state",
        "kernel_digest": DIGEST,
        "server_digest": "e" * 64,
        "boot_id": BOOT,
        "block_ports_digest": "f" * 64,
        "uid": 1001,
    }
    doc.update(overrides)
    return doc


class _Writer:
    def __init__(self, server: _FakeExecutor) -> None:
        self._server = server

    def write(self, data: bytes) -> None:
        self._server.sent += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._server.closed += 1

    async def wait_closed(self) -> None:
        return None


class _FakeExecutor:
    """一條連線回一段固定的位元組；記錄連到哪裡、收到什麼、有沒有關掉。"""

    def __init__(self, reply: bytes = b"", *, refuse: BaseException | None = None) -> None:
        self.reply = reply
        self.refuse = refuse
        self.sent = bytearray()
        self.paths: list[object] = []
        self.closed = 0
        self.stall = False

    async def connect(self, path: object) -> tuple[asyncio.StreamReader, _Writer]:
        self.paths.append(path)
        if self.refuse is not None:
            raise self.refuse
        reader = asyncio.StreamReader()
        if not self.stall:
            reader.feed_data(self.reply)
            reader.feed_eof()
        return reader, _Writer(self)

    def hellos(self) -> list[dict]:
        decoder = fr.FrameDecoder()
        return [fr.decode_json(frame) for frame in decoder.feed(bytes(self.sent))]


def _ready(**overrides: object) -> bytes:
    return fr.encode_json(fr.READY, _ready_doc(**overrides))


@pytest.fixture
def ipc(tmp_path):
    (tmp_path / fr.TOKEN_NAME).write_text(TOKEN_A + "\n", encoding="ascii")
    return tmp_path


async def _health(ipc, server: _FakeExecutor, ports=(8000, 2375), **kwargs):
    return await ec.check_health(ipc, ports, connect=server.connect, **kwargs)


# --- 連線與權杖 ------------------------------------------------------------------------


async def test_token_is_reread_from_the_file_on_every_connection(ipc):
    server = _FakeExecutor(_ready())
    first = await _health(ipc, server)
    (ipc / fr.TOKEN_NAME).write_text(TOKEN_B, encoding="ascii")  # 執行器重啟換了新權杖
    second = await _health(ipc, server)
    assert first.reason is None and second.reason is None
    assert [h["token"] for h in server.hellos()] == [TOKEN_A, TOKEN_B]
    assert server.paths == [ipc / fr.SOCKET_NAME] * 2


async def test_hello_is_health_mode_with_the_given_block_ports(ipc):
    server = _FakeExecutor(_ready())
    await _health(ipc, server, ports=(2375, 8000, 11434))
    (hello,) = server.hellos()
    assert hello == {
        "v": fr.PROTOCOL_VERSION,
        "token": TOKEN_A,
        "block_ports": [2375, 8000, 11434],
        "mode": "health",
    }
    assert server.closed == 1


@pytest.mark.parametrize("content", [None, "", "\n", "a" * 63, "A" * 64, "g" * 64, "a" * 65])
async def test_missing_or_malformed_token_is_auth_failure_without_hello(ipc, content):
    token = ipc / fr.TOKEN_NAME
    if content is None:
        token.unlink()
    else:
        token.write_text(content, encoding="ascii")
    server = _FakeExecutor(_ready())
    health = await _health(ipc, server)
    assert (health.ready, health.reason) == (None, "executor_auth_failed")
    assert server.sent == b""
    assert server.closed == 1


@pytest.mark.parametrize(
    "refuse", [FileNotFoundError(), ConnectionRefusedError(), OSError(), AttributeError()]
)
async def test_connect_failure_is_unreachable(ipc, refuse):
    health = await _health(ipc, _FakeExecutor(refuse=refuse))
    assert (health.ready, health.reason) == (None, "executor_unreachable")


async def test_default_connector_without_a_socket_is_unreachable(ipc):
    """沒有 socket（或 Windows 沒有 AF_UNIX）一律是連不上，不拋例外。"""
    health = await ec.check_health(ipc / "missing", (8000,), timeout=2)
    assert (health.ready, health.reason) == (None, "executor_unreachable")


async def test_eof_truncation_and_timeout_are_unreachable(ipc):
    for reply in (b"", _ready()[:-3]):
        health = await _health(ipc, _FakeExecutor(reply))
        assert health.reason == "executor_unreachable", reply
    stalled = _FakeExecutor()
    stalled.stall = True
    health = await _health(ipc, stalled, timeout=0.05)
    assert health.reason == "executor_unreachable"


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (fr.ERROR_AUTH, "executor_auth_failed"),
        (fr.ERROR_VERSION, "executor_version_mismatch"),
        (fr.ERROR_PROTOCOL, "executor_version_mismatch"),
        (fr.ERROR_REJECTED, "executor_version_mismatch"),
    ],
)
async def test_error_frames_map_to_reasons(ipc, code, reason):
    health = await _health(ipc, _FakeExecutor(fr.encode_error(code)))
    assert (health.ready, health.reason) == (None, reason)


@pytest.mark.parametrize(
    "reply",
    [
        fr.encode_json(fr.STARTED, {"exec_id": "x"}),  # 不是 READY
        b"\x63\x00\x00\x00\x00",  # 看不懂的類型
        fr.encode(fr.READY, b"[1, 2]"),  # 不是 JSON 物件
        fr.encode(fr.READY, b'{"v": 1, "v": 1}'),  # 重複的鍵
    ],
)
async def test_protocol_disagreement_is_version_mismatch(ipc, reply):
    health = await _health(ipc, _FakeExecutor(reply))
    assert (health.ready, health.reason) == (None, "executor_version_mismatch")


# --- READY 的格式 -----------------------------------------------------------------------


async def test_valid_ready_is_parsed(ipc):
    health = await _health(ipc, _FakeExecutor(_ready(net="net_mptcp", repairs={"chmod": 2})))
    ready = health.ready
    assert health.reason is None and ready is not None
    assert (ready.version, ready.abi, ready.enforced, ready.net) == (1, 6, True, "net_mptcp")
    assert ready.roots == tuple(ec.ExecutorRoot(r["path"], r["access"]) for r in ROOTS)
    assert (ready.state_root, ready.boot_id, ready.uid) == ("/srv/executor_state", BOOT, 1001)
    assert (ready.kernel_digest, ready.server_digest) == (DIGEST, "e" * 64)
    assert ready.block_ports_digest == "f" * 64
    assert ready.features == ("fs", "tcp", "ioctl", "signal", "abstract_unix")


def _without(key: str) -> dict:
    doc = _ready_doc()
    del doc[key]
    return doc


_BAD_READY = {
    "missing_boot_id": _without("boot_id"),
    "missing_roots": _without("roots"),
    "extra_key": _ready_doc(token=TOKEN_A),
    "abi_bool": _ready_doc(abi=True),
    "abi_negative": _ready_doc(abi=-1),
    "v_str": _ready_doc(v="1"),
    "enforced_int": _ready_doc(enforced=1),
    "net_unknown": _ready_doc(net="net_other"),
    "net_unhashable": _ready_doc(net=["net_mptcp"]),
    "roots_access_unhashable": _ready_doc(roots=[{"path": "/srv/x", "access": ["rw"]}]),
    "features_not_list": _ready_doc(features="fs"),
    "features_item": _ready_doc(features=["fs", 3]),
    "roots_access": _ready_doc(roots=[{"path": "/srv/x", "access": "rwx"}]),
    "roots_extra": _ready_doc(roots=[{"path": "/srv/x", "access": "rw", "x": 1}]),
    "roots_path_type": _ready_doc(roots=[{"path": 1, "access": "rw"}]),
    "roots_relative": _ready_doc(roots=[{"path": "srv/x", "access": "rw"}]),
    "state_root_empty": _ready_doc(state_root=""),
    "state_root_nul": _ready_doc(state_root="/srv/\x00"),
    "boot_id_short": _ready_doc(boot_id="c" * 31),
    "digest_upper": _ready_doc(kernel_digest="D" * 64),
    "digest_type": _ready_doc(server_digest=None),
    "uid_negative": _ready_doc(uid=-1),
}


@pytest.mark.parametrize("case", list(_BAD_READY))
async def test_malformed_ready_is_version_mismatch(ipc, case):
    reply = fr.encode_json(fr.READY, _BAD_READY[case])
    health = await _health(ipc, _FakeExecutor(reply))
    assert (health.ready, health.reason) == (None, "executor_version_mismatch")


async def test_the_token_never_reaches_logs_or_results(ipc, caplog):
    caplog.set_level(logging.DEBUG)
    for reply in (_ready(), fr.encode_error(fr.ERROR_AUTH), b"garbage!"):
        health = await _health(ipc, _FakeExecutor(reply))
        assert TOKEN_A not in repr(health)
    assert TOKEN_A not in caplog.text


# --- READY 和預期的核對 -------------------------------------------------------------------

EXPECTED = ec.ExecutorExpectation(
    kernel_digest=DIGEST,
    server_digest="e" * 64,
    block_ports_digest="f" * 64,
    roots=frozenset(ec.ExecutorRoot(r["path"], r["access"]) for r in ROOTS),
)


def _parsed(**overrides: object) -> ec.ExecutorReady:
    ready = ec.parse_ready(_ready_doc(**overrides))
    assert ready is not None
    return ready


def test_ready_matching_every_expectation_has_no_issue():
    assert ec.ready_issue(_parsed(), EXPECTED) is None
    assert ec.ready_issue(_parsed(abi=3, net="net_tcp_fastopen"), EXPECTED) is None


_ISSUES = {
    "version": ({"v": 2}, {}, "executor_version_mismatch"),
    "kernel": ({"kernel_digest": "0" * 64}, {}, "kernel_outdated"),
    "kernel_unreadable": ({}, {"kernel_digest": None}, "kernel_outdated"),
    "server": ({"server_digest": "0" * 64}, {}, "executor_version_mismatch"),
    "server_unwired": ({}, {"server_digest": None}, "executor_version_mismatch"),
    "block_ports": ({"block_ports_digest": "0" * 64}, {}, "executor_version_mismatch"),
    "block_ports_unwired": ({}, {"block_ports_digest": None}, "executor_version_mismatch"),
    "roots_subset": ({"roots": ROOTS[:2]}, {}, "executor_roots_mismatch"),
    "roots_superset": (
        {"roots": [*ROOTS, {"path": "/srv/workspace", "access": "rw"}]},
        {},
        "executor_roots_mismatch",
    ),
    "roots_duplicate": ({"roots": [*ROOTS, ROOTS[0]]}, {}, "executor_roots_mismatch"),
    "roots_access": (
        {"roots": [{**ROOTS[0], "access": "ro"}, *ROOTS[1:]]},
        {},
        "executor_roots_mismatch",
    ),
    "abi_too_old": ({"abi": 2}, {}, "landlock_abi_too_old"),
    "not_enforced": ({"enforced": False}, {}, "executor_selftest_failed"),
}


@pytest.mark.parametrize("case", list(_ISSUES))
def test_ready_issue_names_the_first_mismatch(case):
    ready_overrides, expected_overrides, reason = _ISSUES[case]
    expected = replace(EXPECTED, **expected_overrides)
    assert ec.ready_issue(_parsed(**ready_overrides), expected) == reason


def test_ready_issue_order_puts_version_and_kernel_before_the_rest():
    everything = _parsed(
        v=2, kernel_digest="0" * 64, server_digest="0" * 64, roots=ROOTS[:1], enforced=False
    )
    assert ec.ready_issue(everything, EXPECTED) == "executor_version_mismatch"
    assert ec.ready_issue(replace(everything, version=1), EXPECTED) == "kernel_outdated"
