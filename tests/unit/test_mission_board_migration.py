"""舊任務看板只搬一次（ADR-0325）.

升級後第一次啟動、外部 MCP 伺服器開起來之前，把每個代理人個人空間裡的
``missions/current_mission.json`` 搬到 ``<data>/missions/<雜湊>.json``，確認寫好後刪舊檔，
寫下「搬過了」的記號；之後舊位置的看板不會再被搬、也不會被讀。

- 內容不合看板規則（直接改檔才做得出來的）、內容的 ``agent_id`` 是別的代理人、讀不到、是連結：
  略過、記 ERROR（只有編號與原因）、舊檔留著，記號照寫。
- 候選編號列不完整：這次不寫記號，下次啟動再來。
- 候選順序：靈魂的正式 ID、系統與登記表的編號、``custom/`` 的資料夾名稱、最後才是只出現在
  個人空間的名稱。

data 根換到 ``zone_data_root``（``tmp_path``），不碰 ``data/``。
"""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from memory_server.models.mission import MissionState
from memory_server.services.mission_manager import (
    LegacyImport,
    MissionManager,
    mission_board_path,
)
from src import main as main_module
from src.models.identity import AgentIdentity
from src.services import mission_boards
from src.services.agent_service import RegistryUnreadableError
from src.services.resource_manager import resource_manager

from tests.support.links import make_dir_link
from tests.support.zone_records import expect_zone_refusal

if TYPE_CHECKING:
    from pathlib import Path

CANARY = "CANARY board content"


def _board(agent_id: str, **task: Any) -> str:
    """平常經 MissionManager 寫出來的樣子（``model_dump_json(indent=4)``）."""
    state = MissionState.model_validate(
        {
            "mission_id": f"m-{agent_id}",
            "goal": CANARY,
            "agent_id": agent_id,
            "tasks": [{"id": "t1", "description": "task", **task}],
            "created_at": 1.0,
            "updated_at": 2.0,
        }
    )
    return state.model_dump_json(indent=4)


def _soul(data: Path, agent_id: str) -> Path:
    """靈魂資料夾與它的個人空間；回個人空間."""
    (data / "soul" / agent_id).mkdir(parents=True)
    (data / "soul" / agent_id / "config.json").write_text(
        json.dumps({"is_toukasoul": True}), encoding="utf-8"
    )
    drive = data / "personal_drive" / agent_id
    drive.mkdir(parents=True)
    return drive


def _model(data: Path, agent_id: str) -> Path:
    """自訂模型資料夾與它的個人空間；回個人空間."""
    (data / "custom" / agent_id / "drive").mkdir(parents=True)
    (data / "custom" / agent_id / "config.json").write_text("{}", encoding="utf-8")
    return data / "custom" / agent_id / "drive"


def _put_legacy(drive: Path, content: str) -> Path:
    (drive / "missions").mkdir(exist_ok=True)
    legacy = drive / "missions" / "current_mission.json"
    legacy.write_text(content, encoding="utf-8")
    return legacy


def _new(agent_id: str) -> str | None:
    path = mission_board_path(agent_id)
    return path.read_text(encoding="utf-8") if path.exists() else None


def _done() -> bool:
    return MissionManager().legacy_import_done()


# --------------------------------------------------------------------------- #
# 1. 正常搬一次
# --------------------------------------------------------------------------- #


async def test_boards_move_once_and_the_old_files_go(zone_data_root: Path) -> None:
    soul_legacy = _put_legacy(_soul(zone_data_root, "SoulOne"), _board("SoulOne"))
    model_legacy = _put_legacy(_model(zone_data_root, "ModelOne"), _board("ModelOne"))

    await mission_boards.import_legacy_boards()

    assert _new("SoulOne") == _board("SoulOne")
    assert _new("ModelOne") == _board("ModelOne")
    assert not soul_legacy.exists()
    assert not model_legacy.exists()
    assert _done()
    loaded = await MissionManager().get_current_mission(
        AgentIdentity.for_role(agent_id="SoulOne", role="Owner")
    )
    assert loaded is not None
    assert loaded.goal == CANARY


async def test_nothing_moves_once_the_marker_is_there(zone_data_root: Path) -> None:
    MissionManager().mark_legacy_import_done()
    legacy = _put_legacy(_soul(zone_data_root, "SoulLate"), _board("SoulLate"))

    await mission_boards.import_legacy_boards()

    assert _new("SoulLate") is None
    assert legacy.exists()


async def test_an_existing_new_board_is_not_overwritten(zone_data_root: Path) -> None:
    legacy = _put_legacy(_soul(zone_data_root, "SoulKeep"), _board("SoulKeep"))
    owner = AgentIdentity.for_role(agent_id="SoulKeep", role="Owner")
    await MissionManager().initialize_mission(owner, "already moved")
    before = _new("SoulKeep")

    await mission_boards.import_legacy_boards()

    assert _new("SoulKeep") == before
    assert legacy.exists()
    assert _done()


# --------------------------------------------------------------------------- #
# 2. 不合規則、別人的、讀不到：略過、記 ERROR、舊檔留著、記號照寫
# --------------------------------------------------------------------------- #

BAD_BOARDS = {
    "broken_json": "{not json " + CANARY,
    "done_without_review": _board(
        "SoulBad", status="DONE", review_required=True, review_status=None
    ),
    "locked_command_stripped": _board(
        "SoulBad", verification_cmd_locked=True, verification_cmd=None
    ),
}


@pytest.mark.parametrize("content", BAD_BOARDS.values(), ids=BAD_BOARDS.keys())
async def test_a_bad_board_is_skipped(
    zone_data_root: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = _put_legacy(_soul(zone_data_root, "SoulBad"), content)
    caplog.set_level(logging.ERROR)

    await mission_boards.import_legacy_boards()

    assert _new("SoulBad") is None
    assert legacy.read_text(encoding="utf-8") == content
    assert _done()
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("SoulBad" in message for message in errors), errors
    assert not any(CANARY in message for message in errors)


async def test_a_linked_board_folder_is_skipped(
    zone_data_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    drive = _soul(zone_data_root, "SoulLink")
    fake = tmp_path.resolve() / "fake"
    fake.mkdir()
    (fake / "current_mission.json").write_text(_board("SoulLink"), encoding="utf-8")
    make_dir_link(drive / "missions", fake)
    caplog.set_level(logging.ERROR)

    with expect_zone_refusal("ZoneLinkRefusedError"):
        await mission_boards.import_legacy_boards()

    assert _new("SoulLink") is None
    assert (fake / "current_mission.json").exists()
    assert _done()
    assert any("SoulLink" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


async def test_an_agent_id_that_differs_only_in_case_is_rewritten(zone_data_root: Path) -> None:
    """以前在 Windows 上用小寫叫到時寫進去的 ``agent_id``：改成匯入的編號.

    互審「不能自己簽自己」看的就是它。
    """
    legacy = _put_legacy(_soul(zone_data_root, "SoulCase"), _board("soulcase"))

    await mission_boards.import_legacy_boards()

    moved = json.loads(_new("SoulCase"))
    assert moved["agent_id"] == "SoulCase"
    assert not legacy.exists()


async def test_a_board_saved_before_a_rename_moves_under_the_new_id(
    zone_data_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """舊版改名只搬資料夾，看板裡還是舊編號：照樣搬、改成現在的編號，不當成別人的看板丟掉."""
    legacy = _put_legacy(_model(zone_data_root, "RenamedNew"), _board("RenamedOld"))
    caplog.set_level(logging.INFO)

    await mission_boards.import_legacy_boards()

    moved = json.loads(_new("RenamedNew"))
    assert (moved["agent_id"], moved["goal"]) == ("RenamedNew", CANARY)
    assert _new("RenamedOld") is None
    assert not legacy.exists()
    assert not any(CANARY in r.getMessage() for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_a_read_back_mismatch_keeps_the_old_board(
    zone_data_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = _put_legacy(_soul(zone_data_root, "SoulMismatch"), _board("SoulMismatch"))
    target = str(mission_board_path("SoulMismatch"))
    real_read = resource_manager.read_file_text_no_follow

    def _read(path: object, encoding: str = "utf-8") -> str:
        text = real_read(path, encoding=encoding)
        return text + " " if str(path) == target else text

    monkeypatch.setattr(resource_manager, "read_file_text_no_follow", _read)
    caplog.set_level(logging.ERROR)

    await mission_boards.import_legacy_boards()

    assert _new("SoulMismatch") is None
    assert legacy.exists()
    assert _done()
    assert any(
        "SoulMismatch" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
    )


# --------------------------------------------------------------------------- #
# 3. 候選列不完整：不寫記號，下次再來
# --------------------------------------------------------------------------- #


def _boom(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError


def _unreadable_registry(*_args: Any, **_kwargs: Any) -> Any:
    raise RegistryUnreadableError


LISTINGS = {
    "souls": ("_soul_ids", _boom),
    "registry": ("_registry_ids", _unreadable_registry),
    "custom": ("_custom_names", _boom),
    "personal_space": ("_personal_names", _boom),
}


@pytest.mark.parametrize(("name", "failing"), LISTINGS.values(), ids=LISTINGS.keys())
async def test_an_incomplete_listing_leaves_the_marker_unwritten(
    zone_data_root: Path, monkeypatch: pytest.MonkeyPatch, name: str, failing: Any
) -> None:
    legacy = _put_legacy(_model(zone_data_root, "ModelLater"), _board("ModelLater"))
    with monkeypatch.context() as patch:
        patch.setattr(mission_boards, name, failing)
        await mission_boards.import_legacy_boards()
    assert not _done()

    await mission_boards.import_legacy_boards()
    assert _done()
    assert _new("ModelLater") == _board("ModelLater")
    assert not legacy.exists()


# --------------------------------------------------------------------------- #
# 4. 候選：系統與登記表的編號、順序
# --------------------------------------------------------------------------- #


async def test_a_system_id_set_as_owner_has_its_board_moved(
    zone_data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """系統編號被設成 Owner 時看板在個人空間、不在靈魂清單上，照樣搬."""
    drive = zone_data_root / "personal_drive" / "DebugAgent"
    drive.mkdir(parents=True)
    legacy = _put_legacy(drive, _board("DebugAgent"))
    real = mission_boards.identity_service.get_identity

    def _identity(agent_id: str) -> AgentIdentity:
        if agent_id == "DebugAgent":
            return AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
        return real(agent_id)

    monkeypatch.setattr(mission_boards.identity_service, "get_identity", _identity)

    await mission_boards.import_legacy_boards()

    assert _new("DebugAgent") == _board("DebugAgent")
    assert not legacy.exists()


def test_candidates_come_in_order(zone_data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _soul(zone_data_root, "Zed")
    _model(zone_data_root, "Amy")
    (zone_data_root / "personal_drive" / "Bob").mkdir(parents=True)
    monkeypatch.setattr(mission_boards, "_registry_ids", lambda: ["Amy", "Reg", "bad/name"])

    ids, complete = mission_boards.legacy_candidates()

    assert complete
    # 靈魂、系統、登記表、custom/、只出現在個人空間的；去重，不是單一段的略過
    assert ids == ["Zed", "DebugAgent", "DispatchAgent", "Amy", "Reg", "Bob"]


@pytest.mark.skipif(sys.platform != "win32", reason="不分大小寫的磁碟才找得到別的大小寫")
async def test_a_personal_space_made_with_another_case_goes_to_the_canonical_id(
    zone_data_root: Path,
) -> None:
    """舊版在 Windows 上可能用小寫建出個人空間：搬給靈魂的正式 ID，不給小寫的寫法."""
    (zone_data_root / "soul" / "SoulWin").mkdir(parents=True)
    (zone_data_root / "soul" / "SoulWin" / "config.json").write_text(
        json.dumps({"is_toukasoul": True}), encoding="utf-8"
    )
    drive = zone_data_root / "personal_drive" / "soulwin"
    drive.mkdir(parents=True)
    _put_legacy(drive, _board("SoulWin"))

    await mission_boards.import_legacy_boards()

    assert _new("SoulWin") == _board("SoulWin")
    assert _new("soulwin") is None


async def test_on_a_case_insensitive_disk_the_souls_id_asks_first(
    zone_data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux 也驗得到上一個測試的關鍵：不分大小寫的磁碟上兩種寫法找到同一份舊檔、先問的搬走，
    所以搬的時候要先用靈魂的正式 ID 問，再問個人空間裡的小寫名稱."""
    _ = zone_data_root
    asked: list[str] = []

    class _RecordingManager:
        def legacy_import_done(self) -> bool:
            return False

        def mark_legacy_import_done(self) -> None:
            return None

        async def import_legacy_board(self, identity: AgentIdentity) -> LegacyImport:
            asked.append(identity.agent_id)
            return LegacyImport.ABSENT

    monkeypatch.setattr(mission_boards, "MissionManager", _RecordingManager)
    monkeypatch.setattr(mission_boards, "_soul_ids", lambda: ["SoulWin"])
    monkeypatch.setattr(mission_boards, "_registry_ids", list)
    monkeypatch.setattr(mission_boards, "_custom_names", list)
    monkeypatch.setattr(mission_boards, "_personal_names", lambda: ["soulwin"])

    await mission_boards.import_legacy_boards()

    assert [a for a in asked if a.casefold() == "soulwin"] == ["SoulWin", "soulwin"]


# --------------------------------------------------------------------------- #
# 5. 啟動：在外部 MCP 伺服器開起來之前、失敗不擋啟動
# --------------------------------------------------------------------------- #


def test_startup_imports_before_mcp_servers_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _import() -> None:
        calls.append("import")

    real_initialize = main_module.protocol_manager.initialize_all

    async def _initialize(*args: Any, **kwargs: Any) -> Any:
        calls.append("mcp")
        return await real_initialize(*args, **kwargs)

    monkeypatch.setattr(main_module, "import_legacy_boards", _import)
    monkeypatch.setattr(main_module.protocol_manager, "initialize_all", _initialize)
    with TestClient(main_module.app):
        pass
    assert calls[:2] == ["import", "mcp"]


def test_startup_survives_a_failing_import(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom_import() -> None:
        raise OSError

    monkeypatch.setattr(main_module, "import_legacy_boards", _boom_import)
    with TestClient(main_module.app) as client:
        assert client.app.state.dispatch_supervisor is not None
