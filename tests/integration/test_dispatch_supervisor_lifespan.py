"""ADR-0082：DispatchSupervisor 與 proxy lifespan 的整合測試.

驗證：預設關閉時 lifespan 零 spawn；顯式開啟（monkeypatch settings）時啟動一次、
關機時停止；/stats 折入 dispatch_supervisor 觀測欄位（沿用 interrupted_runs 的
dict-fold 技巧，OpenAPI 零變動）。subprocess 層全 mock；identity 僅用 DebugAgent。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from src import main as main_module
from src.config import settings
from src.main import app
from src.models.identity import AgentIdentity
from src.services.dispatch_supervisor import DispatchSupervisor
from src.services.identity_router import get_verified_identity


def test_lifespan_disabled_by_default_never_spawns():
    """DISPATCH_AUTOSPAWN=false（conftest 安全線）→ lifespan 不啟動 supervisor。"""
    assert settings.DISPATCH_AUTOSPAWN is False
    with (
        patch.object(DispatchSupervisor, "start", new_callable=AsyncMock) as mock_start,
        patch.object(DispatchSupervisor, "stop", new_callable=AsyncMock),
    ):
        with TestClient(app) as client:
            sup = client.app.state.dispatch_supervisor
            assert isinstance(sup, DispatchSupervisor)
            assert sup.status()["status"] == "disabled"
        mock_start.assert_not_awaited()


def test_lifespan_enabled_starts_once_and_stops_on_shutdown(monkeypatch):
    """顯式開啟（settings 屬性層）→ 啟動恰一次；shutdown 必停（即使曾啟動失敗）。"""
    monkeypatch.setattr(settings, "DISPATCH_AUTOSPAWN", True)
    with (
        patch.object(DispatchSupervisor, "start", new_callable=AsyncMock) as mock_start,
        patch.object(DispatchSupervisor, "stop", new_callable=AsyncMock) as mock_stop,
    ):
        with TestClient(app):
            pass
        mock_start.assert_awaited_once()
        mock_stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_cleanup_continues_after_protocol_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """較早 cleanup 失敗不得阻止後續清理：dispatch supervisor 仍必須被停止。"""
    events: list[str] = []
    original_protocol_cleanup = main_module.protocol_manager.cleanup
    original_dispatch_stop = DispatchSupervisor.stop
    body_entered = False

    async def _failing_protocol_cleanup() -> None:
        events.append("protocol_cleanup")
        await original_protocol_cleanup()
        if body_entered:
            raise RuntimeError("cleanup-failure-marker")

    async def _record_dispatch_stop(supervisor: DispatchSupervisor) -> None:
        events.append("dispatch_stop")
        await original_dispatch_stop(supervisor)

    monkeypatch.setattr(main_module.protocol_manager, "cleanup", _failing_protocol_cleanup)
    monkeypatch.setattr(DispatchSupervisor, "stop", _record_dispatch_stop)

    with pytest.raises(RuntimeError, match="cleanup-failure-marker"):
        async with main_module.lifespan(app):
            body_entered = True
            events.append("body")

    shutdown = events[events.index("body") + 1 :]
    assert "protocol_cleanup" in shutdown
    assert "dispatch_stop" in shutdown
    assert shutdown.index("protocol_cleanup") < shutdown.index("dispatch_stop")


def test_stats_exposes_dispatch_supervisor_field():
    """/v1/stats 含 dispatch_supervisor: {status, pid, restarts}（預設 disabled）。"""
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        agent_id="DebugAgent", role="Owner"
    )
    try:
        with TestClient(app) as client:
            resp = client.get("/v1/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert "dispatch_supervisor" in body
        field = body["dispatch_supervisor"]
        assert field["status"] == "disabled"
        assert "pid" in field
        assert "restarts" in field
    finally:
        app.dependency_overrides.clear()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
