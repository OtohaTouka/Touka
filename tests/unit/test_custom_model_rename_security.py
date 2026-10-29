"""自訂模型改名：搬資料夾失敗不能回 200、不能把擁有權搬到不存在的 ID（H12）.

過去 ``rename_custom_model`` 不看搬資料夾的結果：來源不見、目標已被占用、搬移本身出錯時
照樣回 200，並把 ``users.json`` 的擁有權從舊 ID 搬到不存在的新 ID，使用者失去原模型。

- 失敗一律回錯誤：來源不見 404 ``MODEL_NOT_FOUND``、目標已被占用 409
  ``MODEL_ALREADY_EXISTS``、搬移出錯 500 ``SYSTEM_ERROR``；擁有權與 config.json 都不動。
- 只差大小寫的改名（Windows 上新舊名是同一個資料夾）要真的改名成功，擁有權照舊搬。
- 一般改名成功：資料夾、config.json 的 id、擁有權都跟著改（釘住）。

2026-10-25 補記（ADR-0254，舊審查報告丙-2）：

- 改名之後的步驟失敗（config.json 讀不到或寫不進去、擁有權寫不進去）要把資料夾與 config 搬回；
  搬回也失敗或出錯時寫 CRITICAL 的 AUDIT。
- 擁有權改成所有人的清單一起換名（不再只搬呼叫者自己的，呼叫者不是人類也搬），
  沒有舊 ID 的人清單裡的新 ID 是殘留、拿掉；新建模型時別人清單裡的同一個 ID 也拿掉，
  刪除時所有人的清單一起拿掉。
- 和靈魂同名的舊資料夾不能改名、刪除時不動擁有權；只認名稱完全相同、有 config.json 的
  模型資料夾（個人空間不算）；新舊 ID 相同什麼都不寫；新 ID 可以是中文（和建立時一樣），
  不能是 DebugAgent、DispatchAgent。

路由測試寫在 conftest 測試沙盒，擁有清單的寫入換成 MagicMock、只記錄呼叫；
``UserService`` 的換名與認領用沙盒裡真的 users.json。
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager
from src.services.user_service import UserService
from src.utils import custom_io

OLD = "RenameSecOld"
NEW = "RenameSecNew"
GHOST = "RenameSecGhost"
UNICODE = "改名安全測試"
_ADMIN = AgentIdentity.for_role("rename-sec-admin", "admin", is_human=True)
_DEBUGGER = AgentIdentity.for_role("DebugAgent", "Debugger")
_BODY = {"name": "x", "description": "", "systemPrompt": "PROMPT", "config": {}}
_AUDIT_OLD_NEW = f"AUDIT: custom model rename rollback FAILED old={OLD} new={NEW}"


def _custom_base() -> Path:
    return Path(resource_manager.get_base_path()) / "custom"


@pytest.fixture
def owners(monkeypatch: pytest.MonkeyPatch) -> Any:
    """擁有清單換成 MagicMock；結束時清掉本檔建立的資料夾與檔案。"""
    users = MagicMock()
    monkeypatch.setattr("src.routers.custom_models.user_service", users)
    yield users
    for name in (OLD, NEW, GHOST, OLD.lower(), UNICODE):
        target = _custom_base() / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()


def _send(method: str, url: str, body: dict[str, Any] | None, who: AgentIdentity) -> Any:
    app.dependency_overrides[get_verified_identity] = lambda: who
    try:
        return TestClient(app, raise_server_exceptions=False).request(method, url, json=body)
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def _post(url: str, body: dict[str, Any], who: AgentIdentity = _ADMIN) -> Any:
    return _send("POST", url, body, who)


def _create(owners: MagicMock, model_id: str) -> bytes:
    """建立模型後清掉建立時的擁有權呼叫紀錄；回傳 config.json 原始位元組。"""
    response = _post(f"/v1/ui/custom_models/{model_id}", _BODY)
    assert response.status_code == 200, response.text
    owners.reset_mock()
    return (_custom_base() / model_id / "config.json").read_bytes()


def _rename(old_id: str, new_id: str, who: AgentIdentity = _ADMIN) -> Any:
    return _post(f"/v1/ui/custom_models/{old_id}/rename", {"new_id": new_id}, who)


def _assert_ownership_untouched(owners: MagicMock) -> None:
    owners.rename_owned_agent.assert_not_called()
    owners.remove_owned_agent.assert_not_called()
    owners.add_owned_agent.assert_not_called()
    owners.claim_owned_agent.assert_not_called()
    owners.remove_owned_agent_from_all.assert_not_called()


def _assert_ownership_moved(owners: MagicMock, old_id: str, new_id: str) -> None:
    """所有人的擁有清單一起換名（不再只搬呼叫者自己的，丙-2）。"""
    owners.rename_owned_agent.assert_called_once_with(old_id, new_id)
    owners.remove_owned_agent.assert_not_called()
    owners.add_owned_agent.assert_not_called()


def _stored_id(model_id: str) -> str:
    config = json.loads((_custom_base() / model_id / "config.json").read_text(encoding="utf-8"))
    return config["id"]


def _audit(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [(r.levelno, r.getMessage()) for r in caplog.records if "AUDIT" in r.getMessage()]


def _fail_for(model_id: str, real: Any, result: Any, hits: list[str] | None = None) -> Any:
    """替身：``model_id`` 的資料夾存在時才回 ``result``（例外就丟出），其他照原本的函式.

    改名前的撞名檢查也會讀新 ID 的設定；只在資料夾搬過去之後失敗，才測得到回滾。
    """

    def _fake(identity: AgentIdentity, *args: Any) -> Any:
        if identity.agent_id == model_id and (_custom_base() / model_id).is_dir():
            if hits is not None:
                hits.append(model_id)
            if isinstance(result, BaseException):
                raise result
            return result
        return real(identity, *args)

    return _fake


def _moves_fail_after(
    monkeypatch: pytest.MonkeyPatch, ok_moves: int, failure: Any
) -> dict[str, int]:
    """前 ``ok_moves`` 次搬移照常，之後回 ``failure``（例外就丟出）。"""
    real_move = custom_io.move_custom_dir
    moves = {"n": 0}

    def _fake(old_id: str, new_id: str) -> bool:
        moves["n"] += 1
        if moves["n"] <= ok_moves:
            return real_move(old_id, new_id)
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(custom_io, "move_custom_dir", _fake)
    return moves


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

    monkeypatch.setattr(resource_manager, "rename_agent_container", _locked)

    response = _rename(OLD, NEW)

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "SYSTEM_ERROR"
    _assert_ownership_untouched(owners)
    assert (_custom_base() / OLD / "config.json").read_bytes() == before
    assert NEW not in custom_io.list_custom_ids()


def test_a_request_id_that_differs_from_the_folder_in_case_is_404(owners: MagicMock) -> None:
    """請求的 ID 只差大小寫（不分大小寫的磁碟上也搬得動）→ 404，什麼都不動（丙-2）。"""
    before = _create(owners, OLD)

    response = _rename(OLD.lower(), NEW)

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "MODEL_NOT_FOUND"
    _assert_ownership_untouched(owners)
    assert (_custom_base() / OLD / "config.json").read_bytes() == before
    assert NEW not in custom_io.list_custom_ids()


def test_a_legacy_folder_named_like_a_soul_is_not_renamed(
    owners: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """和靈魂同名的舊資料夾：清單裡的 ID 也是靈魂的擁有權 → 409，什麼都不動（丙-2）。"""
    before = _create(owners, OLD)
    monkeypatch.setattr("src.routers.custom_models.soul_io.list_soul_ids", lambda: [OLD])

    response = _rename(OLD, NEW)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MODEL_ALREADY_EXISTS"
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


def test_a_non_human_caller_moves_ownership_too(owners: MagicMock) -> None:
    """呼叫者不是人類（Debugger）也照搬擁有權，不再只在人類呼叫時搬（丙-2）。"""
    _create(owners, OLD)

    response = _rename(OLD, NEW, who=_DEBUGGER)

    assert response.status_code == 200, response.text
    assert NEW in custom_io.list_custom_ids()
    _assert_ownership_moved(owners, OLD, NEW)


def test_renaming_to_the_same_id_writes_nothing(owners: MagicMock) -> None:
    """新舊 ID 相同 → 200，擁有權不寫（丙-2）。"""
    before = _create(owners, OLD)

    response = _rename(OLD, OLD)

    assert response.status_code == 200, response.text
    _assert_ownership_untouched(owners)
    assert (_custom_base() / OLD / "config.json").read_bytes() == before


def test_a_chinese_new_id_is_accepted_like_on_create(owners: MagicMock) -> None:
    """建立時可以用中文 ID，改名也要收（以前改名一律 422，丙-2）。"""
    _create(owners, OLD)

    response = _rename(OLD, UNICODE)

    assert response.status_code == 200, response.text
    assert UNICODE in custom_io.list_custom_ids()
    assert _stored_id(UNICODE) == UNICODE
    _assert_ownership_moved(owners, OLD, UNICODE)


def test_a_folder_without_config_is_not_a_model(owners: MagicMock) -> None:
    """沒有 config.json 的資料夾（別的身分的個人空間 custom/<id>/drive）不是模型（丙-2）.

    查看、改名、刪除都 404：不搬、不刪，擁有清單裡的這個 ID 也不動。
    """
    drive = _custom_base() / GHOST / "drive"
    drive.mkdir(parents=True)
    (drive / "note.txt").write_text("keep", encoding="utf-8")

    responses = [
        _send("GET", f"/v1/ui/custom_models/{GHOST}", None, _ADMIN),
        _rename(GHOST, NEW),
        _send("DELETE", f"/v1/ui/custom_models/{GHOST}", None, _ADMIN),
    ]

    for response in responses:
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["code"] == "MODEL_NOT_FOUND"
    _assert_ownership_untouched(owners)
    assert (drive / "note.txt").read_text(encoding="utf-8") == "keep"
    assert NEW not in custom_io.list_custom_ids()


@pytest.mark.parametrize("content", ["not json", "[]", '"x"'], ids=["invalid", "list", "string"])
def test_a_config_that_is_not_a_json_object_does_not_block_the_rename(
    owners: MagicMock, content: str
) -> None:
    """config.json 不是 JSON 物件時不用改 id（之後存檔會整份重寫）→ 照樣改名、內容不動。"""
    _create(owners, OLD)
    (_custom_base() / OLD / "config.json").write_text(content, encoding="utf-8")

    response = _rename(OLD, NEW)

    assert response.status_code == 200, response.text
    assert (_custom_base() / NEW / "config.json").read_text(encoding="utf-8") == content
    _assert_ownership_moved(owners, OLD, NEW)


@pytest.mark.parametrize("reserved", ["DebugAgent", "dispatchagent"])
def test_system_agent_ids_cannot_be_used_for_a_model(owners: MagicMock, reserved: str) -> None:
    """DebugAgent、DispatchAgent（不分大小寫）不能拿來新建或改名：擁有清單和它們共用 ID。"""
    created = _post(f"/v1/ui/custom_models/{reserved}", _BODY)
    _create(owners, OLD)
    renamed = _rename(OLD, reserved)

    for response in (created, renamed):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "MODEL_ALREADY_EXISTS"
    _assert_ownership_untouched(owners)
    # 實際環境裡 custom/DebugAgent 是它的個人空間、本來就在；只確認沒有變成模型
    assert not (_custom_base() / reserved / "config.json").exists()


# ── 後續步驟失敗：搬回原狀（ADR-0254 H12 補記，舊審查報告丙-2） ────────────────────


def test_ownership_failure_rolls_the_rename_back(owners: MagicMock) -> None:
    """擁有權寫不進去 → 500；資料夾搬回舊 ID、config.json 的 id 改回舊 ID。"""
    _create(owners, OLD)
    owners.rename_owned_agent.side_effect = OSError

    response = _rename(OLD, NEW)

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "SYSTEM_ERROR"
    ids = custom_io.list_custom_ids()
    assert OLD in ids
    assert NEW not in ids
    assert _stored_id(OLD) == OLD


@pytest.mark.parametrize(
    ("patched", "result"),
    [
        pytest.param("write_custom_file", False, id="write-returns-false"),
        pytest.param("read_custom_file", None, id="exists-but-unreadable"),
        pytest.param("read_custom_file", RecursionError(), id="read-raises"),
    ],
)
def test_a_config_that_cannot_be_updated_rolls_the_rename_back(
    owners: MagicMock, monkeypatch: pytest.MonkeyPatch, patched: str, result: Any
) -> None:
    """新位置的 config.json 讀不到、寫不進去或出錯 → 500；資料夾搬回、擁有權不動。"""
    before = _create(owners, OLD)
    hits: list[str] = []
    monkeypatch.setattr(
        custom_io, patched, _fail_for(NEW, getattr(custom_io, patched), result, hits)
    )

    response = _rename(OLD, NEW)

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "SYSTEM_ERROR"
    assert hits == [NEW]
    _assert_ownership_untouched(owners)
    assert NEW not in custom_io.list_custom_ids()
    assert (_custom_base() / OLD / "config.json").read_bytes() == before


@pytest.mark.parametrize("failure", [False, PermissionError()], ids=["returns-false", "raises"])
def test_failed_rollback_after_a_config_failure_is_audited(
    owners: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Any,
) -> None:
    """config.json 寫不進去、搬回也失敗或出錯 → 500，寫一行 CRITICAL 的 AUDIT。"""
    _create(owners, OLD)
    monkeypatch.setattr(
        custom_io, "write_custom_file", _fail_for(NEW, custom_io.write_custom_file, False)
    )
    moves = _moves_fail_after(monkeypatch, 1, failure)

    with caplog.at_level(logging.WARNING):
        response = _rename(OLD, NEW)

    assert response.status_code == 500, response.text
    assert moves["n"] == 2
    assert _audit(caplog) == [(logging.CRITICAL, _AUDIT_OLD_NEW)]
    _assert_ownership_untouched(owners)


@pytest.mark.parametrize("failure", [False, PermissionError()], ids=["returns-false", "raises"])
def test_failed_rollback_after_an_ownership_failure_is_audited(
    owners: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Any,
) -> None:
    """擁有權寫不進去、搬回也失敗或出錯 → 500，AUDIT 記下是誰做的。"""
    _create(owners, OLD)
    owners.rename_owned_agent.side_effect = OSError
    moves = _moves_fail_after(monkeypatch, 1, failure)

    with caplog.at_level(logging.WARNING):
        response = _rename(OLD, NEW)

    assert response.status_code == 500, response.text
    assert moves["n"] == 2
    assert _audit(caplog) == [(logging.CRITICAL, f"{_AUDIT_OLD_NEW} by={_ADMIN.agent_id}")]


def test_a_rollback_that_cannot_restore_the_config_id_is_audited(
    owners: MagicMock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """資料夾搬回了、config.json 的 id 改不回舊 ID → 仍算沒搬回，寫 AUDIT。"""
    _create(owners, OLD)
    owners.rename_owned_agent.side_effect = OSError
    monkeypatch.setattr(
        custom_io, "write_custom_file", _fail_for(OLD, custom_io.write_custom_file, False)
    )

    with caplog.at_level(logging.WARNING):
        response = _rename(OLD, NEW)

    assert response.status_code == 500, response.text
    ids = custom_io.list_custom_ids()
    assert OLD in ids
    assert NEW not in ids
    assert _audit(caplog) == [(logging.CRITICAL, f"{_AUDIT_OLD_NEW} by={_ADMIN.agent_id}")]


# ── 建立與刪除的擁有權 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("who", "owner"),
    [(_ADMIN, _ADMIN.agent_id), (_DEBUGGER, None)],
    ids=["human-creator", "non-human-creator"],
)
def test_creating_a_model_claims_its_id(
    owners: MagicMock, who: AgentIdentity, owner: str | None
) -> None:
    """新建時一次換掉這個 ID 的擁有權：人類建立者拿到，別人的殘留拿掉（丙-2）。"""
    response = _post(f"/v1/ui/custom_models/{OLD}", _BODY, who)

    assert response.status_code == 200, response.text
    owners.claim_owned_agent.assert_called_once()
    claimed, claimed_owner = owners.claim_owned_agent.call_args.args
    assert claimed.agent_id == OLD
    assert claimed_owner == owner
    owners.add_owned_agent.assert_not_called()


def test_a_save_that_cannot_be_written_is_an_error(
    owners: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既有模型寫不進去 → 500、不認領；新建時 config.json 寫不進去 → 500、寫了一半的資料夾清掉.

    沒有 config.json 的資料夾不算模型，留著的話之後查看、刪除都找不到它（丙-2）。
    """
    before = _create(owners, OLD)
    # 第二個替身包住第一個：OLD 與 NEW 的寫入都失敗，其他照原本的函式
    monkeypatch.setattr(
        custom_io, "write_custom_file", _fail_for(OLD, custom_io.write_custom_file, False)
    )
    monkeypatch.setattr(
        custom_io, "write_custom_file", _fail_for(NEW, custom_io.write_custom_file, False)
    )

    updated = _post(f"/v1/ui/custom_models/{OLD}", {**_BODY, "systemPrompt": "OTHER"})
    created = _post(f"/v1/ui/custom_models/{NEW}", _BODY)

    for response in (updated, created):
        assert response.status_code == 500, response.text
        assert response.json()["detail"]["code"] == "SYSTEM_ERROR"
    _assert_ownership_untouched(owners)
    assert (_custom_base() / OLD / "config.json").read_bytes() == before
    assert not (_custom_base() / NEW).exists()


def test_updating_an_existing_model_does_not_claim_it(owners: MagicMock) -> None:
    """已存在的模型存檔 → 擁有權不動。"""
    _create(owners, OLD)

    response = _post(f"/v1/ui/custom_models/{OLD}", _BODY)

    assert response.status_code == 200, response.text
    _assert_ownership_untouched(owners)


@pytest.mark.parametrize("who", [_ADMIN, _DEBUGGER], ids=["human", "non-human"])
def test_deleting_a_model_drops_it_from_every_list(owners: MagicMock, who: AgentIdentity) -> None:
    """刪除時所有人的清單都拿掉這個 ID，不只呼叫者的，也不限人類呼叫者（丙-2）。"""
    _create(owners, OLD)

    response = _send("DELETE", f"/v1/ui/custom_models/{OLD}", None, who)

    assert response.status_code == 200, response.text
    assert OLD not in custom_io.list_custom_ids()
    owners.remove_owned_agent_from_all.assert_called_once()
    assert owners.remove_owned_agent_from_all.call_args.args[0].agent_id == OLD
    owners.remove_owned_agent.assert_not_called()


def test_other_endpoints_only_accept_the_real_folder_name(owners: MagicMock) -> None:
    """查看、刪除用只差大小寫的 ID → 404；存檔 → 409（走新建的撞名檢查），什麼都不動（丙-2）.

    不分大小寫的磁碟上這些 ID 找得到資料夾，擁有清單卻比對不到。
    """
    before = _create(owners, OLD)
    other = OLD.lower()

    got = _send("GET", f"/v1/ui/custom_models/{other}", None, _ADMIN)
    deleted = _send("DELETE", f"/v1/ui/custom_models/{other}", None, _ADMIN)
    saved = _post(f"/v1/ui/custom_models/{other}", {**_BODY, "systemPrompt": "OTHER"})

    assert got.status_code == 404, got.text
    assert deleted.status_code == 404, deleted.text
    assert saved.status_code == 409, saved.text
    assert saved.json()["detail"]["code"] == "MODEL_ALREADY_EXISTS"
    _assert_ownership_untouched(owners)
    assert (_custom_base() / OLD / "config.json").read_bytes() == before
    assert OLD in custom_io.list_custom_ids()


def test_deleting_a_legacy_folder_named_like_a_soul_keeps_ownership(
    owners: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """和靈魂同名的舊資料夾刪除時，不拿掉清單裡的 ID（那也是靈魂的擁有權，丙-2）。"""
    _create(owners, OLD)
    monkeypatch.setattr("src.routers.custom_models.soul_io.list_soul_ids", lambda: [OLD])

    response = _send("DELETE", f"/v1/ui/custom_models/{OLD}", None, _ADMIN)

    assert response.status_code == 200, response.text
    assert OLD not in custom_io.list_custom_ids()
    _assert_ownership_untouched(owners)


# ── UserService：所有人的清單一起改（真沙盒 users.json） ───────────────────────────

_PW = "RenameOwnersPass123!"


@pytest.fixture
def four_users() -> Any:
    auth = AuthService()
    users = UserService(auth)
    created = [
        auth.create_user(f"rename-sec-{n}@test.com", _PW, role="normal") for n in range(4)
    ]
    try:
        yield users, created
    finally:
        for user in created:
            users.delete_user(user.id)


def test_rename_owned_agent_keeps_every_owner(four_users: Any) -> None:
    """所有人的清單一起換名：admin 替別人改名時擁有權留給原主人。

    別人的清單不動；兩個 ID 都有的人只留一份新 ID；只有新 ID 的人（刪除後的殘留）拿掉。
    """
    users, (owner, other, both, stale) = four_users
    users.add_owned_agent(owner.id, AgentIdentity(agent_id=OLD))
    users.add_owned_agent(other.id, AgentIdentity(agent_id="RenameSecKeep"))
    for model_id in (OLD, NEW):
        users.add_owned_agent(both.id, AgentIdentity(agent_id=model_id))
    for model_id in ("RenameSecKeep", NEW):
        users.add_owned_agent(stale.id, AgentIdentity(agent_id=model_id))

    users.rename_owned_agent(OLD, NEW)

    assert users.get_user(owner.id).owned_agents == [NEW]
    assert users.get_user(other.id).owned_agents == ["RenameSecKeep"]
    assert users.get_user(both.id).owned_agents == [NEW]
    assert users.get_user(stale.id).owned_agents == ["RenameSecKeep"]


def test_claim_owned_agent_gives_the_id_to_the_creator_only(four_users: Any) -> None:
    """新建的 ID：別人清單裡的殘留拿掉、只加給建立者；刪除時所有人的清單一起拿掉。"""
    users, (creator, stale, other, _unused) = four_users
    users.add_owned_agent(stale.id, AgentIdentity(agent_id=OLD))
    users.add_owned_agent(other.id, AgentIdentity(agent_id="RenameSecKeep"))

    users.claim_owned_agent(AgentIdentity(agent_id=OLD), creator.id)

    assert users.get_user(creator.id).owned_agents == [OLD]
    assert users.get_user(stale.id).owned_agents == []
    assert users.get_user(other.id).owned_agents == ["RenameSecKeep"]

    users.remove_owned_agent_from_all(AgentIdentity(agent_id=OLD))

    assert users.get_user(creator.id).owned_agents == []
    assert users.get_user(other.id).owned_agents == ["RenameSecKeep"]
