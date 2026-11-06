"""``/v1/outputs`` 的回應物件：關檔與交出字串的登記（ADR-0293 R7）。

Linux 版 ``ZoneFileResponse`` 持有開好的檔案，送完、出錯（400／416）、送到一半中斷或沒被呼叫就被
丟掉時都會關檔並取消 ``PROC_FD`` 登記。沒有 ``/proc`` 的系統用 ``PathZoneFileResponse``：只在送出
期間以 ``WINDOWS_FILE_RESPONSE`` 登記。兩者都直接建構，不經路由，所以在任何系統都能跑。

data 根換到 ``zone_data_root``（``tmp_path``），不碰 ``data/``。
"""

from __future__ import annotations

import asyncio
import gc
import os
from typing import TYPE_CHECKING, Any

import pytest
from memory_server.utils.zone_paths import HandoffKind, is_handed_off
from src.models.identity import AgentIdentity
from src.services import zone_file_response as zfr
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from pathlib import Path

ALICE = AgentIdentity.for_role(agent_id="Alice", role="Owner")
CONTENT = b"hello outputs 0123456789\n"


@pytest.fixture
def outputs(zone_data_root: Path) -> Path:
    folder = zone_data_root / "personal_drive" / "Alice" / "outputs"
    folder.mkdir(parents=True)
    (folder / "x.txt").write_bytes(CONTENT)
    return folder


def _proc_response(path: Path) -> tuple[zfr.ZoneFileResponse, Any]:
    handle = path.open("rb")
    return zfr.ZoneFileResponse(handle, os.fstat(handle.fileno()), "text/plain"), handle


def _scope(headers: dict[str, str]) -> dict[str, Any]:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return {"type": "http", "method": "GET", "headers": raw}


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.disconnect"}


@pytest.mark.parametrize("headers", [{"Range": "bytes=x"}, {"Range": "bytes=999-"}])
def test_error_responses_close_the_file_and_release(outputs: Path, headers: dict[str, str]) -> None:
    response, handle = _proc_response(outputs / "x.txt")
    proc = response.path
    assert is_handed_off(proc, HandoffKind.PROC_FD)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        assert not handle.closed  # 送出前檔案一直開著
        sent.append(message)

    asyncio.run(response(_scope(headers), _noop_receive, send))
    assert sent[0]["status"] in {400, 416}
    assert handle.closed and not is_handed_off(proc, HandoffKind.PROC_FD)


def test_an_interrupted_response_closes_the_file(outputs: Path) -> None:
    response, handle = _proc_response(outputs / "x.txt")

    async def send(_message: dict[str, Any]) -> None:
        raise ConnectionResetError

    with pytest.raises(ConnectionResetError):
        asyncio.run(response(_scope({}), _noop_receive, send))
    assert handle.closed and not is_handed_off(response.path, HandoffKind.PROC_FD)


def test_a_response_dropped_without_being_called_closes_the_file(outputs: Path) -> None:
    response, handle = _proc_response(outputs / "x.txt")
    proc = response.path
    gc.collect()
    assert not handle.closed  # 回應物件還在：檔案開著，fd 號碼不會被重用
    del response
    gc.collect()
    assert handle.closed and not is_handed_off(proc, HandoffKind.PROC_FD)


def test_the_path_response_is_handed_off_only_while_it_is_sent(outputs: Path) -> None:
    """``PathZoneFileResponse``：建立時不登記，送出期間登記，送完取消；內容照送。"""
    kind = HandoffKind.WINDOWS_FILE_RESPONSE
    target = str(outputs / "x.txt")
    zp = resource_manager.outputs_dir(ALICE).child("x.txt")
    response = zfr.PathZoneFileResponse(zp, "text/plain")
    assert not is_handed_off(target, kind)
    seen: list[bool] = []
    body = bytearray()

    async def send(message: dict[str, Any]) -> None:
        seen.append(is_handed_off(target, kind))
        body.extend(message.get("body", b""))

    asyncio.run(response(_scope({}), _noop_receive, send))
    assert seen and all(seen) and bytes(body) == CONTENT
    assert not is_handed_off(target, kind)
