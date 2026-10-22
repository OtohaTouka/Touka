"""已登入的人重輸密碼打錯回 403 ``AUTH_REAUTH_FAILED``，不回 401.

前端 ``fetchTouka`` 遇 401 會先續期再重送一次，同一次點擊就驗兩次密碼。比照改／刪靈魂的
step-up：重置（含清除連備份）、還原、設定匯入、清除／匯出我的資料、改密碼都回 403。
只有登入失敗（``auth_service``）是 401：那是 ``isAuthEndpoint`` 排除、不會重送的端點。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from src.config import get_initial_admin_password_file
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.routers.auth import get_current_user
from src.services.auth_service import AuthService
from src.services.user_service import UserService

SRC = Path(__file__).resolve().parents[2] / "ToukaProxy" / "src"
LOGIN_ONLY = frozenset({"services/auth_service.py"})


def _invalid_credentials_sites() -> list[str]:
    sites = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sites += [
            f"{rel}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "AUTH_INVALID_CREDENTIALS"
        ]
    return [site for site in sites if site.split(":", 1)[0] not in LOGIN_ONLY]


def test_only_login_answers_a_wrong_password_with_401() -> None:
    """新的密碼重輸要用 AUTH_REAUTH_FAILED；AUTH_INVALID_CREDENTIALS 只留給登入."""
    assert _invalid_credentials_sites() == []


def test_first_login_password_change_with_the_wrong_initial_password(tmp_path: Path) -> None:
    """首次登入：初始密碼打錯回 403、什麼都不變（檔案與臨時旗標還在）；打對才換掉並刪檔."""
    pw_file = get_initial_admin_password_file()
    pw_file.unlink(missing_ok=True)
    svc = AuthService(users_file_path=str(tmp_path / "users.json"))
    svc.ensure_admin_user([])
    admin = next(u for u in svc.get_all_users() if u.role == "admin")
    initial = next(
        line.split(":", 1)[1].strip()
        for line in pw_file.read_text(encoding="utf-8").splitlines()
        if line.lower().startswith("password")
    )
    users = UserService(auth_service=svc)

    with pytest.raises(ToukaException) as exc:
        users.change_own_password(admin.id, "wrong-initial", "NewStrongPass1!")
    assert (exc.value.code, exc.value.status_code) == (ErrorCode.AUTH_REAUTH_FAILED, 403)
    assert pw_file.exists() and svc.get_user_by_id(admin.id).is_temp_password is True

    users.change_own_password(admin.id, initial, "NewStrongPass1!")
    assert not pw_file.exists() and svc.get_user_by_id(admin.id).is_temp_password is False


def test_change_password_with_wrong_current_password_is_403() -> None:
    user = AuthService().create_user("chg_wrong@touka.io", "correct_pw_123", role="normal")
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        resp = TestClient(app, raise_server_exceptions=False).post(
            "/v1/auth/change-password",
            json={"current_password": "WRONG", "new_password": "NewStrong456!"},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "AUTH_REAUTH_FAILED"
