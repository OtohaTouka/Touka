"""任務看板不跟隨連結，鎖檔不放在代理人資料夾（ADR-0293）。

代理人的指令可以把自己的個人空間或其中的 ``missions/`` 換成連結，指到假的設定資料夾或別的
代理人的個人空間。看板的讀寫不能經過連結；工具回結構化 ``permission_denied``，Dispatch 回固定
訊息，聊天介面的看板端點當作沒有任務。鎖檔放在 ``<data>/temp/locks/missions/<id>.lock``，
代理人寫不到。

經 Proxy 的 ``mission_tools``、ToukaDispatch 與 ``/v1/ui/mission`` 跑，用真的 ``MissionManager``
與 ``ResourceManager``；data 根換到 ``zone_data_root``（``tmp_path``），不碰 ``data/``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from memory_server.services.mission_manager import MissionManager
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.ui_sessions import _require_authenticated
from src.tools.mission_tools import mission_tools

from tests.support.links import LINK_FOLLOWED, make_dir_link
from tests.support.zone_records import expect_zone_refusal
from ToukaDispatch import server as dispatch_server

ALICE = AgentIdentity.for_role(agent_id="Alice", role="Owner")
M1 = AgentIdentity.for_role(agent_id="M1", role="Base")
CANARY = "CANARY goal"
BOARD = json.dumps(
    {
        "mission_id": "planted",
        "goal": CANARY,
        "agent_id": "Alice",
        "tasks": [{"id": "t1", "description": CANARY}],
        "created_at": 0.0,
        "updated_at": 0.0,
    }
)


def _fake_folder(folder: Path, *, board_below: str) -> Path:
    """假的資料夾：裡面有一份看板（連結被跟隨時就讀得到）和假的設定檔。"""
    (folder / board_below).mkdir(parents=True)
    (folder / board_below / "current_mission.json").write_text(BOARD, encoding="utf-8")
    (folder / "hooks.json").write_text('{"hooks": "' + CANARY + '"}', encoding="utf-8")
    return folder


def _snapshot(folder: Path) -> dict[str, bytes | None]:
    return {
        p.relative_to(folder).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in folder.rglob("*")
    }


@pytest.fixture
def world(zone_data_root: Path, tmp_path: Path) -> dict[str, Path]:
    """Alice、M1 與 DispatchAgent 的個人空間，以及 data 外面的假資料夾。"""
    alice = zone_data_root / "personal_drive" / "Alice"
    alice.mkdir(parents=True)
    (zone_data_root / "custom" / "M1" / "drive").mkdir(parents=True)
    (zone_data_root / "custom" / "DispatchAgent" / "drive").mkdir(parents=True)
    bob = zone_data_root / "personal_drive" / "Bob"
    return {
        "data": zone_data_root,
        "alice": alice,
        "m1_drive": zone_data_root / "custom" / "M1" / "drive",
        "dispatch_drive": zone_data_root / "custom" / "DispatchAgent" / "drive",
        "alice_missions": alice / "missions",
        "fake_config": _fake_folder(tmp_path.resolve() / "fake_config", board_below="missions"),
        "bob": _fake_folder(bob, board_below="missions"),
        "fake_missions": _fake_folder(tmp_path.resolve() / "fake_missions", board_below="."),
    }


def _swap(world: dict[str, Path], key: str, target: str) -> Path:
    folder = world[key]
    if folder.exists():
        folder.rename(folder.with_name(folder.name + "_old"))
    make_dir_link(folder, world[target])
    return world[target]


ACTIONS: dict[str, dict[str, Any]] = {
    "initialize": {"goal": "new goal", "tasks": [{"task_id": "n1", "description": "new"}]},
    "get_board": {},
    "update_status": {"task_id": "t1", "status": "DOING"},
}
# （誰, 換成連結的資料夾, 連結另一端）
SWAPS = {
    "drive_to_config": (ALICE, "alice", "fake_config"),
    "drive_to_bob": (ALICE, "alice", "bob"),
    "custom_drive_to_config": (M1, "m1_drive", "fake_config"),
    "missions_to_planted_board": (ALICE, "alice_missions", "fake_missions"),
}


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("case", SWAPS.values(), ids=SWAPS.keys())
async def test_a_board_folder_swapped_to_a_link_is_refused(
    world: dict[str, Path], case: tuple[AgentIdentity, str, str], action: str
) -> None:
    """換成連結的個人空間或 missions/：讀不到另一端的看板、也寫不進去，回 permission_denied。"""
    identity, key, target_key = case
    target = _swap(world, key, target_key)
    before = _snapshot(target)
    with expect_zone_refusal("ZoneLinkRefusedError"):
        output = await mission_tools(action, identity=identity, **ACTIONS[action])
    assert _snapshot(target) == before, LINK_FOLLOWED
    assert CANARY not in output, LINK_FOLLOWED
    result = json.loads(output)
    assert result["error_class"] == "permission_denied"
    assert str(target) not in output


async def test_the_lock_file_is_outside_the_agent_folder(world: dict[str, Path]) -> None:
    """看板照常建立；鎖檔在 ``temp/locks/missions``，代理人的 missions/ 裡只有看板。"""
    output = await mission_tools("initialize", identity=ALICE, **ACTIONS["initialize"])
    assert output.startswith("✅")
    # Windows 放開鎖時會刪掉鎖檔，所以看鎖的路徑
    lock_file = MissionManager()._get_lock("Alice")._file_lock.lock_file
    assert Path(lock_file) == world["data"] / "temp" / "locks" / "missions" / "Alice.lock"
    assert sorted(p.name for p in world["alice_missions"].iterdir()) == ["current_mission.json"]
    board = await mission_tools("get_board", identity=ALICE)
    assert "new goal" in board


async def test_dispatch_returns_a_fixed_message_for_a_linked_board(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """DispatchAgent 的個人空間換成連結：固定訊息、不發 token，另一端沒變。"""
    session_manager = type(dispatch_server._session_mgr)()
    monkeypatch.setattr(dispatch_server, "_session_mgr", session_manager)
    session_token = session_manager.create_session({"name": "synthetic-internal"})
    rotate = Mock(return_value="synthetic-issued-credential")
    monkeypatch.setattr(session_manager, "rotate_mission_token", rotate)
    target = _swap(world, "dispatch_drive", "fake_config")
    before = _snapshot(target)
    with expect_zone_refusal("ZoneLinkRefusedError"):
        output = await dispatch_server.dispatch_initialize_mission(session_token, "dispatch goal")
    assert _snapshot(target) == before, LINK_FOLLOWED
    assert output == f"Error: {dispatch_server.BOARD_UNAVAILABLE}"
    rotate.assert_not_called()


def test_the_board_endpoint_treats_a_linked_board_as_no_mission(world: dict[str, Path]) -> None:
    """聊天介面的看板端點：連結另一端的看板不會被送出，當作沒有任務。"""
    target = _swap(world, "alice", "fake_config")
    before = _snapshot(target)
    app.dependency_overrides[_require_authenticated] = lambda: ALICE
    try:
        with expect_zone_refusal("ZoneLinkRefusedError"):
            resp = TestClient(app, raise_server_exceptions=False).get("/v1/ui/mission")
    finally:
        app.dependency_overrides.clear()
    assert _snapshot(target) == before, LINK_FOLLOWED
    assert CANARY not in resp.text, LINK_FOLLOWED
    assert resp.status_code == 404, resp.text
