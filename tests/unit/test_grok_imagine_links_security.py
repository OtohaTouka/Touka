"""生圖工具把圖存進個人空間的 ``outputs``，不跟隨連結（ADR-0293）。

代理人的指令可以把 ``outputs`` 換成連結，指到假的設定資料夾；以前圖會寫進連結另一端，
現在整個不存、回錯誤。連 xAI 的兩個請求換成假的 client，不連外、不花額度（禁令 61）；
data 根換到 ``zone_data_root``（``tmp_path``），不碰 ``data/``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from src.models.identity import AgentIdentity
from src.tools import grok_imagine_server as grok

from tests.support.links import LINK_FOLLOWED, make_dir_link
from tests.support.zone_records import expect_zone_refusal

if TYPE_CHECKING:
    from pathlib import Path

MODEL = "M1"
_IMAGE = b"\x89PNG fake image"


class _Response:
    def __init__(self, payload: dict[str, Any] | None = None, content: bytes = b"") -> None:
        self.status_code = 200
        self.text = ""
        self._payload = payload or {}
        self.content = content

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Client:
    """假的 ``httpx.AsyncClient``：生圖回一個網址，下載回固定的位元組."""

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, *_args: Any, **_kwargs: Any) -> _Response:
        return _Response({"data": [{"url": "https://images.invalid/fake.png"}]})

    async def get(self, *_args: Any, **_kwargs: Any) -> _Response:
        return _Response(content=_IMAGE)


@pytest.fixture
def fake_xai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grok, "GROK_API_KEY", "unit-test-placeholder")
    monkeypatch.setattr(grok.httpx, "AsyncClient", _Client)


def _outputs(zone_data_root: Path) -> Path:
    return zone_data_root / "custom" / MODEL / "drive" / "outputs"


async def test_a_linked_outputs_folder_is_not_written(
    zone_data_root: Path, tmp_path: Path, fake_xai: None
) -> None:
    fake = tmp_path.resolve() / "fake_config"
    fake.mkdir()
    (fake / "users.json").write_text("{}", encoding="utf-8")
    _outputs(zone_data_root).parent.mkdir(parents=True)
    make_dir_link(_outputs(zone_data_root), fake)

    with expect_zone_refusal("ZoneLinkRefusedError"):
        result = await grok.grok_imagine("a cat", AgentIdentity(agent_id=MODEL))

    assert sorted(p.name for p in fake.iterdir()) == ["users.json"], LINK_FOLLOWED
    assert result.startswith("❌")


async def test_the_image_lands_in_the_agent_outputs(zone_data_root: Path, fake_xai: None) -> None:
    result = await grok.grok_imagine("a cat", AgentIdentity(agent_id=MODEL))

    saved = list(_outputs(zone_data_root).iterdir())
    assert len(saved) == 1, result
    assert saved[0].name.startswith("grok_") and saved[0].suffix == ".png"
    assert saved[0].read_bytes() == _IMAGE
    assert f"/v1/outputs/{MODEL}/{saved[0].name}" in result
