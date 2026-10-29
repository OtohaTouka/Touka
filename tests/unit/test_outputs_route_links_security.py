"""``/v1/outputs`` 回檔不跟隨連結，標頭、Range 與 If-Range 和直接送出同一個檔案一樣（ADR-0293）。

代理人的指令可以把自己的 ``outputs`` 或其中的檔名換成連結，指到 ``.env`` 這類檔案所在的資料夾；
這些都回 404（和不存在一樣），不送出另一端的內容。檔名不能當成一段（``..``、``\\``、Windows 的
``C:x``、``CON.png``）時回 403。回應物件的關檔與登記在 ``test_zone_file_response.py``。

這裡只用改之前就有的邊界（路由與 ``tests.support.links``），才能在改之前的程式上跑一次、證明
連結測試會變紅（R15）。data 根換到 ``zone_data_root``（``tmp_path``），不碰 ``data/``。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from src.core.exceptions import ToukaException
from src.main import app, get_sovereign_output
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from starlette.applications import Starlette
from starlette.responses import FileResponse
from starlette.routing import Route

from tests.support.links import LINK_FOLLOWED, make_dir_link

if TYPE_CHECKING:
    from pathlib import Path

ALICE = AgentIdentity.for_role(agent_id="Alice", role="Owner")
CANARY = "CANARY=1"
CONTENT = b"hello outputs 0123456789\n"


@pytest.fixture
def outputs(zone_data_root: Path) -> Path:
    folder = zone_data_root / "personal_drive" / "Alice" / "outputs"
    folder.mkdir(parents=True)
    (folder / "x.txt").write_bytes(CONTENT)
    return folder


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    """連結另一端：有 ``.env`` 和同名的 ``x.txt``（跟隨連結時就讀得到）。"""
    folder = tmp_path.resolve() / "fake"
    folder.mkdir()
    (folder / ".env").write_text(CANARY, encoding="utf-8")
    (folder / "x.txt").write_text(CANARY, encoding="utf-8")
    return folder


@pytest.fixture
def client():
    app.dependency_overrides[get_verified_identity] = lambda: ALICE
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def _reference(path: Path) -> TestClient:
    """直接用 Starlette 的 ``FileResponse`` 送同一個檔案（今天的行為）。"""
    return TestClient(Starlette(routes=[Route("/f", lambda _request: FileResponse(path))]))


REQUESTS: dict[str, dict[str, str]] = {
    "plain": {},
    "range": {"Range": "bytes=2-5"},
    "suffix_range": {"Range": "bytes=-4"},
    "unsatisfiable": {"Range": "bytes=999-"},
    "malformed": {"Range": "bytes=x"},
}
HEADERS = ("content-type", "content-length", "etag", "last-modified", "accept-ranges")


@pytest.mark.parametrize("request_headers", REQUESTS.values(), ids=REQUESTS.keys())
def test_a_regular_file_is_sent_like_a_plain_file_response(
    client: TestClient, outputs: Path, request_headers: dict[str, str]
) -> None:
    got = client.get("/v1/outputs/Alice/x.txt", headers=request_headers)
    want = _reference(outputs / "x.txt").get("/f", headers=request_headers)
    assert (got.status_code, got.content) == (want.status_code, want.content)
    for name in (*HEADERS, "content-range"):
        assert got.headers.get(name) == want.headers.get(name), name


def test_if_range_follows_the_etag(client: TestClient, outputs: Path) -> None:
    etag = client.get("/v1/outputs/Alice/x.txt").headers["etag"]
    same = client.get("/v1/outputs/Alice/x.txt", headers={"Range": "bytes=0-1", "If-Range": etag})
    other = client.get("/v1/outputs/Alice/x.txt", headers={"Range": "bytes=0-1", "If-Range": '"x"'})
    assert (same.status_code, same.content) == (206, CONTENT[:2])
    assert (other.status_code, other.content) == (200, CONTENT)


@pytest.mark.parametrize("swapped", ["outputs", "file"])
def test_a_link_is_not_found(
    client: TestClient, outputs: Path, fake: Path, swapped: str
) -> None:
    """``outputs`` 或檔名本身換成連結：404，另一端的內容沒送出。"""
    if swapped == "outputs":
        outputs.rename(outputs.with_name("outputs_old"))
        make_dir_link(outputs, fake)
    else:
        (outputs / "x.txt").unlink()
        make_dir_link(outputs / "x.txt", fake)
    resp = client.get("/v1/outputs/Alice/x.txt")
    assert CANARY not in resp.text, LINK_FOLLOWED
    assert resp.status_code == 404, resp.text
    assert str(fake) not in resp.text


def test_a_missing_file_or_folder_is_not_found(client: TestClient, outputs: Path) -> None:
    (outputs / "sub").mkdir()
    assert client.get("/v1/outputs/Alice/nope.txt").status_code == 404
    assert client.get("/v1/outputs/Alice/sub").status_code == 404


NOT_A_SEGMENT = ["..", ".", "..\\..\\.env", "D:\\Touka\\.env", " x"]
WINDOWS_ONLY = ["C:x", "CON.png", "x."]  # 只有 Windows 不能當一段


@pytest.mark.parametrize("name", [*NOT_A_SEGMENT, *WINDOWS_ONLY])
async def test_a_name_that_cannot_be_a_segment_is_forbidden(outputs: Path, name: str) -> None:
    """檔名不能當成一段時 403、不送出內容；能當一段（POSIX 的 ``C:x``）就照一般檔名找（404）。"""
    forbidden = name in NOT_A_SEGMENT or sys.platform == "win32"
    with pytest.raises(ToukaException) as exc:
        await get_sovereign_output("Alice", name, identity=ALICE)
    assert exc.value.status_code == (403 if forbidden else 404)


async def test_another_agents_outputs_are_refused(outputs: Path) -> None:
    with pytest.raises(ToukaException) as exc:
        await get_sovereign_output("Bob", "x.txt", identity=ALICE)
    assert exc.value.status_code == 403
