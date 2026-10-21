"""執行器控制通道的 frame 格式（``sandbox_kernel/framing.py``；純函式，任何平台都能跑）.

1 位元組類型＋4 位元組長度（big-endian）＋payload。上限：控制 JSON 64 KiB、START 1 MiB、
資料 1 MiB；看不懂的類型、截斷、超過上限、重複的 JSON 鍵一律 ``FrameError``（對端回 ERROR 後關閉）。
"""

from __future__ import annotations

import json
import struct

import pytest
from src.sandbox_kernel import framing as fr

KIB, MIB = 1024, 1024 * 1024
ALL_TYPES = sorted(fr.TYPE_NAMES)


def _code(call, *args) -> str:
    with pytest.raises(fr.FrameError) as caught:
        call(*args)
    return caught.value.code


def test_protocol_constants():
    assert fr.PROTOCOL_VERSION == 1
    assert fr.HEADER_SIZE == 5
    assert set(fr.TYPE_NAMES.values()) == {
        "HELLO", "READY", "START", "STARTED", "OUT", "ERR", "STDIN", "STDIN_EOF", "EXIT",
        "STOP", "ERROR",
    }
    assert set(fr.TYPE_NAMES) == fr.JSON_TYPES | fr.DATA_TYPES | fr.EMPTY_TYPES
    assert not (fr.JSON_TYPES & fr.DATA_TYPES or fr.JSON_TYPES & fr.EMPTY_TYPES)
    assert (fr.MAX_CONTROL_BYTES, fr.MAX_START_BYTES, fr.MAX_DATA_BYTES) == (64 * KIB, MIB, MIB)


def test_frame_layout_is_type_length_payload():
    assert fr.encode(fr.OUT, b"hi") == bytes([fr.OUT]) + struct.pack(">I", 2) + b"hi"
    assert fr.encode(fr.STDIN_EOF) == bytes([fr.STDIN_EOF, 0, 0, 0, 0])


@pytest.mark.parametrize("kind", ALL_TYPES, ids=lambda k: fr.TYPE_NAMES[k])
def test_every_type_round_trips_through_the_decoder(kind):
    if kind in fr.EMPTY_TYPES:
        payload = b""
    elif kind in fr.DATA_TYPES:
        payload = b"\x00\xffdata"
    else:
        payload = json.dumps({"v": 1, "x": "中"}, ensure_ascii=False).encode("utf-8")
    decoder = fr.FrameDecoder()
    assert decoder.feed(fr.encode(kind, payload)) == [fr.Frame(kind, payload)]
    decoder.close()


@pytest.mark.parametrize("kind", [0, 12, 99, 255, -1, 256])
def test_unknown_types_are_refused_on_both_sides(kind):
    assert _code(fr.encode, kind, b"") == "frame_type"
    if 0 <= kind <= 255:
        assert _code(fr.FrameDecoder().feed, bytes([kind, 0, 0, 0, 0])) == "frame_type"


@pytest.mark.parametrize(
    ("kind", "limit"),
    [
        (fr.HELLO, 64 * KIB),
        (fr.READY, 64 * KIB),
        (fr.STARTED, 64 * KIB),
        (fr.EXIT, 64 * KIB),
        (fr.ERROR, 64 * KIB),
        (fr.START, MIB),
        (fr.OUT, MIB),
        (fr.ERR, MIB),
        (fr.STDIN, MIB),
    ],
    ids=lambda v: fr.TYPE_NAMES.get(v, str(v)),
)
def test_each_type_has_its_own_payload_limit(kind, limit):
    assert fr.limit_for(kind) == limit
    fr.encode(kind, b"x" * limit)  # 剛好上限可以
    assert _code(fr.encode, kind, b"x" * (limit + 1)) == "frame_too_large"
    header = bytes([kind]) + struct.pack(">I", limit + 1)
    assert _code(fr.FrameDecoder().feed, header) == "frame_too_large"  # 看到標頭就拒絕，不先收


@pytest.mark.parametrize("kind", sorted(fr.EMPTY_TYPES), ids=lambda k: fr.TYPE_NAMES[k])
def test_empty_types_carry_no_payload(kind):
    assert _code(fr.encode, kind, b"x") == "frame_too_large"
    assert _code(fr.FrameDecoder().feed, bytes([kind]) + struct.pack(">I", 1) + b"x") == (
        "frame_too_large"
    )


def test_decoder_handles_byte_by_byte_and_batched_input():
    stream = fr.encode(fr.OUT, b"one") + fr.encode(fr.ERR, b"two") + fr.encode(fr.STDIN_EOF)
    decoder = fr.FrameDecoder()
    frames = [frame for byte in stream for frame in decoder.feed(bytes([byte]))]
    expected = [fr.Frame(fr.OUT, b"one"), fr.Frame(fr.ERR, b"two"), fr.Frame(fr.STDIN_EOF, b"")]
    assert frames == expected
    decoder.close()
    assert fr.FrameDecoder().feed(stream) == frames


@pytest.mark.parametrize("cut", [1, 4, 5, 7])
def test_truncated_frame_at_eof_is_an_error(cut):
    decoder = fr.FrameDecoder()
    assert decoder.feed(fr.encode(fr.OUT, b"abcdef")[:cut]) == []
    assert _code(decoder.close) == "frame_truncated"


def test_decoder_refuses_more_input_after_an_error():
    decoder = fr.FrameDecoder()
    with pytest.raises(fr.FrameError):
        decoder.feed(b"\x00\x00\x00\x00\x00")
    assert _code(decoder.feed, fr.encode(fr.OUT, b"x")) == "frame_type"


def _recv_from(data: bytes, chunk: int = 3):
    view = memoryview(data)
    position = 0

    def _recv(size: int) -> bytes:
        nonlocal position
        piece = bytes(view[position : position + min(size, chunk)])
        position += len(piece)
        return piece

    return _recv


def test_read_frame_reads_one_frame_at_a_time_and_none_at_a_clean_eof():
    recv = _recv_from(fr.encode(fr.OUT, b"hello") + fr.encode_json(fr.EXIT, {"code": 0}))
    assert fr.read_frame(recv) == fr.Frame(fr.OUT, b"hello")
    assert fr.decode_json(fr.read_frame(recv)) == {"code": 0}
    assert fr.read_frame(recv) is None


@pytest.mark.parametrize("cut", [2, 5, 8])
def test_read_frame_eof_inside_a_frame_is_truncated(cut):
    recv = _recv_from(fr.encode(fr.OUT, b"hello")[:cut])
    assert _code(fr.read_frame, recv) == "frame_truncated"


def test_read_frame_checks_type_and_limit_from_the_header():
    assert _code(fr.read_frame, _recv_from(b"\x63\x00\x00\x00\x00")) == "frame_type"
    oversize = bytes([fr.HELLO]) + struct.pack(">I", 64 * KIB + 1)
    assert _code(fr.read_frame, _recv_from(oversize)) == "frame_too_large"


def test_encode_json_is_compact_utf8_and_counts_bytes():
    frame = fr.encode_json(fr.HELLO, {"v": 1, "mode": "exec"})
    assert frame[fr.HEADER_SIZE :] == b'{"v":1,"mode":"exec"}'
    wide = {"x": "中" * (64 * KIB // 3)}  # 字元數在上限內，但 UTF-8 位元組數超過
    assert _code(fr.encode_json, fr.HELLO, wide) == "frame_too_large"
    assert _code(fr.encode_json, fr.OUT, {"x": 1}) == "frame_type"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"v": 1, "v": 1}',
        b'{"a": {"b": 1, "b": 2}}',
        b"[]",
        b'"text"',
        b"",
        b"{",
        b"\xff\xfe",
        b'{"v": NaN}',
    ],
    ids=["duplicate", "nested-duplicate", "array", "string", "empty", "broken", "not-utf8", "nan"],
)
def test_decode_json_refuses_anything_but_one_plain_object(payload):
    assert _code(fr.decode_json, fr.Frame(fr.READY, payload)) == "frame_json"


def test_decode_json_only_for_json_types():
    assert _code(fr.decode_json, fr.Frame(fr.OUT, b"{}")) == "frame_type"
    assert fr.decode_json(fr.Frame(fr.START, b'{"argv": ["true"]}')) == {"argv": ["true"]}


def test_error_frames_carry_a_known_code_only():
    assert {"protocol", "auth", "version", "rejected"} == fr.ERROR_CODES
    for code in sorted(fr.ERROR_CODES):
        assert fr.error_code(fr.FrameDecoder().feed(fr.encode_error(code))[0]) == code
    assert _code(fr.encode_error, "made_up") == "frame_json"
    forged = fr.Frame(fr.ERROR, b'{"code": "made_up"}')
    assert fr.error_code(forged) == "protocol"  # 看不懂的代碼當成協定錯誤
    assert fr.error_code(fr.Frame(fr.ERROR, b"[]")) == "protocol"
