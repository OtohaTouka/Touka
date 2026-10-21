"""ADR-0250 A5 保護 2、3：自訂模型（L2）的新 ID 只差大小寫也算撞名，Windows 與 Linux 一樣擋。

- 保護 2：和靈魂撞名改成明確的不分大小寫比對（比對靈魂 ID 清單，不靠檔案系統）。
- 保護 3：新 ID 和現有 L2 自訂模型只差大小寫 → 409 ``MODEL_ALREADY_EXISTS``；建立與改名都擋，
  改名排除自己（只改大小寫的自我改名不擋）；已經存在的資料不動。

Windows 檔案系統本來就不分大小寫，所以「靈魂存在」「自訂模型檔存在」換成分大小寫的判斷
（模擬 Linux／Docker），改動前才紅得出來。全部寫在測試沙盒，擁有清單的寫入改成假的。
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
from src.services.soul_service import soul_service
from src.utils import custom_io

SOUL = "CaseAlice"
MINE = ("CaseFoo", "CaseBar")
_ADMIN = AgentIdentity.for_role("case-admin", "admin", is_human=True)
_BODY = {"name": "x", "description": "", "systemPrompt": "CUSTOM", "config": {}}


def _base() -> Path:
    return Path(resource_manager.get_base_path())


@pytest.fixture
def linux_like(monkeypatch: pytest.MonkeyPatch) -> Any:
    """沙盒裡建靈魂 CaseAlice；靈魂與自訂模型「存在」改成分大小寫（模擬 Linux）。"""
    soul_dir = _base() / "soul" / SOUL
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "config.json").write_text(json.dumps({"is_toukasoul": True}), "utf-8")
    real_exists = custom_io.custom_file_exists
    monkeypatch.setattr(
        soul_service, "is_toukasoul", lambda who: getattr(who, "agent_id", who) == SOUL
    )
    monkeypatch.setattr(
        custom_io,
        "custom_file_exists",
        lambda ident, name: ident.agent_id in custom_io.list_custom_ids()
        and real_exists(ident, name),
    )
    monkeypatch.setattr("src.routers.custom_models.user_service", MagicMock())
    created: list[Path] = []
    yield created
    shutil.rmtree(soul_dir, ignore_errors=True)
    for name in (*MINE, *created):
        shutil.rmtree(_base() / "custom" / name, ignore_errors=True)


def _post(url: str, body: dict[str, Any]) -> Any:
    app.dependency_overrides[get_verified_identity] = lambda: _ADMIN
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.post(url, json=body)


def _create(model_id: str) -> Any:
    return _post(f"/v1/ui/custom_models/{model_id}", _BODY)


def _custom_ids() -> set[str]:
    return set(custom_io.list_custom_ids())


@pytest.mark.parametrize(
    "model_id", ["casealice", "CASEALICE", "@casealice", "toukasoul-casealice"]
)
def test_create_named_like_a_soul_in_other_case_is_rejected(
    linux_like: list, model_id: str
) -> None:
    """場景 8：已有靈魂 CaseAlice，建立 casealice（含 MonitorLayer 認得的寫法）→ 409。"""
    linux_like.append(model_id)
    resp = _create(model_id)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MODEL_ALREADY_EXISTS"
    assert model_id not in _custom_ids()


def test_rename_to_a_soul_name_in_other_case_is_rejected(linux_like: list) -> None:
    linux_like.append("casealice")
    assert _create("CaseFoo").status_code == 200
    resp = _post("/v1/ui/custom_models/CaseFoo/rename", {"new_id": "casealice"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MODEL_ALREADY_EXISTS"
    assert {"CaseFoo"} <= _custom_ids() and "casealice" not in _custom_ids()


def test_create_differing_only_in_case_from_a_custom_model_is_rejected(
    linux_like: list,
) -> None:
    """保護 3：已有 CaseFoo，建立 CASEFOO → 409；CaseFoo 的內容沒被改。"""
    linux_like.append("CASEFOO")
    assert _create("CaseFoo").status_code == 200
    prompt = _base() / "custom" / "CaseFoo" / "system_prompt.md"
    before = prompt.read_bytes()
    resp = _post("/v1/ui/custom_models/CASEFOO", {**_BODY, "systemPrompt": "OTHER"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MODEL_ALREADY_EXISTS"
    assert prompt.read_bytes() == before
    assert {"CaseFoo"} <= _custom_ids() and "CASEFOO" not in _custom_ids()


def test_rename_onto_another_custom_model_in_other_case_is_rejected(linux_like: list) -> None:
    linux_like.append("casefoo")
    assert _create("CaseFoo").status_code == 200
    assert _create("CaseBar").status_code == 200
    resp = _post("/v1/ui/custom_models/CaseBar/rename", {"new_id": "casefoo"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MODEL_ALREADY_EXISTS"
    assert {"CaseFoo", "CaseBar"} <= _custom_ids()


def test_renaming_itself_to_another_case_is_not_blocked(linux_like: list) -> None:
    """改名排除自己：CaseFoo 只改大小寫成 casefoo 不擋。"""
    linux_like.append("casefoo")
    assert _create("CaseFoo").status_code == 200
    resp = _post("/v1/ui/custom_models/CaseFoo/rename", {"new_id": "casefoo"})
    assert resp.status_code == 200, resp.text


def test_updating_an_existing_custom_model_still_works(linux_like: list) -> None:
    """已存在的 ID 照原寫法更新：不經新 ID 檢查，照舊可存。"""
    assert _create("CaseFoo").status_code == 200
    resp = _post("/v1/ui/custom_models/CaseFoo", {**_BODY, "systemPrompt": "UPDATED"})
    assert resp.status_code == 200, resp.text
    prompt = _base() / "custom" / "CaseFoo" / "system_prompt.md"
    assert prompt.read_text(encoding="utf-8") == "UPDATED"
