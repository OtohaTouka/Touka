"""proxy 端的執行器健康檢查用戶端（iso-c2 C2-2）.

``check_health`` 連 ``<ipc>/ctl.sock``，送 ``HELLO{v, token, block_ports, mode:"health"}``，
收 ``READY`` 後就關掉連線：

- 權杖每次連線都從 ``<ipc>/token`` 重讀（不跟連結；讀法同 relay，``relay.read_token``），
  argv、環境變數與 log 裡都沒有權杖；執行器重啟換了權杖之後自動恢復。
- 連不上、EOF、截斷、逾時 → ``executor_unreachable``；權杖讀不到或被拒 →
  ``executor_auth_failed``；其他 ERROR、看不懂的 frame、格式不對的 READY →
  ``executor_version_mismatch``（兩邊對協定的理解不同）。

``ready_issue`` 把 READY 和 proxy 這邊的預期逐項核對（monitor 用它判定執行器層）；
``server_digest`` 與 ``block_ports_digest`` 的算法屬於執行器伺服器，由呼叫端算好傳進來。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal, Protocol, cast

from src.sandbox_kernel import framing
from src.sandbox_kernel.relay import RelayError, read_token
from src.sandbox_kernel.spec import NET_SYSCTL_CODES
from src.utils.isolation_policy import EXECUTOR_LAYER_MIN_ABI

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

EXECUTOR_UNREACHABLE = "executor_unreachable"
EXECUTOR_AUTH_FAILED = "executor_auth_failed"
EXECUTOR_VERSION_MISMATCH = "executor_version_mismatch"
EXECUTOR_ROOTS_MISMATCH = "executor_roots_mismatch"
EXECUTOR_SELFTEST_FAILED = "executor_selftest_failed"
KERNEL_OUTDATED = "kernel_outdated"
LANDLOCK_ABI_TOO_OLD = "landlock_abi_too_old"
HEALTH_TIMEOUT_SECONDS = 2.0
READ_CHUNK_BYTES = 64 * 1024

RootAccess = Literal["rw", "ro"]
_ROOT_ACCESS = frozenset({"rw", "ro"})
_READY_KEYS = frozenset(
    {"v", "abi", "enforced", "net", "features", "roots", "state_root", "kernel_digest"}
    | {"server_digest", "boot_id", "block_ports_digest", "uid"}
)
_READY_OPTIONAL = frozenset({"repairs"})  # 自癒計數（U2n）；內容由 monitor 之後另外接
_ROOT_KEYS = frozenset({"path", "access"})
_DIGEST = re.compile(r"[0-9a-f]{16,128}")
_FEATURE = re.compile(r"[a-z_]{1,32}")
_ERROR_REASONS = {framing.ERROR_AUTH: EXECUTOR_AUTH_FAILED}


@dataclass(frozen=True)
class ExecutorRoot:
    """執行器掛載的一個 root（執行器那邊的絕對路徑）。"""

    path: str
    access: RootAccess


@dataclass(frozen=True)
class ExecutorReady:
    """格式驗證過的 READY。"""

    version: int
    abi: int
    enforced: bool
    net: str | None
    features: tuple[str, ...]
    roots: tuple[ExecutorRoot, ...]
    state_root: str
    kernel_digest: str
    server_digest: str
    boot_id: str
    block_ports_digest: str
    uid: int


@dataclass(frozen=True)
class ExecutorHealth:
    """一次健康檢查的結果：``ready`` 與 ``reason`` 恰好一個有值。"""

    ready: ExecutorReady | None
    reason: str | None = None


@dataclass(frozen=True)
class ExecutorExpectation:
    """proxy 這邊對 READY 的預期；``None`` 表示算不出來（一律當不符）。"""

    kernel_digest: str | None
    server_digest: str | None
    block_ports_digest: str | None
    roots: frozenset[ExecutorRoot]


class Connector(Protocol):
    """開一條到 socket 的雙向 stream（DIP：測試用假的 stream）。"""

    async def __call__(
        self, path: Path
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """連線；失敗拋 ``OSError``。"""
        ...


async def open_unix_socket(path: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """預設的連線方式；Windows 的 asyncio 沒有 AF_UNIX，當成連不上。"""
    opener = getattr(asyncio, "open_unix_connection", None)
    if opener is None:
        raise ConnectionRefusedError
    return await opener(str(path))


def _int(value: object) -> bool:
    return type(value) is int and value >= 0


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value


def _choice(value: object, allowed: frozenset[str]) -> bool:
    return isinstance(value, str) and value in allowed  # 先確認型別：list 之類不能拿來查集合


def _root(item: object) -> ExecutorRoot | None:
    if not isinstance(item, dict) or set(item) != _ROOT_KEYS:
        return None
    path, access = item["path"], item["access"]
    absolute = _text(path) and PurePosixPath(path).is_absolute()
    if not absolute or not _choice(access, _ROOT_ACCESS):
        return None
    return ExecutorRoot(path, cast("RootAccess", access))


def parse_ready(document: dict) -> ExecutorReady | None:
    """READY 的格式：必要鍵齊全、沒有多的鍵（``repairs`` 例外）、型別都對；不合格回 None。"""
    keys = set(document)
    if not keys >= _READY_KEYS or keys - _READY_KEYS - _READY_OPTIONAL:
        return None
    net, features, roots = document["net"], document["features"], document["roots"]
    digests = [document[k] for k in ("kernel_digest", "server_digest", "block_ports_digest")]
    valid = (
        all(_int(document[k]) for k in ("v", "abi", "uid"))
        and type(document["enforced"]) is bool
        and (net is None or _choice(net, NET_SYSCTL_CODES))
        and isinstance(features, list)
        and all(isinstance(f, str) and _FEATURE.fullmatch(f) for f in features)
        and isinstance(roots, list)
        and _text(document["state_root"])
        and all(isinstance(d, str) and _DIGEST.fullmatch(d) for d in digests)
        and isinstance(document["boot_id"], str)
        and framing.BOOT_ID_PATTERN.fullmatch(document["boot_id"]) is not None
    )
    parsed = tuple(_root(item) for item in roots) if valid else ()
    if not valid or None in parsed:
        return None
    return ExecutorReady(
        version=document["v"],
        abi=document["abi"],
        enforced=document["enforced"],
        net=net,
        features=tuple(features),
        roots=cast("tuple[ExecutorRoot, ...]", parsed),
        state_root=document["state_root"],
        kernel_digest=digests[0],
        server_digest=digests[1],
        boot_id=document["boot_id"],
        block_ports_digest=digests[2],
        uid=document["uid"],
    )


def ready_issue(ready: ExecutorReady, expected: ExecutorExpectation) -> str | None:
    """READY 和預期不符的第一個原因；全部相符回 None（網路設定與封鎖埠由 monitor 另外判）。"""
    if ready.version != framing.PROTOCOL_VERSION:
        return EXECUTOR_VERSION_MISMATCH
    if ready.kernel_digest != expected.kernel_digest:  # 預期是 None（算不出來）也算不符
        return KERNEL_OUTDATED
    if (ready.server_digest, ready.block_ports_digest) != (
        expected.server_digest,
        expected.block_ports_digest,
    ):
        return EXECUTOR_VERSION_MISMATCH
    if len(set(ready.roots)) != len(ready.roots) or set(ready.roots) != expected.roots:
        return EXECUTOR_ROOTS_MISMATCH
    if ready.abi < EXECUTOR_LAYER_MIN_ABI:
        return LANDLOCK_ABI_TOO_OLD
    return None if ready.enforced else EXECUTOR_SELFTEST_FAILED


async def _first_frame(reader: asyncio.StreamReader) -> framing.Frame | None:
    """讀到第一個完整的 frame；在那之前 EOF（含截斷）回 None。"""
    decoder = framing.FrameDecoder()
    while chunk := await reader.read(READ_CHUNK_BYTES):
        frames = decoder.feed(chunk)
        if frames:
            return frames[0]
    return None


def _health_from(frame: framing.Frame | None) -> ExecutorHealth:
    if frame is None:
        return ExecutorHealth(None, EXECUTOR_UNREACHABLE)
    if frame.kind == framing.ERROR:
        code = framing.error_code(frame)
        return ExecutorHealth(None, _ERROR_REASONS.get(code, EXECUTOR_VERSION_MISMATCH))
    ready = parse_ready(framing.decode_json(frame)) if frame.kind == framing.READY else None
    if ready is None:
        return ExecutorHealth(None, EXECUTOR_VERSION_MISMATCH)
    return ExecutorHealth(ready)


async def _exchange(
    ipc_dir: Path, block_ports: Sequence[int], connect: Connector
) -> ExecutorHealth:
    try:
        reader, writer = await connect(ipc_dir / framing.SOCKET_NAME)
    except (OSError, AttributeError):
        return ExecutorHealth(None, EXECUTOR_UNREACHABLE)
    try:
        try:
            token = await asyncio.to_thread(read_token, str(ipc_dir))
        except RelayError:
            return ExecutorHealth(None, EXECUTOR_AUTH_FAILED)
        hello = {"v": framing.PROTOCOL_VERSION, "token": token, "block_ports": list(block_ports)}
        writer.write(framing.encode_json(framing.HELLO, {**hello, "mode": "health"}))
        await writer.drain()
        return _health_from(await _first_frame(reader))
    except OSError:
        return ExecutorHealth(None, EXECUTOR_UNREACHABLE)
    except framing.FrameError:  # 看不懂的類型、超過上限、JSON 不合格：兩邊的協定不同
        return ExecutorHealth(None, EXECUTOR_VERSION_MISMATCH)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def check_health(
    ipc_dir: Path,
    block_ports: Sequence[int],
    *,
    timeout: float = HEALTH_TIMEOUT_SECONDS,
    connect: Connector = open_unix_socket,
) -> ExecutorHealth:
    """一次健康檢查（mode health）；不拋例外，逾時當成連不上。"""
    try:
        return await asyncio.wait_for(_exchange(ipc_dir, block_ports, connect), timeout)
    except TimeoutError:
        return ExecutorHealth(None, EXECUTOR_UNREACHABLE)
