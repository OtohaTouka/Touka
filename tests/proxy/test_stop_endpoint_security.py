"""中斷機制（#1）— `POST /ui/sessions/{id}/stop` 端點修復驗證。

修復前：端點呼叫不存在的 `autonomy_service.interrupt_session()` → 執行期 AttributeError，
「停止」實際是壞的。本測試驗證端點改呼 `rhythm_service.interrupt_session()`、
回 success，且確實把該 session 標記為中斷（供 ReAct/脈衝迴圈於下一步停下）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.ui_sessions import _require_authenticated
from src.services.process_registry import process_registry
from src.services.rhythm_service import rhythm_service


class _StopFakeProc:
    """子進程替身：被 /stop 終止時記錄 kill 並設定 returncode。"""

    def __init__(self) -> None:
        self.returncode = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_stop_endpoint_marks_session_interrupted():
    sid = "test-stop-endpoint-1"
    rhythm_service.clear_interrupt(sid)
    app.dependency_overrides[_require_authenticated] = lambda: AgentIdentity.for_role(
        agent_id="Alice", role="admin"
    )
    try:
        with patch(
            "src.routers.ui_sessions.session_service.get_session_by_id",
            new=AsyncMock(return_value={"agent_id": "Alice"}),
        ), TestClient(app) as client:
            resp = client.post(f"/v1/ui/sessions/{sid}/stop")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        # 關鍵：端點不再 AttributeError，且旗標確實被設上
        assert rhythm_service.is_interrupted(sid) is True
    finally:
        app.dependency_overrides.clear()
        rhythm_service.clear_interrupt(sid)


def test_stop_endpoint_terminates_processes():
    """硬中斷：/stop 會真正終止該會話登記中的子進程（如執行中的 bash）。"""
    sid = "test-stop-endpoint-kill-1"
    rhythm_service.clear_interrupt(sid)
    process_registry.reload_all()
    proc = _StopFakeProc()
    process_registry.register(sid, proc)

    app.dependency_overrides[_require_authenticated] = lambda: AgentIdentity.for_role(
        agent_id="Alice", role="admin"
    )
    try:
        with patch(
            "src.routers.ui_sessions.session_service.get_session_by_id",
            new=AsyncMock(return_value={"agent_id": "Alice"}),
        ), TestClient(app) as client:
            resp = client.post(f"/v1/ui/sessions/{sid}/stop")
        assert resp.status_code == 200
        assert proc.killed is True
        assert process_registry.count(sid) == 0
    finally:
        app.dependency_overrides.clear()
        rhythm_service.clear_interrupt(sid)
        process_registry.reload_all()
