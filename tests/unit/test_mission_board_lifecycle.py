"""任務看板跟著代理人走（ADR-0325）.

看板以前在個人空間裡，刪除、改名、重置、備份、匯出都「順便」處理了它；搬到 ``<data>/missions``
之後要一個一個接上：

- 刪除靈魂或自訂模型時刪看板；刪不掉就回錯誤、儲存與註冊不刪。和靈魂同名的舊自訂模型刪除時
  不碰靈魂的看板，只差大小寫的舊模型刪的是它自己的看板。
- 自訂模型改名時看板一起搬；搬不了就把資料夾與 config 改回。
- 建立新代理人時，在所有會拒絕的檢查都通過之後清掉不屬於任何代理人的殘留看板；編號屬於別的
  身分而且它有看板時，建立或改名自訂模型回「已存在」。判斷讀不到資料時不建立。
- 重置清新看板與個人空間的舊資料夾；全系統備份包含看板；個人資料匯出包含每個代理人的看板。

data 根換到 ``zone_data_root``（``tmp_path``），設定檔在 conftest 測試沙盒，不碰 ``data/``。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from memory_server.services.mission_manager import MissionManager, mission_board_path
from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.models.user import User
from src.services import gdpr_export_service as gx
from src.services import mission_boards
from src.services.agent_reset import primitives as P
from src.services.agent_service import RegistryUnreadableError
from src.services.auth_service import AuthService
from src.services.custom_model_service import custom_model_service
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.resource_manager import resource_manager
from src.services.user_service import UserService

if TYPE_CHECKING:
    from collections.abc import Iterator

ADMIN = AgentIdentity.for_role("board-life-admin", "admin", is_human=True)
MODEL_BODY = {"name": "x", "description": "", "systemPrompt": "PROMPT", "config": {}}
_SCRIPT = Path(__file__).resolve().parents[2] / "ToukaProxy" / "scripts" / "manage_backup.py"


# --------------------------------------------------------------------------- #
# 共用
# --------------------------------------------------------------------------- #


SANDBOX_SOULS = (
    "LifeSoul", "KeptSoul", "FreshSoul", "ExistingSoul", "EditSoul", "EditBad", "ConvSoul",
    "UnsureSoul",
)


@pytest.fixture
def sandbox() -> Iterator[Path]:
    """靈魂路由要 conftest 測試沙盒的對話資料庫（``zone_data_root`` 沒有），data 根用沙盒.

    沙盒每個測試清 ``soul/``；這裡另外清本檔靈魂的看板、個人空間與 legacy。
    """
    data = Path(resource_manager.get_base_path())
    yield data
    for agent_id in SANDBOX_SOULS:
        mission_board_path(agent_id).unlink(missing_ok=True)
        for base in ("personal_drive", "legacy"):
            shutil.rmtree(data / base / agent_id, ignore_errors=True)


@pytest.fixture(autouse=True)
def data_root(request: pytest.FixtureRequest) -> Path:
    """用了 ``sandbox`` 的測試在沙盒，其他測試的 data 根換到 ``zone_data_root``."""
    if "sandbox" in request.fixturenames:
        return request.getfixturevalue("sandbox")
    return request.getfixturevalue("zone_data_root")


@pytest.fixture(autouse=True)
def registry(data_root: Path) -> Iterator[Path]:
    """models.json、agents_registry.json 每個測試從空的開始，結束時還原."""
    _ = data_root
    config = Path(get_config_dir())
    files = [config / "models.json", config / "agents_registry.json"]
    original = {f: (f.read_text(encoding="utf-8") if f.exists() else None) for f in files}
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}", encoding="utf-8")
    model_manager.load_initial_config()
    yield config / "agents_registry.json"
    for f, content in original.items():
        if content is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(content, encoding="utf-8")


@pytest.fixture
def owners(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """自訂模型路由的擁有清單換成 MagicMock（只記錄呼叫）."""
    users = MagicMock()
    monkeypatch.setattr("src.routers.custom_models.user_service", users)
    return users


@pytest.fixture
def admin() -> Iterator[tuple[AgentIdentity, str]]:
    """真的 admin 帳號（刪除與用編輯建立靈魂要重新輸入密碼）."""
    pw = "BoardLifePass123!"
    auth = AuthService()
    user = auth.create_user("board-life-admin@test.com", pw, role="admin")
    yield AgentIdentity.for_role(user.id, "admin", is_human=True), pw
    UserService(auth).delete_user(user.id)


def _send(method: str, url: str, body: dict[str, Any] | None, who: AgentIdentity = ADMIN) -> Any:
    app.dependency_overrides[get_verified_identity] = lambda: who
    try:
        return TestClient(app, raise_server_exceptions=False).request(method, url, json=body)
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def _code(response: Any) -> str:
    return response.json()["detail"]["code"]


async def _put_board(agent_id: str, goal: str) -> None:
    identity = AgentIdentity.for_role(agent_id=agent_id, role="Base")
    await MissionManager().initialize_mission(identity, goal)


def _goal(agent_id: str) -> str | None:
    path = mission_board_path(agent_id)
    return json.loads(path.read_text(encoding="utf-8"))["goal"] if path.exists() else None


def _register(registry: Path, agent_id: str) -> None:
    data = json.loads(registry.read_text(encoding="utf-8"))
    data[agent_id] = [agent_id]
    registry.write_text(json.dumps(data), encoding="utf-8")


def _soul_folder(data: Path, agent_id: str, *, is_soul: bool = True) -> None:
    (data / "soul" / agent_id).mkdir(parents=True)
    (data / "soul" / agent_id / "config.json").write_text(
        json.dumps({"is_toukasoul": is_soul}), encoding="utf-8"
    )


def _model_folder(data: Path, agent_id: str) -> None:
    (data / "custom" / agent_id / "drive").mkdir(parents=True)
    (data / "custom" / agent_id / "config.json").write_text(
        json.dumps({"id": agent_id}), encoding="utf-8"
    )


def _create_soul(agent_id: str) -> Any:
    return _send("POST", "/v1/souls", {"agent_id": agent_id, "config": {"base_model": None}})


def _fail(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError


async def _async_fail(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError


# --------------------------------------------------------------------------- #
# 1. 刪除
# --------------------------------------------------------------------------- #


async def test_deleting_a_soul_deletes_its_board(
    sandbox: Path, admin: tuple[AgentIdentity, str]
) -> None:
    _ = sandbox
    who, password = admin
    assert _send("POST", "/v1/souls", {"agent_id": "LifeSoul", "config": {}}, who).is_success
    await _put_board("LifeSoul", "soul goal")
    response = _send("DELETE", "/v1/souls/LifeSoul", {"admin_password": password}, who)
    assert response.status_code == 200, response.text
    assert _goal("LifeSoul") is None


async def test_a_soul_whose_board_cannot_be_deleted_is_kept(
    sandbox: Path, admin: tuple[AgentIdentity, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    who, password = admin
    assert _send("POST", "/v1/souls", {"agent_id": "KeptSoul", "config": {}}, who).is_success
    await _put_board("KeptSoul", "kept")
    monkeypatch.setattr(MissionManager, "delete_board", _async_fail)
    response = _send("DELETE", "/v1/souls/KeptSoul", {"admin_password": password}, who)
    assert response.status_code == 500, response.text
    assert _code(response) == ErrorCode.SOUL_VAULT_ERROR.value
    assert (sandbox / "soul" / "KeptSoul").is_dir()
    assert _goal("KeptSoul") == "kept"


async def test_deleting_a_model_deletes_its_board(owners: MagicMock) -> None:
    _ = owners
    assert _send("POST", "/v1/ui/custom_models/LifeModel", MODEL_BODY).status_code == 200
    await _put_board("LifeModel", "model goal")
    assert _send("DELETE", "/v1/ui/custom_models/LifeModel", None).status_code == 200
    assert _goal("LifeModel") is None


async def test_a_model_whose_board_cannot_be_deleted_is_kept(
    zone_data_root: Path, owners: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _send("POST", "/v1/ui/custom_models/KeptModel", MODEL_BODY).status_code == 200
    await _put_board("KeptModel", "kept")
    owners.reset_mock()
    monkeypatch.setattr(MissionManager, "delete_board", _async_fail)
    response = _send("DELETE", "/v1/ui/custom_models/KeptModel", None)
    assert response.status_code == 500, response.text
    assert _code(response) == ErrorCode.MODEL_DELETE_FAILED.value
    assert (zone_data_root / "custom" / "KeptModel" / "config.json").is_file()
    assert _goal("KeptModel") == "kept"
    owners.remove_owned_agent_from_all.assert_not_called()


async def test_deleting_an_old_model_named_exactly_like_a_soul_keeps_the_souls_board(
    zone_data_root: Path, owners: MagicMock
) -> None:
    """ADR-0245 之前留下的同名資料夾：看板是靈魂的，不刪."""
    _ = owners
    _soul_folder(zone_data_root, "TwinSoul")
    _model_folder(zone_data_root, "TwinSoul")
    await _put_board("TwinSoul", "the soul's board")
    response = _send("DELETE", "/v1/ui/custom_models/TwinSoul", None)
    assert response.status_code == 200, response.text
    assert _goal("TwinSoul") == "the soul's board"


async def test_deleting_an_old_model_differing_in_case_deletes_only_its_own_board(
    zone_data_root: Path, owners: MagicMock
) -> None:
    _ = owners
    _soul_folder(zone_data_root, "CaseSoul")
    _model_folder(zone_data_root, "casesoul")
    await _put_board("CaseSoul", "soul")
    await _put_board("casesoul", "model")
    response = _send("DELETE", "/v1/ui/custom_models/casesoul", None)
    assert response.status_code == 200, response.text
    assert _goal("casesoul") is None
    assert _goal("CaseSoul") == "soul"


# --------------------------------------------------------------------------- #
# 2. 改名
# --------------------------------------------------------------------------- #


def _rename(old: str, new: str) -> Any:
    return _send("POST", f"/v1/ui/custom_models/{old}/rename", {"new_id": new})


def _stored_id(data: Path, model_id: str) -> str:
    return json.loads((data / "custom" / model_id / "config.json").read_text(encoding="utf-8"))[
        "id"
    ]


async def test_renaming_a_model_moves_its_board(owners: MagicMock) -> None:
    _ = owners
    assert _send("POST", "/v1/ui/custom_models/RenOld", MODEL_BODY).status_code == 200
    await _put_board("RenOld", "moving")
    response = _rename("RenOld", "RenNew")
    assert response.status_code == 200, response.text
    assert _goal("RenOld") is None
    assert _goal("RenNew") == "moving"


async def test_renaming_only_the_case_moves_the_board(owners: MagicMock) -> None:
    _ = owners
    assert _send("POST", "/v1/ui/custom_models/CaseRen", MODEL_BODY).status_code == 200
    await _put_board("CaseRen", "case")
    response = _rename("CaseRen", "caseren")
    assert response.status_code == 200, response.text
    assert _goal("CaseRen") is None
    assert _goal("caseren") == "case"


async def test_a_board_that_cannot_move_puts_the_model_back(
    zone_data_root: Path, owners: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _send("POST", "/v1/ui/custom_models/StayOld", MODEL_BODY).status_code == 200
    await _put_board("StayOld", "stay")
    owners.reset_mock()
    monkeypatch.setattr(MissionManager, "move_board", _async_fail)
    response = _rename("StayOld", "StayNew")
    assert response.status_code == 500, response.text
    assert _stored_id(zone_data_root, "StayOld") == "StayOld"
    assert not (zone_data_root / "custom" / "StayNew").exists()
    assert _goal("StayOld") == "stay"
    owners.rename_owned_agent.assert_not_called()


async def test_a_failed_folder_rename_leaves_the_boards_alone(
    owners: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = owners
    assert _send("POST", "/v1/ui/custom_models/FolderOld", MODEL_BODY).status_code == 200
    await _put_board("FolderOld", "old")
    await _put_board("FolderNew", "leftover")
    monkeypatch.setattr(custom_model_service, "rename_model", _fail)
    response = _rename("FolderOld", "FolderNew")
    assert response.status_code == 500, response.text
    assert _goal("FolderOld") == "old"
    assert _goal("FolderNew") == "leftover"


async def test_an_ownership_failure_moves_the_board_back(owners: MagicMock) -> None:
    assert _send("POST", "/v1/ui/custom_models/OwnOld", MODEL_BODY).status_code == 200
    await _put_board("OwnOld", "own")
    owners.rename_owned_agent.side_effect = RuntimeError
    response = _rename("OwnOld", "OwnNew")
    assert response.status_code == 500, response.text
    assert _goal("OwnOld") == "own"
    assert _goal("OwnNew") is None


async def test_renaming_onto_a_leftover_board_replaces_it(owners: MagicMock) -> None:
    _ = owners
    assert _send("POST", "/v1/ui/custom_models/LeftOld", MODEL_BODY).status_code == 200
    await _put_board("LeftOld", "mine")
    await _put_board("LeftNew", "leftover")
    assert _rename("LeftOld", "LeftNew").status_code == 200
    assert _goal("LeftNew") == "mine"


async def test_renaming_onto_an_id_with_another_identitys_board_is_refused(
    zone_data_root: Path, owners: MagicMock, registry: Path
) -> None:
    _ = owners
    assert _send("POST", "/v1/ui/custom_models/ClaimOld", MODEL_BODY).status_code == 200
    await _put_board("ClaimOld", "mine")
    _register(registry, "ClaimHolder")
    await _put_board("ClaimHolder", "theirs")
    response = _rename("ClaimOld", "ClaimHolder")
    assert response.status_code == 409, response.text
    assert _code(response) == ErrorCode.MODEL_ALREADY_EXISTS.value
    assert _goal("ClaimOld") == "mine"
    assert _goal("ClaimHolder") == "theirs"
    assert (zone_data_root / "custom" / "ClaimOld").is_dir()


# --------------------------------------------------------------------------- #
# 3. 建立：清殘留看板（所有拒絕檢查之後）、別的身分的看板不接手
# --------------------------------------------------------------------------- #


async def test_creating_a_soul_clears_a_leftover_board(sandbox: Path) -> None:
    _ = sandbox
    await _put_board("FreshSoul", "leftover")
    assert _create_soul("FreshSoul").status_code == 200
    assert _goal("FreshSoul") is None


async def test_creating_a_soul_with_an_existing_souls_id_keeps_its_board(
    sandbox: Path,
) -> None:
    _soul_folder(sandbox, "ExistingSoul")
    await _put_board("ExistingSoul", "live board")
    response = _create_soul("ExistingSoul")
    assert response.status_code == 409, response.text
    assert _goal("ExistingSoul") == "live board"


async def test_creating_a_model_clears_a_leftover_board(owners: MagicMock) -> None:
    _ = owners
    await _put_board("FreshModel", "leftover")
    assert _send("POST", "/v1/ui/custom_models/FreshModel", MODEL_BODY).status_code == 200
    assert _goal("FreshModel") is None


async def test_creating_a_model_over_another_identitys_board_is_refused(
    zone_data_root: Path, owners: MagicMock, registry: Path
) -> None:
    _ = owners
    _register(registry, "RegHolder")
    await _put_board("RegHolder", "theirs")
    response = _send("POST", "/v1/ui/custom_models/RegHolder", MODEL_BODY)
    assert response.status_code == 409, response.text
    assert _code(response) == ErrorCode.MODEL_ALREADY_EXISTS.value
    assert _goal("RegHolder") == "theirs"
    assert not (zone_data_root / "custom" / "RegHolder").exists()


async def test_a_failed_model_create_does_not_delete_a_board(
    owners: MagicMock, registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """建立失敗的回滾不碰看板：編號可能屬於別的身分."""
    _ = owners
    _register(registry, "RollHolder")
    calls: list[str] = []
    real = MissionManager.delete_board

    async def _spy(self: MissionManager, owner: AgentIdentity) -> bool:
        calls.append(owner.agent_id)
        return await real(self, owner)

    monkeypatch.setattr(MissionManager, "delete_board", _spy)
    monkeypatch.setattr(custom_model_service, "save_model", lambda *_a, **_k: False)
    response = _send("POST", "/v1/ui/custom_models/RollHolder", MODEL_BODY)
    assert response.status_code == 500, response.text
    assert calls == []


async def test_creating_by_editing_clears_a_leftover_board(
    sandbox: Path, admin: tuple[AgentIdentity, str]
) -> None:
    _ = sandbox
    who, password = admin
    await _put_board("EditSoul", "leftover")
    body = {"config": {"is_toukasoul": True}, "admin_password": password}
    response = _send("POST", "/v1/souls/EditSoul", body, who)
    assert response.status_code == 200, response.text
    assert _goal("EditSoul") is None


async def test_creating_by_editing_with_an_unusable_model_keeps_the_board(
    sandbox: Path, admin: tuple[AgentIdentity, str]
) -> None:
    _ = sandbox
    who, password = admin
    await _put_board("EditBad", "kept")
    body = {
        "config": {"is_toukasoul": True, "base_model": "no-such-model"},
        "admin_password": password,
    }
    response = _send("POST", "/v1/souls/EditBad", body, who)
    assert response.status_code == 404, response.text
    assert _goal("EditBad") == "kept"


async def test_converting_an_existing_folder_keeps_its_board(
    sandbox: Path, admin: tuple[AgentIdentity, str]
) -> None:
    who, password = admin
    _soul_folder(sandbox, "ConvSoul", is_soul=False)
    await _put_board("ConvSoul", "its own board")
    body = {"config": {"is_toukasoul": True}, "admin_password": password}
    response = _send("POST", "/v1/souls/ConvSoul", body, who)
    assert response.status_code == 200, response.text
    assert _goal("ConvSoul") == "its own board"


async def test_an_unreadable_registry_stops_the_creation(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unreadable() -> Any:
        raise RegistryUnreadableError

    await _put_board("UnsureSoul", "kept")
    monkeypatch.setattr(mission_boards, "_registry_ids", _unreadable)
    response = _create_soul("UnsureSoul")
    assert response.status_code == 503, response.text
    assert _code(response) == ErrorCode.AGENT_ID_CHECK_FAILED.value
    assert not (sandbox / "soul" / "UnsureSoul").exists()
    assert _goal("UnsureSoul") == "kept"


# --------------------------------------------------------------------------- #
# 4. 「不屬於任何代理人」的判斷
# --------------------------------------------------------------------------- #


def test_who_claims_an_id(
    zone_data_root: Path, registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _soul_folder(zone_data_root, "ClaimSoul")
    (zone_data_root / "custom" / "HumanSpace" / "drive").mkdir(parents=True)  # 沒有 config
    _register(registry, "ClaimReg")
    monkeypatch.setattr(mission_boards, "_human_ids", lambda: {"human-uuid"})
    claimed = ("ClaimSoul", "HumanSpace", "DebugAgent", "DispatchAgent", "ClaimReg", "human-uuid")
    for agent_id in claimed:
        assert not mission_boards.is_unclaimed(AgentIdentity(agent_id=agent_id)), agent_id
    assert mission_boards.is_unclaimed(AgentIdentity(agent_id="Nobody"))
    assert mission_boards.is_unclaimed(AgentIdentity(agent_id="claimsoul"))  # 逐字比對


def test_an_unreadable_user_list_is_not_treated_as_unclaimed(
    zone_data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = zone_data_root
    monkeypatch.setattr(mission_boards, "_human_ids", _fail)
    with pytest.raises(ToukaException) as raised:
        mission_boards.is_unclaimed(AgentIdentity(agent_id="Nobody"))
    assert raised.value.code is ErrorCode.AGENT_ID_CHECK_FAILED


# --------------------------------------------------------------------------- #
# 5. 重置、備份、匯出
# --------------------------------------------------------------------------- #


async def test_reset_clears_the_new_board_and_the_old_folder(zone_data_root: Path) -> None:
    legacy = zone_data_root / "custom" / "ResetAgent" / "drive" / "missions"
    legacy.mkdir(parents=True)
    (legacy / "current_mission.json").write_text("{}", encoding="utf-8")
    await _put_board("ResetAgent", "reset me")
    await _put_board("OtherAgent", "keep me")

    def guard(candidate: str) -> str:
        return candidate

    assert await P.remaining_mission("ResetAgent", guard) == 2
    assert await P.purge_mission("ResetAgent", guard) == 2
    assert await P.remaining_mission("ResetAgent", guard) == 0
    assert not legacy.exists()
    assert _goal("ResetAgent") is None
    assert _goal("OtherAgent") == "keep me"


def _load_backup_script() -> Any:
    spec = importlib.util.spec_from_file_location("manage_backup_boards_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backup_and_restore_carry_the_boards(zone_data_root: Path, tmp_path: Path) -> None:
    mod = _load_backup_script()
    assert "missions" in mod.SANCTUARY_FOLDERS
    board = zone_data_root / "missions" / ("a" * 64 + ".json")
    board.parent.mkdir()
    board.write_text('{"goal": "backed up"}', encoding="utf-8")
    (zone_data_root / "missions" / ".migrated").write_text("1", encoding="utf-8")
    project = tmp_path.resolve() / "project"
    config = project / "ToukaProxy" / "config"
    config.mkdir(parents=True)
    env = project / ".env"
    env.write_text(f"SANCTUARY_DATA_PATH={zone_data_root.as_posix()}\n", encoding="utf-8")
    resolver = mod.PathResolver(
        project, env_path=env, data_root=zone_data_root, config_dir=config
    )
    output = mod.BackupEngine(resolver).run_backup(tmp_path / "backups", "Touka_Backup")
    with zipfile.ZipFile(output) as zf:
        assert f"data/missions/{board.name}" in zf.namelist()
    fresh = tmp_path.resolve() / "fresh_data"
    mod.RestoreEngine(resolver).run_restore(str(output), str(fresh))
    assert (fresh / "missions" / board.name).read_text(encoding="utf-8") == '{"goal": "backed up"}'
    assert (fresh / "missions" / ".migrated").exists()


async def test_the_export_contains_each_agents_board(
    zone_data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = zone_data_root
    await _put_board("ExpAgent", "exported goal")

    async def _empty() -> list[Any]:
        return []

    async def _no_facts(_identity: AgentIdentity) -> None:
        return None

    monkeypatch.setattr(gx.session_service, "get_all_sessions", _empty)
    monkeypatch.setattr(gx.session_service, "get_all_projects", _empty)
    monkeypatch.setattr(gx, "find_avatar", lambda _uid: None)
    owned = ["ExpAgent", "NoBoard"]
    user = User(id="U-board", email="board@touka.io", hashed_password="h", owned_agents=owned)
    export = await gx._collect(user, facts_fn=_no_facts)
    board = json.loads(export.text_entries["agents/ExpAgent/mission_board.json"])
    assert board["goal"] == "exported goal"
    assert "agents/NoBoard/mission_board.json" not in export.text_entries
    assert "agent_mission_board" in export.manifest["included"]
    assert "mission_board.json" in export.text_entries["README.txt"]
