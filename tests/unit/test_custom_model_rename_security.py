"""自訂模型改名：搬資料夾失敗不能回 200、不能把擁有權搬到不存在的 ID（H12）.

過去 ``rename_custom_model`` 不看搬資料夾的結果：來源不見、目標已被占用、搬移本身出錯時
照樣回 200，並把 ``users.json`` 的擁有權從舊 ID 搬到不存在的新 ID，使用者失去原模型。

- 失敗一律回錯誤：來源不見 404 ``MODEL_NOT_FOUND``、目標已被占用 409
  ``MODEL_ALREADY_EXISTS``、搬移出錯 500 ``SYSTEM_ERROR``；擁有權與 config.json 都不動。
- 只差大小寫的改名（Windows 上新舊名是同一個資料夾）要真的改名成功，擁有權照舊搬。
- 一般改名成功：資料夾、config.json 的 id、擁有權都跟著改（釘住）。

全部寫在 conftest 測試沙盒；擁有清單的寫入換成 MagicMock，只記錄呼叫。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager
from src.utils import custom_io

OLD = "RenameSecOld"
NEW = "RenameSecNew"
GHOST = "RenameSecGhost"
_ADMIN = AgentIdentity.for_role("rename-sec-admin", "admin", is_human=True)
_BODY = {"name": "x", "description": "", "systemPrompt": "PROMPT", "config": {}}


def _custom_base() -> Path:
    return Path(resource_manager.get_base_path()) / "custom"


@pytest.fixture
def owners(monkeypatch: pytest.MonkeyPatch) -> Any:
    """擁有清單換成 MagicMock；結束時清掉本檔建立的資料夾與檔案。"""
    users = MagicMock()
    monkeypatch.setattr("src.routers.custom_models.user_service", users)
    yield users
    for name in (OLD, NEW, GHOST, OLD.lower()):
        target = _custom_base() / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()


def _post(url: str, body: dict[str, Any]) -> Any:
    app.dependency_overrides[get_verified_identity] = lambda: _ADMIN
    try:
        return TestClient(app, raise_server_exceptions=False).post(url, json=body)
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def _create(owners: MagicMock, model_id: str) -> bytes:
    """建立模型後清掉建立時的擁有權呼叫紀錄；回傳 config.json 原始位元組。"""
    response = _post(f"/v1/ui/custom_models/{model_id}", _BODY)
    assert response.status_code == 200, response.text
    owners.reset_mock()
    return (_custom_base() / model_id / "config.json").read_bytes()


def _rename(old_id: str, new_id: str) -> Any:
    return _post(f"/v1/ui/custom_models/{old_id}/rename", {"new_id": new_id})


def _assert_ownership_untouched(owners: MagicMock) -> None:
    owners.remove_owned_agent.assert_not_called()
    owners.add_owned_agent.assert_not_called()


def _assert_ownership_moved(owners: MagicMock, old_id: str, new_id: str) -> None:
    owners.remove_owned_agent.assert_called_once()
    owners.add_owned_agent.assert_called_once()
    removed = owners.remove_owned_agent.call_args.args
    added = owners.add_owned_agent.call_args.args
    assert (removed[0], removed[1].agent_id) == (_ADMIN.agent_id, old_id)
    assert (added[0], added[1].agent_id) == (_ADMIN.agent_id, new_id)


def _stored_id(model_id: str) -> str:
    config = json.loads((_custom_base() / model_id / "config.json").read_text(encoding="utf-8"))
    return config["id"]


# ── 失敗：回錯誤，擁有權與設定都不動 ─────────────────────────────────────────────


def test_renaming_a_missing_model_is_404_and_moves_no_ownership(owners: MagicMock) -> None:
    """來源不見（admin 跳過擁有權檢查也一樣）→ 404，不產生新 ID 的擁有權。"""
    response = _rename(GHOST, NEW)

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "MODEL_NOT_FOUND"
    _assert_ownership_untouched(owners)
    assert not (_custom_base() / NEW).exists()


def test_renaming_onto_an_occupied_path_is_409_and_keeps_config(owners: MagicMock) -> None:
    """新 ID 已被不是模型資料夾的東西占用（新 ID 檢查只看資料夾）→ 409，什麼都不動。"""
    before = _create(owners, OLD)
    occupied = _custom_base() / NEW
    occupied.write_text("not a model", encoding="utf-8")

    response = _rename(OLD, NEW)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MODEL_ALREADY_EXISTS"
    _assert_ownership_untouched(owners)
    assert (_custom_base() / OLD / "config.json").read_bytes() == before
    assert occupied.read_text(encoding="utf-8") == "not a model"


def test_a_failed_move_is_an_error_and_moves_no_ownership(
    owners: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """搬移本身出錯（例如 Windows 上檔案被占用）→ 500，舊資料夾與設定原樣。"""
    before = _create(owners, OLD)

    def _locked(*_args: object) -> None:
        raise PermissionError

    monkeypatch.setattr(custom_io.shutil, "move", _locked)

    response = _rename(OLD, NEW)

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "SYSTEM_ERROR"
    _assert_ownership_untouched(owners)
    assert (_custom_base() / OLD / "config.json").read_bytes() == before
    assert NEW not in custom_io.list_custom_ids()


# ── 成功：資料夾、config.json 的 id、擁有權都跟著改 ────────────────────────────────


def test_case_only_rename_renames_the_folder_and_moves_ownership(owners: MagicMock) -> None:
    """只差大小寫（Windows 上是同一個資料夾）→ 真的改名；Linux 上本來就是兩個名字。"""
    _create(owners, OLD)
    new_id = OLD.lower()

    response = _rename(OLD, new_id)

    assert response.status_code == 200, response.text
    ids = custom_io.list_custom_ids()
    assert new_id in ids
    assert OLD not in ids
    assert _stored_id(new_id) == new_id
    _assert_ownership_moved(owners, OLD, new_id)


def test_successful_rename_moves_folder_config_and_ownership(owners: MagicMock) -> None:
    """釘住：一般改名成功時，擁有權照舊從舊 ID 搬到新 ID。"""
    _create(owners, OLD)

    response = _rename(OLD, NEW)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "success", "old_id": OLD, "new_id": NEW}
    ids = custom_io.list_custom_ids()
    assert NEW in ids
    assert OLD not in ids
    assert _stored_id(NEW) == NEW
    _assert_ownership_moved(owners, OLD, NEW)
