"""執行器控制通道的 frame 格式（SSOT；只用標準函式庫，proxy、relay 與執行器伺服器共用）.

一條連線一條指令。每個 frame 是 1 位元組類型＋4 位元組長度（big-endian）＋payload：

- JSON 類型（HELLO、READY、START、STARTED、EXIT、ERROR）：UTF-8 JSON 物件，不能有重複的鍵、
  不能有 NaN／Infinity；START 上限 1 MiB，其他 64 KiB；
- 資料類型（OUT、ERR、STDIN）：原始位元組，上限 1 MiB；
- 空類型（STDIN_EOF、STOP）：payload 必須是空的。

看不懂的類型、超過上限（看到標頭就拒絕，不先收 payload）、截斷、JSON 不合格一律拋
``FrameError``；對端回 ERROR 後關閉連線。訊息欄位的驗證由兩端各自負責，這裡只管格式。
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

PROTOCOL_VERSION = 1
# ipc 資料夾的版面：socket 與權杖檔（權杖 ``secrets.token_hex(32)``，每次啟動換新；proxy 與 relay
# 每次連線都從檔案讀，argv 與環境變數都沒有權杖）；boot_id 是 ``secrets.token_hex(16)``
SOCKET_NAME, TOKEN_NAME = "ctl.sock", "token"
TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")
BOOT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

HELLO, READY, START, STARTED = 1, 2, 3, 4
OUT, ERR, STDIN, STDIN_EOF = 5, 6, 7, 8
EXIT, STOP, ERROR = 9, 10, 11
TYPE_NAMES = {
    HELLO: "HELLO", READY: "READY", START: "START", STARTED: "STARTED", OUT: "OUT", ERR: "ERR",
    STDIN: "STDIN", STDIN_EOF: "STDIN_EOF", EXIT: "EXIT", STOP: "STOP", ERROR: "ERROR",
}
JSON_TYPES = frozenset({HELLO, READY, START, STARTED, EXIT, ERROR})
DATA_TYPES = frozenset({OUT, ERR, STDIN})
EMPTY_TYPES = frozenset({STDIN_EOF, STOP})

MAX_CONTROL_BYTES = 64 * 1024
MAX_START_BYTES = 1024 * 1024
MAX_DATA_BYTES = 1024 * 1024
_HEADER = struct.Struct(">BI")
HEADER_SIZE = _HEADER.size

# ERROR 的代碼：protocol＝格式或順序不對；auth＝權杖不對；version＝協定版本不同；
# rejected＝START 沒通過驗證。看不懂的代碼一律當 protocol
ERROR_PROTOCOL, ERROR_AUTH = "protocol", "auth"
ERROR_VERSION, ERROR_REJECTED = "version", "rejected"
ERROR_CODES = frozenset({ERROR_PROTOCOL, ERROR_AUTH, ERROR_VERSION, ERROR_REJECTED})


def block_ports_digest(ports: Iterable[int]) -> str:
    """封鎖埠集合的 sha256（排序、去重、緊湊 JSON，小寫 hex）：執行器在 READY 回報、proxy 核對."""
    text = json.dumps(sorted(set(ports)), separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


class FrameError(Exception):
    """格式錯誤；``code``：frame_type／frame_too_large／frame_truncated／frame_json。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Frame:
    """一個完整的 frame。"""

    kind: int
    payload: bytes


def limit_for(kind: int) -> int:
    """這個類型的 payload 上限；看不懂的類型拋 ``frame_type``。"""
    if kind in EMPTY_TYPES:
        return 0
    if kind == START:
        return MAX_START_BYTES
    if kind in DATA_TYPES:
        return MAX_DATA_BYTES
    if kind in JSON_TYPES:
        return MAX_CONTROL_BYTES
    raise FrameError("frame_type")


def encode(kind: int, payload: bytes = b"") -> bytes:
    """組一個 frame；類型或長度不合格就拋錯，不會送出去。"""
    if len(payload) > limit_for(kind):
        raise FrameError("frame_too_large")
    return _HEADER.pack(kind, len(payload)) + payload


def encode_json(kind: int, document: dict) -> bytes:
    """JSON 類型的 frame（緊湊、UTF-8；上限以位元組計）。"""
    if kind not in JSON_TYPES:
        raise FrameError("frame_type")
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return encode(kind, text.encode("utf-8"))


def encode_error(code: str) -> bytes:
    """ERROR frame；只收 ``ERROR_CODES``。"""
    if code not in ERROR_CODES:
        raise FrameError("frame_json")
    return encode_json(ERROR, {"code": code})


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FrameError("frame_json")
        result[key] = value
    return result


def _no_constants(_name: str) -> None:
    raise FrameError("frame_json")


def decode_json(frame: Frame) -> dict:
    """JSON 類型的 payload；必須是一個沒有重複鍵的物件。"""
    if frame.kind not in JSON_TYPES:
        raise FrameError("frame_type")
    try:
        document = json.loads(
            frame.payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_no_constants,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError("frame_json") from exc
    if not isinstance(document, dict):
        raise FrameError("frame_json")
    return document


def error_code(frame: Frame) -> str:
    """ERROR frame 的代碼；格式不對或看不懂一律 ``protocol``。"""
    try:
        code = decode_json(frame).get("code")
    except FrameError:
        return ERROR_PROTOCOL
    return code if code in ERROR_CODES else ERROR_PROTOCOL


def _check_header(header: bytes) -> tuple[int, int]:
    kind, length = _HEADER.unpack(header)
    if length > limit_for(kind):
        raise FrameError("frame_too_large")
    return kind, length


class FrameDecoder:
    """逐段餵入位元組、取出完整的 frame；出錯之後不再接受輸入。"""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._broken = False

    def feed(self, data: bytes) -> list[Frame]:
        """餵入資料，回傳已完整的 frame（可能是空的）。"""
        if self._broken:
            raise FrameError("frame_type")
        self._buffer += data
        frames: list[Frame] = []
        try:
            while len(self._buffer) >= HEADER_SIZE:
                kind, length = _check_header(bytes(self._buffer[:HEADER_SIZE]))
                end = HEADER_SIZE + length
                if len(self._buffer) < end:
                    break
                frames.append(Frame(kind, bytes(self._buffer[HEADER_SIZE:end])))
                del self._buffer[:end]
        except FrameError:
            self._broken = True
            raise
        return frames

    def close(self) -> None:
        """連線結束：還有沒收完的 frame 就是截斷。"""
        if self._buffer:
            self._broken = True
            raise FrameError("frame_truncated")


def _read_exact(recv: Callable[[int], bytes], size: int, *, at_boundary: bool) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = recv(remaining)
        if not chunk:
            if at_boundary and remaining == size:
                return None
            raise FrameError("frame_truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(recv: Callable[[int], bytes]) -> Frame | None:
    """用阻塞式 ``recv(n)``（最多 n 個位元組，EOF 回空）讀一個 frame；在邊界遇到 EOF 回 None。"""
    header = _read_exact(recv, HEADER_SIZE, at_boundary=True)
    if header is None:
        return None
    kind, length = _check_header(header)
    payload = _read_exact(recv, length, at_boundary=False) if length else b""
    return Frame(kind, payload or b"")
