"""Synthetic identities and real SQLite storage for conversation scope tests."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest_asyncio
from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity
from src.models.session_persistence import SessionChanges
from src.models.user import User
from src.routers import ui_sessions
from src.services.agent_service import agent_service
from src.services.auth_service import AuthService
from src.services.identity_service import identity_service
from src.services.resource_manager import resource_manager
from src.utils import async_db

A = AgentIdentity.for_role("ScopeA", "Owner")
B = AgentIdentity.for_role("ScopeB", "Owner")
C = AgentIdentity.for_role("ScopeC", "Owner")
HUMAN = AgentIdentity.for_role(
    "scope-human", "normal", is_human=True, owned_agents=[A.agent_id, B.agent_id]
)
ADMIN = AgentIdentity.for_role("scope-admin", "admin", is_human=True)
DEBUG = AgentIdentity.for_role("DebugAgent", "Debugger")
EMPTY = {"messages": {}, "currentId": None}
TREE = {
    "messages": {
        "u": {"id": "u", "role": "user", "content": "private A", "parentId": None},
        "a": {"id": "a", "role": "assistant", "content": "answer A", "parentId": "u"},
    },
    "currentId": "a",
}


def passport(value):
    canonical = value.removeprefix("ToukaSoul")
    return {
        "agent_id": canonical,
        "is_toukasoul": canonical in {A.agent_id, B.agent_id, C.agent_id},
        "is_custom": False,
    }


@pytest_asyncio.fixture
async def scope_db(tmp_path, monkeypatch):
    """Only the database location and identity directory are replaced."""
    path = tmp_path / "sessions.db"
    monkeypatch.setattr(async_db, "get_db_path", lambda: path)
    monkeypatch.setattr(agent_service, "identify_agent", passport)
    monkeypatch.setattr(
        agent_service, "get_all_agents", lambda: [{"id": i.agent_id} for i in (A, B, C)]
    )
    monkeypatch.setattr(agent_service, "resolve_role_and_caps", lambda _: ("Owner", []))
    monkeypatch.setattr(
        identity_service,
        "get_identity",
        lambda value: AgentIdentity.for_role(passport(value)["agent_id"], "Owner"),
    )
    monkeypatch.setattr(
        resource_manager,
        "is_owner",
        lambda target, caller: (
            caller.is_admin_authority()
            or (caller.is_human and passport(target.agent_id)["agent_id"] in caller.owned_agents)
        ),
    )
    users = [
        User(
            id=HUMAN.agent_id,
            email="scope@example.com",
            hashed_password="unused",
            owned_agents=HUMAN.owned_agents,
            refresh_token_exp=9999999999,
        ),
        User(
            id="other-human",
            email="other@example.com",
            hashed_password="unused",
            owned_agents=[C.agent_id],
            refresh_token_exp=9999999999,
        ),
    ]
    monkeypatch.setattr(AuthService, "get_all_users", lambda _: users)
    await async_db.init_db_async()
    return SimpleNamespace(path=path, users=users)


async def seed(sid="chat", identity=A, tree=None, **values):
    assert await async_db.session_service.upsert_session(
        sid,
        "private title",
        copy.deepcopy(TREE if tree is None else tree),
        identity=identity,
        owner_user_id=HUMAN.agent_id,
        updatedAt=1000,
        **values,
    )


async def raw_row(sid="chat"):
    async with async_db.get_async_db() as db:
        cursor = await db.execute("SELECT * FROM user_sessions WHERE id = ?", (sid,))
        row = await cursor.fetchone()
        return dict(row) if row is not None else None


async def attempt(operation):
    try:
        return await operation
    except ToukaException as exc:
        return exc


def assert_scope_denied(result):
    if isinstance(result, ToukaException):
        assert result.code.name == "SESSION_SCOPE_MISMATCH"
    else:
        assert getattr(result, "status", None) == "failed", repr(result)
        code = getattr(async_db.ErrorCode, "SESSION_SCOPE_MISMATCH", None)
        assert code is not None, "SESSION_SCOPE_MISMATCH is missing"
        assert result.error_code == ToukaException(code).error_id


async def cas(sid="chat", identity=A, **changes):
    snapshot = await async_db.session_service.read_persistence_snapshot(sid, identity)
    return await async_db.session_service.conditional_save_session(
        sid, snapshot.revision, SessionChanges(**changes), identity
    )


async def ui_save(sid="chat", caller=HUMAN, **changes):
    snapshot = await async_db.session_service.read_persistence_snapshot(sid, caller)
    row = snapshot.session or {}
    values = {
        "id": sid,
        "revision": snapshot.revision,
        "title": row.get("title", "draft"),
        "messages": copy.deepcopy(row.get("messages", EMPTY)),
        "updatedAt": 2000,
    }
    values.update(changes)
    return await ui_sessions.save_session(sid, ui_sessions.SessionModel(**values), caller)
