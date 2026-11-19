"""AI 的檔案工具不能改任務看板（ADR-0324）.

看板 ``missions/current_mission.json`` 在代理人的個人空間；用檔案動詞直接改它，就能繞過看板規則
（禁令 30、31）並把 ``review_status`` 寫成 APPROVED 跳過互審（ADR-0137）。檔案動詞的寫入類動作碰
個人空間根目錄的 ``missions/`` 一律拒絕；讀照常可讀，系統經 ``MissionManager`` 照常寫看板。
終端與能寫個人空間的 MCP 伺服器那兩條路要等看板搬到系統資料夾（另一個任務）。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest
from memory_server.services import personal_catalog as pc
from memory_server.services.mission_manager import MISSIONS_DIR_NAME, MissionManager
from memory_server.tools import vault_tools as memory_vault_tools
from memory_server.utils.zone_paths import Anchor, agent_root
from src.models.identity import AgentIdentity
from src.services import tool_errors
from src.services.resource_manager import resource_manager
from src.tools import file_verbs
from src.tools import vault_tools as vault_bridge

if TYPE_CHECKING:
    from pathlib import Path

    from memory_server.utils.zone_paths import ZonePath

IDENTITY = AgentIdentity.for_role(agent_id="BoardAgent", role="Owner")
BOARD = '{"goal": "real board"}'


def _zone(root: Path) -> ZonePath:
    return agent_root(Anchor.PERSONAL_DRIVE, root.name)


class _Manager:
    """只提供個人空間根目錄的假 MemoryManager（vault 分發器只用到這個方法）。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def get_personal_drive_dir(self, identity: Any, resource_proxy: Any = None) -> ZonePath:
        _ = identity, resource_proxy
        return _zone(self.root)


async def _vault(root: Path, action: str, **kwargs: Any) -> Any:
    return await memory_vault_tools(
        vault="personal",
        action=action,
        identity=IDENTITY,
        manager=_Manager(root),
        resource_proxy=resource_manager,
        **kwargs,
    )


@pytest.fixture
def drive(zone_data_root: Path) -> Path:
    root = zone_data_root / "personal_drive" / "BoardAgent"
    (root / "missions").mkdir(parents=True)
    (root / "missions" / "current_mission.json").write_text(BOARD, encoding="utf-8")
    return root


def _board(drive: Path) -> str:
    return (drive / "missions" / "current_mission.json").read_text(encoding="utf-8")


def test_the_folder_name_is_shared_with_the_board():
    """拒寫的名稱和看板實際所在的資料夾是同一個常數."""
    assert MISSIONS_DIR_NAME == "missions"
    assert frozenset({MISSIONS_DIR_NAME}) == pc.SYSTEM_ROOT_DIRS


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        ("write", {"content": '{"goal": "forged", "review_status": "APPROVED"}'}),
        ("append", {"content": "x"}),
        ("str_replace", {"old_str": "real", "new_str": "forged"}),
        ("delete", {}),
    ],
)
@pytest.mark.parametrize(
    "target",
    [
        "missions/current_mission.json",
        "MISSIONS/current_mission.json",
        "missions/sub/x.json",
        "missions/new.json",
        "sub/../missions/current_mission.json",
    ],
)
async def test_write_actions_cannot_touch_the_board(
    drive: Path, action: str, extra: dict[str, Any], target: str
) -> None:
    with pytest.raises(pc.SystemFolderDeniedError):
        await _vault(drive, action, path=target, **extra)
    assert _board(drive) == BOARD
    assert sorted(p.name for p in (drive / "missions").iterdir()) == ["current_mission.json"]


async def test_an_absolute_path_into_the_board_is_refused(drive: Path) -> None:
    target = drive / "missions" / "current_mission.json"
    with pytest.raises(pc.SystemFolderDeniedError):
        await _vault(drive, "write", path=str(target), content="{}")
    assert _board(drive) == BOARD


@pytest.mark.parametrize("target", ["missionsx/a.md", "notes/missions/a.md", "missions.md"])
async def test_similar_names_are_still_writable(drive: Path, target: str) -> None:
    result = await _vault(drive, "write", path=target, content="hello")
    assert str(result).startswith("Success"), result
    assert (drive / target).read_text(encoding="utf-8") == "hello"


async def test_the_board_can_still_be_read(drive: Path) -> None:
    result = await _vault(drive, "read", path="missions/current_mission.json")
    assert "real board" in str(result)


@pytest.mark.parametrize(
    ("verb", "extra"),
    [
        ("write", {"content": "{}"}),
        ("edit", {"old_str": "real", "new_str": "forged"}),
        ("insert", {"line": 1, "text": "forged"}),
        ("delete", {}),
    ],
)
async def test_file_verbs_get_a_structured_permission_error(
    drive: Path, verb: str, extra: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """經檔案動詞與 Proxy vault 橋接層：結構化 permission_denied.

    訊息與建議講看板，不講 `.touka`。
    """
    handler = getattr(file_verbs, f"verb_{verb}")
    caplog.set_level(logging.WARNING, logger=vault_bridge.__name__)

    result = await handler(
        scope="personal",
        path="missions/current_mission.json",
        identity=IDENTITY,
        manager=_Manager(drive),
        **extra,
    )

    assert tool_errors.is_tool_error(result)
    assert tool_errors.extract_error_class(result) == tool_errors.PERMISSION_DENIED
    body = json.loads(result)
    assert body["message"] == str(pc.SystemFolderDeniedError(MISSIONS_DIR_NAME))
    assert "mission_tools" in body["message"]
    assert ".touka" not in json.dumps(body, ensure_ascii=False)
    assert _board(drive) == BOARD
    bridge_logs = [r for r in caplog.records if r.name == vault_bridge.__name__]
    assert [(r.levelno, r.exc_info) for r in bridge_logs] == [(logging.WARNING, None)]


async def test_the_system_still_saves_the_board(zone_data_root: Path) -> None:
    """系統自己經 MissionManager 寫看板不受影響（不經檔案動詞）."""
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
    manager = MissionManager()
    mission = await manager.initialize_mission(identity=identity, goal="board still works")
    loaded = await manager.get_current_mission(identity)
    assert loaded is not None
    assert loaded.goal == mission.goal
