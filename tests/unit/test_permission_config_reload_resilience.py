"""P5：權限設定啟動 fail-fast 與熱重載 last-known-good 保護。"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from src.routers import observability
from src.services.permission_service import (
    DEFAULT_PERMISSIONS_MODEL,
    PermissionService,
)

if TYPE_CHECKING:
    from pathlib import Path


def _build_service(config_path: Path) -> PermissionService:
    PermissionService.reset_singleton()
    return PermissionService(
        config_path=str(config_path),
        identity_path=str(config_path.with_name("missing_identity.json")),
        policy_path=str(config_path.with_name("missing_policies.json")),
    )


def _write_permissions(config_path: Path, allowed_tool: str) -> None:
    config_path.write_text(
        json.dumps(
            {
                "roles": {
                    "Owner": {
                        "allowed_tools": [allowed_tool],
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_startup_rejects_nonempty_invalid_permissions(tmp_path: Path) -> None:
    config_path = tmp_path / "permissions.json"
    config_path.write_text(
        json.dumps({"roles": {"Owner": {"allowed_tools": "not-a-list"}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match=r"roles\.Owner\.allowed_tools"):
        _build_service(config_path)

    PermissionService.reset_singleton()


@pytest.mark.asyncio
async def test_hot_reload_retains_last_good_and_reports_degraded_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_path = tmp_path / "permissions.json"
    _write_permissions(config_path, "read_file")
    service = _build_service(config_path)
    previous = service.roles_config

    config_path.write_text("{invalid-json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert service.reload_all() is False

    assert service.roles_config is previous
    assert service.permission_config_health.degraded is True
    assert service.permission_config_health.reason == "permissions_config_reload_failed"
    assert any(
        "AUDIT:" in record.getMessage() and "last-known-good" in record.getMessage()
        for record in caplog.records
    )

    monkeypatch.setattr(observability, "permission_service", service)
    degraded_basic = await observability.health_check()
    assert degraded_basic.status == "degraded"
    assert degraded_basic.permission_config_degraded is True
    assert degraded_basic.permission_config_degraded_reason == "permissions_config_reload_failed"

    degraded = await observability.get_system_health()
    assert degraded.status == "degraded"
    assert degraded.permission_config_degraded is True
    assert degraded.permission_config_degraded_reason == "permissions_config_reload_failed"

    _write_permissions(config_path, "write_file")
    assert service.reload_all() is True
    assert service.roles_config is not previous
    assert service.roles_config.roles["Owner"].allowed_tools == ["write_file"]
    assert service.permission_config_health.degraded is False
    assert service.permission_config_health.reason == ""

    recovered_basic = await observability.health_check()
    assert recovered_basic.status == "healthy"
    assert recovered_basic.permission_config_degraded is False
    assert recovered_basic.permission_config_degraded_reason == ""

    recovered = await observability.get_system_health()
    assert recovered.status == "healthy"
    assert recovered.permission_config_degraded is False
    assert recovered.permission_config_degraded_reason == ""
    PermissionService.reset_singleton()


@pytest.mark.parametrize("contents", [None, "", "{}"])
def test_bootstrap_absent_or_empty_permissions_uses_fallback(
    tmp_path: Path,
    contents: str | None,
) -> None:
    config_path = tmp_path / "permissions.json"
    if contents is not None:
        config_path.write_text(contents, encoding="utf-8")

    service = _build_service(config_path)

    assert service.roles_config is DEFAULT_PERMISSIONS_MODEL
    assert service.permission_config_health.degraded is False
    PermissionService.reset_singleton()
