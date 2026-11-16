"""聊天請求帶的專案編號要核對擁有權（禁令 20 對應安全測試；ADR-0304 2026-11-16 補記）。

修補前的漏洞：
- `/messages`、`/chat/completions`（含 `/v1`）請求帶的 `project_id` 沒檢查是不是請求的人的，
  ContextLayer 直接把那個專案的名稱和根路徑寫進 system。
- 心跳建家用的佔位編號 `default` 沒有真的專案；有家對話的使用者能搶建這個編號，之後所有人的
  家對話都帶到他寫的說明，檔案工具也可能把它當專案。

修補後：router 在換成靈魂身分之前用 `_can_access_project` 核對，不能看就拿掉（不回錯誤），
通過才標 `project_authorized`，ContextLayer 只認這個標記；佔位編號一律當沒有專案，專案頁不能
建立或刪除它。全部離線：服務查詢換成假的，流程本身換成記錄用的替身。
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.middleware.layers import context as context_layer
from src.middleware.layers.context import ContextLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.routers import proxy as proxy_router
from src.services import handover_service as handover_module
from src.services.authorization_gateway import require_authenticated
from src.services.isolation import policy as isolation_policy
from src.utils.async_db import (
    PLACEHOLDER_PROJECT_ID,
    get_async_db,
    init_db_async,
    session_service,
)

OWNER = AgentIdentity.for_role(agent_id="userA", role="Base", is_human=True, capabilities=[])
OTHER = AgentIdentity.for_role(agent_id="userB", role="Base", is_human=True, capabilities=[])
ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True, capabilities=[])
# 非管理員用 X-Agent-ID 代打自己的靈魂時，identity_router 給的是靈魂身分
SOUL_ACTING = AgentIdentity.for_role("soulA", "Owner", is_human=False, capabilities=[])
# 管理員代打：保留管理權（human_role="admin"）、不繼承靈魂角色
ADMIN_ACTING = AgentIdentity(agent_id="soulA", human_role="admin", is_human=False, capabilities=[])

PROJECT_OWNERS: dict[str, str | None] = {
    "p_a": "userA",
    "p_b": "userB",
    PLACEHOLDER_PROJECT_ID: "userB",  # 有人搶建了佔位編號
}
PROJECT_AGENTS: dict[str, list[str]] = {"p_legacy": ["soulA"]}
OWNED_AGENTS = {("userA", "soulA")}
HIDDEN_NAME = "PB-HIDDEN-NAME"
CHAT_PATHS = ["/v1/messages", "/v1/chat/completions", "/messages", "/chat/completions"]


def _payload(path: str, project_id: str | None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": extra.pop("model", "probe-model"),
        "messages": [{"role": "user", "content": "hi"}],
        **extra,
    }
    if path.endswith("messages"):
        body["max_tokens"] = 64
    if project_id is not None:
        body["project_id"] = project_id
    return body


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """假的專案擁有者／對話歸屬，流程換成記錄收到的請求與 ctx。"""
    record: dict[str, Any] = {}

    async def _owner(project_id: str) -> str | None:
        return PROJECT_OWNERS.get(project_id)

    async def _agents(project_id: str) -> list[str]:
        return list(PROJECT_AGENTS.get(project_id, []))

    async def _pipeline(request: Any, ctx: Any, is_anthropic: bool = False) -> dict[str, bool]:
        record["project_id"] = request.project_id
        record["authorized"] = ctx.get("project_authorized")
        return {"ok": True}

    owners = SimpleNamespace(
        is_owner=lambda target, caller: (caller.agent_id, target.agent_id) in OWNED_AGENTS
    )
    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_project_owner", _owner)
    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_project_agent_ids", _agents)
    monkeypatch.setattr("src.services.authorization_gateway.resource_manager", owners)
    monkeypatch.setattr(proxy_router, "process_proxy_request", _pipeline)
    return record


def _post(path: str, identity: AgentIdentity, body: dict[str, Any]) -> Any:
    app.dependency_overrides[require_authenticated] = lambda: identity
    try:
        with TestClient(app) as client:
            return client.post(path, json=body)
    finally:
        app.dependency_overrides.pop(require_authenticated, None)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.name == proxy_router.__name__ and r.levelno >= logging.WARNING
    ]


# ── 別人的專案拿掉，自己能看的保留 ─────────────────────────────────────


@pytest.mark.parametrize("path", CHAT_PATHS)
def test_someone_elses_project_is_dropped(
    path: str, seen: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        resp = _post(path, OTHER, _payload(path, "p_a"))
    assert resp.status_code == 200, resp.text
    assert seen["project_id"] is None
    assert not seen["authorized"]
    logged = " ".join(r.getMessage() for r in _warnings(caplog))
    assert "userB" in logged
    assert "p_a" not in logged  # 只記呼叫者，不記別人的專案
    assert HIDDEN_NAME not in logged


@pytest.mark.parametrize("path", CHAT_PATHS)
@pytest.mark.parametrize(
    ("identity", "project_id"),
    [(OWNER, "p_a"), (ADMIN, "p_b"), (OWNER, "p_legacy"), (ADMIN_ACTING, "p_a")],
    ids=["own", "admin", "legacy-own-session", "admin-acting"],
)
def test_readable_project_is_kept_and_marked(
    path: str, identity: AgentIdentity, project_id: str, seen: dict[str, Any]
) -> None:
    resp = _post(path, identity, _payload(path, project_id))
    assert resp.status_code == 200, resp.text
    assert seen["project_id"] == project_id
    assert seen["authorized"] is True


def test_own_project_kept_when_talking_to_own_soul(seen: dict[str, Any]) -> None:
    # 檢查用的是請求的人，不是之後才換上的靈魂身分
    _post("/v1/chat/completions", OWNER, _payload("/v1/chat/completions", "p_a", model="soulA"))
    assert seen["project_id"] == "p_a"


def test_own_project_kept_in_incognito(seen: dict[str, Any]) -> None:
    body = _payload("/v1/chat/completions", "p_a", incognito=True)
    _post("/v1/chat/completions", OWNER, body)
    assert seen["project_id"] == "p_a"
    assert seen["authorized"] is True


@pytest.mark.parametrize(
    ("identity", "project_id"),
    [(OWNER, "p_missing"), (OWNER, "p_orphan"), (SOUL_ACTING, "p_a")],
    ids=["missing", "orphan", "non-admin-acting"],
)
def test_unreadable_project_is_dropped(
    identity: AgentIdentity, project_id: str, seen: dict[str, Any]
) -> None:
    _post("/v1/chat/completions", identity, _payload("/v1/chat/completions", project_id))
    assert seen["project_id"] is None
    assert not seen["authorized"]


def test_check_failure_drops_the_project_and_logs(
    seen: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        proxy_router, "_can_access_project", AsyncMock(side_effect=RuntimeError("db gone"))
    )
    with caplog.at_level(logging.WARNING):
        _post("/v1/chat/completions", OWNER, _payload("/v1/chat/completions", "p_a"))
    assert seen["project_id"] is None
    assert any(r.exc_info for r in _warnings(caplog))


def test_empty_project_id_is_left_alone(
    seen: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        _post("/v1/chat/completions", OWNER, _payload("/v1/chat/completions", ""))
    assert not seen["project_id"]
    assert not seen["authorized"]
    assert _warnings(caplog) == []


# ── 佔位編號 default 一律當沒有專案 ─────────────────────────────────────


@pytest.mark.parametrize("identity", [OWNER, OTHER, ADMIN], ids=["owner", "squatter", "admin"])
def test_placeholder_project_is_never_used(
    identity: AgentIdentity, seen: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        _post(
            "/v1/chat/completions",
            identity,
            _payload("/v1/chat/completions", PLACEHOLDER_PROJECT_ID),
        )
    assert seen["project_id"] is None
    assert not seen["authorized"]
    assert _warnings(caplog) == []  # 網頁對家對話本來就會送，不算可疑


async def test_tools_do_not_treat_the_placeholder_as_a_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        isolation_policy.session_service,
        "get_session_by_id",
        AsyncMock(return_value={"project_id": PLACEHOLDER_PROJECT_ID}),
    )
    assert await isolation_policy.session_project_id({}, "home-session") is None
    assert await isolation_policy.session_project_id(
        {"project_id": PLACEHOLDER_PROJECT_ID}, None
    ) is None


@pytest.mark.parametrize("identity", [OWNER, ADMIN], ids=["owner", "admin"])
def test_placeholder_project_cannot_be_saved_or_deleted(
    identity: AgentIdentity, seen: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 還沒被搶建、自己的靈魂有家對話綁在佔位編號上：以前一般使用者也能建立或刪除
    monkeypatch.setitem(PROJECT_OWNERS, PLACEHOLDER_PROJECT_ID, None)
    monkeypatch.setitem(PROJECT_AGENTS, PLACEHOLDER_PROJECT_ID, ["soulA"])
    app.dependency_overrides[require_authenticated] = lambda: identity
    try:
        with TestClient(app) as client:
            saved = client.post(
                "/v1/ui/projects/save",
                json={"id": PLACEHOLDER_PROJECT_ID, "name": "x", "root_path": "x"},
            )
            deleted = client.delete(f"/v1/ui/projects/{PLACEHOLDER_PROJECT_ID}")
    finally:
        app.dependency_overrides.pop(require_authenticated, None)
    assert saved.status_code == 403, saved.text
    assert deleted.status_code == 403, deleted.text


# ── 寫說明的那一層只認「專案已核對」 ───────────────────────────────────


@pytest.mark.parametrize("authorized", [True, False])
async def test_context_layer_injects_only_authorized_projects(
    authorized: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        context_layer, "get_project_context", AsyncMock(return_value=f"[{HIDDEN_NAME}]")
    )
    request = SimpleNamespace(
        model="m",
        project_id="p_b",
        ide_context=None,
        messages=[{"role": "user", "content": "hi"}],
    )
    ctx = PipelineContext()
    ctx["project_authorized"] = authorized

    await ContextLayer().process(request, ctx)

    text = " ".join(
        str(m.get("content", "") if isinstance(m, dict) else getattr(m, "content", ""))
        for m in request.messages
    )
    assert (HIDDEN_NAME in text) is authorized



async def test_handover_file_is_never_written_for_the_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 存對話換靈魂時會寫交接檔：佔位編號就算有人搶建了，也不讀它的列、不寫進它的資料夾
    # （函式本身會吞掉例外，所以記錄有沒有被呼叫，而不是靠丟錯）
    opened: list[bool] = []

    def _must_not_open() -> None:
        opened.append(True)
        raise AssertionError("the placeholder project must not be looked up")

    monkeypatch.setattr(handover_module, "get_async_db", _must_not_open)
    await handover_module.handover_service.generate_handover_file(
        project_id=PLACEHOLDER_PROJECT_ID, new_agent_id="soulB", old_agent_id="soulA"
    )
    assert opened == []


async def test_clearing_my_data_keeps_everyones_home_sessions_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 用這條測試自己的資料庫，不在整場共用的沙盒資料庫留下列
    db_path = tmp_path / "ownership.db"
    monkeypatch.setattr("src.utils.async_db.get_db_path", lambda: db_path)
    await init_db_async()
    # 搶建者清除自己的資料：只刪他的列，不解除所有人家對話的綁定（對話版本才不會變）
    async with get_async_db() as db:
        await db.execute(
            "INSERT INTO projects (id, name, root_path, owner_user_id, updatedAt) "
            "VALUES (?, ?, ?, ?, ?)",
            (PLACEHOLDER_PROJECT_ID, "squat", "squat-root", "userB", 1),
        )
        await db.execute(
            "INSERT INTO projects (id, name, root_path, owner_user_id, updatedAt) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p_mine", "mine", "mine-root", "userB", 1),
        )
        await db.execute(
            "INSERT INTO user_sessions (id, title, messages, agent_id, project_id, updatedAt) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("home-a", "home", "[]", "soulA", PLACEHOLDER_PROJECT_ID, 1),
        )
        await db.execute(
            "INSERT INTO user_sessions (id, title, messages, agent_id, project_id, updatedAt) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("work-b", "work", "[]", "soulB", "p_mine", 1),
        )
        await db.commit()

    assert await session_service.clear_projects_owned_by("userB") == 2

    async with get_async_db() as db:
        async with db.execute("SELECT id, project_id FROM user_sessions") as cursor:
            bound = {row["id"]: row["project_id"] for row in await cursor.fetchall()}
        async with db.execute("SELECT id FROM projects") as cursor:
            remaining = {row["id"] for row in await cursor.fetchall()}
    assert bound["home-a"] == PLACEHOLDER_PROJECT_ID
    assert bound["work-b"] is None
    assert PLACEHOLDER_PROJECT_ID not in remaining
