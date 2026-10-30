"""刪除與改名自訂模型時，個人空間根是連結就整個不做（ADR-0293 R11）。

代理人的指令可以把 ``custom/<id>/drive`` 換成連結，指到假的設定資料夾。以前刪除照樣刪掉設定
與人格（刪樹不跟隨連結）、改名照樣搬走；現在兩者都回 409 ``AGENT_DRIVE_LINKED``、什麼都不動，
也不叫停指令。一般刪除與改名在動手前先停這個模型的指令。設定頁「模型」分頁的刪除
（``/v1/models/{id}``）走同一條路；不是自訂模型的 ID（例如只有個人空間的人類）不動資料夾。

用真的路由與 ``ResourceManager``，只把擁有清單換成 MagicMock、停指令換成記錄者。data 根換到
``zone_data_root``（``tmp_path``），不碰 ``data/``。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.tools import terminal

from tests.support.links import CHANGED_ANYWAY, LINK_FOLLOWED, make_dir_link

if TYPE_CHECKING:
    from pathlib import Path

MODEL = "M1"
_ADMIN = AgentIdentity.for_role("link-sec-admin", "admin", is_human=True)


@pytest.fixture
def stop(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr("src.routers.custom_models.user_service", MagicMock())
    recorder = AsyncMock(return_value=0)
    monkeypatch.setattr(terminal, "stop_agent_commands", recorder, raising=False)
    app.dependency_overrides[get_verified_identity] = lambda: _ADMIN
    yield recorder
    app.dependency_overrides.pop(get_verified_identity, None)


def _send(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    return TestClient(app, raise_server_exceptions=False).request(method, url, json=body)


def _model(zone_data_root: Path) -> Path:
    folder = zone_data_root / "custom" / MODEL
    folder.mkdir(parents=True)
    (folder / "config.json").write_text('{"id": "M1"}', encoding="utf-8")
    (folder / "system_prompt.md").write_text("keep", encoding="utf-8")
    return folder


def _linked_model(zone_data_root: Path, tmp_path: Path) -> tuple[Path, Path]:
    fake = tmp_path.resolve() / "fake_config"
    fake.mkdir()
    (fake / "users.json").write_text("{}", encoding="utf-8")
    folder = _model(zone_data_root)
    make_dir_link(folder / "drive", fake)
    return folder, fake


DELETE_URLS = [f"/v1/ui/custom_models/{MODEL}", f"/v1/models/{MODEL}"]


@pytest.mark.parametrize("url", DELETE_URLS)
def test_a_linked_drive_refuses_the_delete(
    url: str, zone_data_root: Path, tmp_path: Path, stop: AsyncMock
) -> None:
    folder, fake = _linked_model(zone_data_root, tmp_path)

    resp = _send("DELETE", url)

    assert (fake / "users.json").read_text(encoding="utf-8") == "{}", LINK_FOLLOWED
    assert (folder / "config.json").exists(), CHANGED_ANYWAY
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "AGENT_DRIVE_LINKED"
    stop.assert_not_awaited()
    assert os.path.lexists(folder / "drive")


def test_a_linked_drive_refuses_the_rename(
    zone_data_root: Path, tmp_path: Path, stop: AsyncMock
) -> None:
    folder, fake = _linked_model(zone_data_root, tmp_path)

    resp = _send("POST", f"/v1/ui/custom_models/{MODEL}/rename", {"new_id": "M2"})

    assert (fake / "users.json").read_text(encoding="utf-8") == "{}", LINK_FOLLOWED
    assert (folder / "config.json").exists(), CHANGED_ANYWAY
    assert not (zone_data_root / "custom" / "M2").exists()
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "AGENT_DRIVE_LINKED"


@pytest.mark.parametrize("url", DELETE_URLS)
def test_delete_stops_the_model_commands_first(
    url: str, zone_data_root: Path, stop: AsyncMock
) -> None:
    folder = _model(zone_data_root)
    seen: list[bool] = []

    async def _record(identity: AgentIdentity) -> int:
        seen.append((folder / "config.json").exists())
        assert identity.agent_id == MODEL
        return 0

    stop.side_effect = _record
    resp = _send("DELETE", url)

    assert resp.status_code == 200, resp.text
    assert seen == [True]  # 先停指令，那時模型資料還在
    assert not folder.exists()


def test_the_models_tab_does_not_delete_a_personal_space(
    zone_data_root: Path, stop: AsyncMock
) -> None:
    """沒有 config.json 的是別的身分的個人空間，不是模型：名冊裡也沒有就 404，資料夾不動."""
    drive = zone_data_root / "custom" / "someone" / "drive"
    drive.mkdir(parents=True)
    (drive / "note.md").write_text("keep", encoding="utf-8")

    resp = _send("DELETE", "/v1/models/someone")

    assert resp.status_code == 404, resp.text
    assert (drive / "note.md").read_text(encoding="utf-8") == "keep"
    stop.assert_not_awaited()


def test_rename_stops_the_old_model_commands_first(zone_data_root: Path, stop: AsyncMock) -> None:
    """改名後用新 ID 停不到舊 ID 登記的指令，所以搬之前先停."""
    folder = _model(zone_data_root)
    seen: list[bool] = []

    async def _record(identity: AgentIdentity) -> int:
        seen.append(folder.exists())
        assert identity.agent_id == MODEL
        return 0

    stop.side_effect = _record
    resp = _send("POST", f"/v1/ui/custom_models/{MODEL}/rename", {"new_id": "M2"})

    assert resp.status_code == 200, resp.text
    assert seen == [True]
    assert (zone_data_root / "custom" / "M2" / "config.json").exists()
