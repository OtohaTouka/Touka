"""``/health`` 的通用提示管道 ``system_notices``（iso-c2 U2h）.

每一項只有 ``{code, level}``，文字由前端翻譯。初期兩個代碼：
- ``autonomy_config_unreadable``：autonomy.json 讀不到，自主執行已停；
- ``auth_policies_unreadable``：安全規則啟動時讀不到，所有工具和指令都拒絕。
用真的服務在 tmp 設定上讓開檔丟 EACCES，確認服務給的原因字串和提示代碼對得上。
"""

from __future__ import annotations

import errno
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.routers import observability
from src.services import autonomy as autonomy_module
from src.services.autonomy import AutonomyService
from src.services.permission_service import PermissionConfigHealth, PermissionService

if TYPE_CHECKING:
    from collections.abc import Iterator

_EXAMPLES = Path(__file__).resolve().parents[2] / "ToukaProxy" / "config"
_REAL_OPEN = Path.open
AUTONOMY = {"code": "autonomy_config_unreadable", "level": "warning"}
AUTH_POLICIES = {"code": "auth_policies_unreadable", "level": "warning"}


@pytest.fixture(autouse=True)
def _fresh_permission_service() -> Iterator[None]:
    PermissionService.reset_singleton()
    yield
    PermissionService.reset_singleton()


def _deny_reads(monkeypatch: pytest.MonkeyPatch, *targets: Path) -> None:
    """讓指定檔案「存在但讀不到」（等同 chmod 000）。"""
    denied = {t.resolve() for t in targets}

    def _open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.resolve() in denied:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return _REAL_OPEN(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)


def _copy_example(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copyfile(_EXAMPLES / f"{name}.example", target)
    return target


def _services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, autonomy: bool, policies: bool
) -> None:
    """裝上真的 PermissionService 與 AutonomyService；指定的那個檔讀不到。"""
    policy_file = _copy_example(tmp_path, "auth_policies.json")
    autonomy_file = _copy_example(tmp_path, "autonomy.json")
    _deny_reads(monkeypatch, *([policy_file] if policies else []),
                *([autonomy_file] if autonomy else []))
    monkeypatch.setattr(autonomy_module, "CONFIG_FILE", autonomy_file)
    PermissionService.reset_singleton()
    permissions = PermissionService(
        config_path=str(tmp_path / "missing_permissions.json"),
        identity_path=str(tmp_path / "missing_identity.json"),
        policy_path=str(policy_file),
    )
    monkeypatch.setattr(observability, "permission_service", permissions)
    monkeypatch.setattr(observability, "autonomy_service", AutonomyService())


@pytest.mark.parametrize(
    ("autonomy", "policies", "notices"),
    [
        (False, False, []),
        (True, False, [AUTONOMY]),
        (False, True, [AUTH_POLICIES]),
        (True, True, [AUTONOMY, AUTH_POLICIES]),
    ],
)
def test_health_lists_a_notice_for_each_unreadable_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, autonomy: bool, policies: bool,
    notices: list[dict[str, str]],
) -> None:
    _services(tmp_path, monkeypatch, autonomy=autonomy, policies=policies)
    client = TestClient(app, raise_server_exceptions=False)
    for path in ("/v1/health", "/v1/system_health"):
        body = client.get(path).json()
        assert body["system_notices"] == notices, path
        assert body["status"] == ("degraded" if notices else "healthy"), path


def test_other_permission_problems_are_not_a_system_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """permissions.json 熱重載失敗另有欄位（``permission_config_degraded_reason``），不算提示."""

    class _Permissions:
        permission_config_health = PermissionConfigHealth(
            degraded=True, reason="permissions_config_reload_failed"
        )

    monkeypatch.setattr(observability, "permission_service", _Permissions())
    body = TestClient(app).get("/v1/health").json()
    assert (body["status"], body["system_notices"]) == ("degraded", [])
