"""POST /v1/chat/upload（Phase 2）端點：沒有代理角色的身分拒絕、型別/大小驗證、回相對路徑。
DebugAgent 身分＋沙盒，零-API。
"""
from __future__ import annotations

import pytest
import src.services.upload_service as us
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

DEBUG = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", capabilities=[])

# ADR-0245：Guest 退役後，原本被擋的自訂模型等代理身分解析為 Base。上傳只放行人類、
# admin 代打與 Owner／Debugger；改名前被擋的 Guest 也仍被擋（不比原本寬）。
_UPLOAD_SHAPES = {
    "human_admin": AgentIdentity.for_role("H1", "admin", is_human=True),
    "human_normal": AgentIdentity.for_role("H2", "normal", is_human=True),
    "assumed_admin": AgentIdentity(agent_id="A1", human_role="admin", is_human=False),
    "agent_owner": AgentIdentity.for_role("A2", "Owner"),
    "agent_debugger": AgentIdentity.for_role("A3", "Debugger"),
    "agent_base": AgentIdentity.for_role("A4", "Base"),
    "agent_subagent": AgentIdentity.for_role("A5", "Subagent"),
    "agent_system": AgentIdentity.for_role("A6", "System"),
    "agent_no_role": AgentIdentity(agent_id="A7"),
    "agent_retired_guest": AgentIdentity(agent_id="A8", agent_role="Guest"),
}
_UPLOAD_ALLOWED = {"human_admin", "human_normal", "assumed_admin", "agent_owner", "agent_debugger"}


def _post(identity, *, file, data):
    app.dependency_overrides[get_verified_identity] = lambda: identity
    try:
        with TestClient(app) as client:
            return client.post("/v1/chat/upload", files=file, data=data)
    finally:
        app.dependency_overrides.clear()


def test_upload_text_ok():
    resp = _post(
        DEBUG, file={"file": ("notes.md", b"# hello", "text/markdown")}, data={"session_id": "s1"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"] == "notes.md"
    assert body["relpath"] == "outputs/uploads/s1/notes.md"
    assert str(body["mime"]).startswith("text/plain")
    assert body["size"] == len(b"# hello")


def test_upload_unsupported_type_422():
    resp = _post(
        DEBUG,
        file={"file": ("x.exe", b"\x00\x01\x02", "application/octet-stream")},
        data={"session_id": "s1"},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("shape", sorted(_UPLOAD_SHAPES))
def test_upload_admits_only_humans_admin_owner_debugger(shape):
    resp = _post(
        _UPLOAD_SHAPES[shape],
        file={"file": ("notes.md", b"hi", "text/markdown")},
        data={"session_id": "s1"},
    )
    assert resp.status_code == (200 if shape in _UPLOAD_ALLOWED else 403)


def test_upload_too_large_413(monkeypatch):
    monkeypatch.setattr(us, "UPLOAD_MAX_BYTES", 4)
    resp = _post(
        DEBUG,
        file={"file": ("notes.md", b"way too big", "text/markdown")},
        data={"session_id": "s1"},
    )
    assert resp.status_code == 413
