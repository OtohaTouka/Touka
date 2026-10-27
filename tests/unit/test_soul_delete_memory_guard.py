"""刪除靈魂前檢查記憶：還有記憶（事實庫任何狀態的列、或向量 collection 還在）就 409 擋下、
什麼都不刪，引導先重置；重置後可刪；沒記憶直接可刪；記憶系統不可用時 503（不能確認就不刪）。

避免孤兒記憶、以及同名新靈魂接手舊記憶。權限順序不變：步升（admin＋密碼）先過，才查記憶。
事實庫與向量庫都換成 tmp_path 裡的實例；不碰實體 data/。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from memory_server import tools as memory_gateway
from memory_server.factual import ArchiveResult, FactualStore
from memory_server.services.manager import MemoryManager
from memory_server.vector import VectorStore
from qdrant_client.models import Distance, PointStruct, VectorParams
from src.main import app
from src.models.identity import AgentIdentity
from src.services import system_backup
from src.services.agent_reset import delete_guard
from src.services.agent_reset import orchestrator as O
from src.services.agent_reset.primitives import MEMORY_STORES
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager
from src.utils.async_db import init_db_async
from src.utils.continuity_events_db import EventRow, count_agent_events, insert_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

ADMIN_PW = "GuardPass123!"
_FAKE_BACKUP = {"status": "success", "message": "ok", "output": "backups/x.zip"}


def _soul_dir(name: str) -> Path:
    path = Path(resource_manager.get_base_path()) / "soul" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(autouse=True)
async def _activity_db(tmp_path: Path) -> AsyncIterator[None]:
    """活動事件表在 tmp 的 ui_sessions.db（Proxy 啟動時 init_db_async 會建）。"""
    with patch("src.utils.async_db.get_db_path", return_value=tmp_path / "sessions.db"):
        await init_db_async()
        yield


async def _activity(name: str) -> None:
    await insert_event(
        EventRow(
            event_id=f"user_turn:{name}:m1",
            agent=name,
            kind="user_turn",
            occurred_at=1,
            subject="m1",
        )
    )


@pytest.fixture
def stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[FactualStore, VectorStore]]:
    factual = FactualStore(base_dir=tmp_path / "vault")
    vector_dir = tmp_path / "vectors"
    vector = VectorStore(base_dir=vector_dir, model_name="sandbox-without-model")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", factual)
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", vector)
    monkeypatch.setattr(memory_gateway, "_global_manager", MemoryManager())
    yield factual, vector
    VectorStore._client_cache.pop(str((vector_dir / "_qdrant_db").absolute())).close()


@pytest.fixture
def admin(request: pytest.FixtureRequest) -> Iterator[tuple[TestClient, AgentIdentity]]:
    slug = "".join(c for c in request.node.name if c.isalnum())
    user = AuthService().create_user(f"guard-{slug}@test.com", ADMIN_PW, role="admin")
    identity = AgentIdentity.for_role(user.id, "admin", is_human=True, owned_agents=[])
    app.dependency_overrides[get_verified_identity] = lambda: identity
    yield TestClient(app, raise_server_exceptions=False), identity
    app.dependency_overrides.clear()


def _delete(client: TestClient, name: str, password: str = ADMIN_PW):
    return client.request("DELETE", f"/v1/souls/{name}", json={"admin_password": password})


def _collection(vector: VectorStore, name: str, points: int) -> None:
    collection = vector.get_collection_name(AgentIdentity(agent_id=name))
    vector.client.create_collection(
        collection_name=collection, vectors_config=VectorParams(size=3, distance=Distance.COSINE)
    )
    if points:
        vector.client.upsert(
            collection_name=collection,
            points=[PointStruct(id=i, vector=[1.0, 0.5, i], payload={}) for i in range(points)],
        )


def _seed(kind: str, name: str, factual: FactualStore, vector: VectorStore) -> None:
    identity = AgentIdentity(agent_id=name)
    if kind in {"fact", "archived_fact"}:
        saved = factual.save_fact(identity, f"{name} likes tea")
        if kind == "archived_fact":
            assert factual.archive_fact(identity, saved["id"]) is ArchiveResult.ARCHIVED
    elif kind == "vectors":
        _collection(vector, name, points=2)
    elif kind == "empty_collection":
        _collection(vector, name, points=0)


# ── 判定表：哪些殘留會擋 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind",
    ["fact", "archived_fact", "vectors", "empty_collection"],
)
def test_delete_is_blocked_while_memory_remains(kind, stores, admin):
    factual, vector = stores
    client, _identity = admin
    name = f"Keep{kind.title().replace('_', '')}"
    soul = _soul_dir(name)
    _seed(kind, name, factual, vector)

    resp = _delete(client, name)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "SOUL_MEMORY_NOT_RESET"
    assert detail["i18n"] == "error.soul.memory_not_reset"
    assert soul.exists()  # 擋下時什麼都不刪
    assert factual.count_agent_facts(AgentIdentity(agent_id=name)) == (
        1 if "fact" in kind else 0
    )


def test_delete_without_memory_succeeds(stores, admin):
    client, _identity = admin
    soul = _soul_dir("NoMemorySoul")

    resp = _delete(client, "NoMemorySoul")

    assert resp.status_code == 200, resp.text
    assert not soul.exists()


async def test_reset_then_delete_succeeds(stores, admin, monkeypatch):
    factual, vector = stores
    client, identity = admin
    soul = _soul_dir("ResetFirst")
    _seed("archived_fact", "ResetFirst", factual, vector)
    _seed("vectors", "ResetFirst", factual, vector)
    assert _delete(client, "ResetFirst").status_code == 409

    monkeypatch.setattr(system_backup, "create_backup", lambda: _FAKE_BACKUP)
    monkeypatch.setattr(O, "ALL_STORES", MEMORY_STORES[:1])  # 只跑記憶這一項
    report = await O.reset_agent_data("ResetFirst", identity)
    assert report.clean is True

    resp = _delete(client, "ResetFirst")
    assert resp.status_code == 200, resp.text
    assert not soul.exists()


# ── 活動紀錄（可信活動事件，屬「記憶」類別）也要先重置 ─────────────────────────────


async def test_delete_is_blocked_while_activity_records_remain(stores, admin):
    client, _identity = admin
    soul = _soul_dir("KeepTimeline")
    await _activity("KeepTimeline")

    resp = _delete(client, "KeepTimeline")

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "SOUL_MEMORY_NOT_RESET"
    assert detail["activity_events"] == 1
    assert soul.exists()
    assert await count_agent_events(AgentIdentity(agent_id="KeepTimeline")) == 1


async def test_reset_with_the_timeline_then_delete_succeeds(stores, admin, monkeypatch):
    client, identity = admin
    soul = _soul_dir("ResetTimeline")
    await _activity("ResetTimeline")
    assert _delete(client, "ResetTimeline").status_code == 409

    monkeypatch.setattr(system_backup, "create_backup", lambda: _FAKE_BACKUP)
    monkeypatch.setattr(O, "ALL_STORES", MEMORY_STORES[:2])  # 記憶＋活動時間軸
    report = await O.reset_agent_data("ResetTimeline", identity)
    assert report.clean is True

    resp = _delete(client, "ResetTimeline")
    assert resp.status_code == 200, resp.text
    assert not soul.exists()


def test_unreadable_activity_record_blocks_delete(stores, admin, monkeypatch):
    client, _identity = admin
    soul = _soul_dir("UnknownTimeline")
    monkeypatch.setattr(
        delete_guard, "count_agent_events", AsyncMock(side_effect=ConnectionError("db"))
    )

    resp = _delete(client, "UnknownTimeline")

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "SOUL_MEMORY_CHECK_FAILED"
    assert "ConnectionError" not in resp.text
    assert soul.exists()


def test_memory_system_unavailable_blocks_delete(stores, admin, monkeypatch):
    client, _identity = admin
    soul = _soul_dir("UnknownMemory")

    def _down() -> object:
        raise ConnectionError

    monkeypatch.setattr(delete_guard, "_memory_manager", _down)

    resp = _delete(client, "UnknownMemory")

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "SOUL_MEMORY_CHECK_FAILED"
    assert "ConnectionError" not in resp.text
    assert soul.exists()


# ── 權限順序不變：步升先過，才查記憶（也不洩漏記憶是否存在）─────────────────────


def test_wrong_password_is_denied_before_the_memory_check(stores, admin):
    factual, vector = stores
    client, _identity = admin
    soul = _soul_dir("PwSoul")
    _seed("fact", "PwSoul", factual, vector)

    resp = _delete(client, "PwSoul", password="WRONG")

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "AUTH_REAUTH_FAILED"
    assert soul.exists()


def test_non_admin_is_denied_before_the_memory_check(stores):
    factual, vector = stores
    user = AuthService().create_user("guard-normal@test.com", ADMIN_PW, role="normal")
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        user.id, "normal", is_human=True
    )
    soul = _soul_dir("NormalSoul")
    _seed("fact", "NormalSoul", factual, vector)
    try:
        resp = _delete(TestClient(app, raise_server_exceptions=False), "NormalSoul")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "AUTH_ADMIN_REQUIRED"
    assert soul.exists()
