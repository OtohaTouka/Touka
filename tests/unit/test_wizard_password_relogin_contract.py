"""首次設定精靈改管理員密碼後的後端契約（端點級，全走 conftest 沙箱 users.json）。

精靈第 2 步用管理員自己的卡 PATCH 自己的密碼。後端撤銷這個人所有舊卡（access 與
refresh cookie）是安全設計（ADR-0144），不放寬；精靈因此請使用者用新密碼重新登入，
登入後從下一步接續（ADR-0053 補記 2026-10-23）。這裡釘住精靈依賴的兩件事：舊卡與舊
refresh cookie 都死，用新密碼登入立刻可用。
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from src.main import app
from src.services.auth_service import AuthService

OLD_PW = "Temporary#Pass1"
NEW_PW = "BrandNew#Pass2"
EMAIL = "wizard-admin@test.com"


def test_self_password_change_revokes_old_session_and_new_login_works():
    admin = AuthService().create_user(EMAIL, OLD_PW, role="admin")

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/v1/auth/login", json={"email": EMAIL, "password": OLD_PW})
        assert r.status_code == 200, r.text
        old_bearer = {"Authorization": f"Bearer {r.json()['access_token']}"}
        old_refresh = r.cookies.get("refresh_token")
        assert old_refresh
        # iat 是秒級；改密碼落在下一秒，舊卡才會被判為改密碼前簽發（同秒依設計放行）。
        time.sleep(1.1)

        r = client.patch(f"/v1/admin/users/{admin.id}", json={"password": NEW_PW}, headers=old_bearer)
        assert r.status_code == 200, r.text

        # 舊卡與舊 refresh cookie 都失效（精靈中途被登出的成因）。
        assert client.get("/v1/auth/me", headers=old_bearer).status_code == 401
        client.cookies.clear()
        client.cookies.set("refresh_token", old_refresh)
        assert client.post("/v1/auth/refresh").status_code == 401
        client.cookies.clear()

        # 用新密碼立刻重新登入可用（同秒簽發依設計放行）。
        r = client.post("/v1/auth/login", json={"email": EMAIL, "password": NEW_PW})
        assert r.status_code == 200, r.text
        new_bearer = {"Authorization": f"Bearer {r.json()['access_token']}"}
        new_refresh = r.cookies.get("refresh_token")
        assert new_refresh

        me = client.get("/v1/auth/me", headers=new_bearer)
        assert me.status_code == 200, me.text
        assert me.json()["id"] == admin.id

        # 新的 refresh cookie 也能續期（之後的 401 續期不會把人登出）。
        client.cookies.clear()
        client.cookies.set("refresh_token", new_refresh)
        assert client.post("/v1/auth/refresh").status_code == 200
