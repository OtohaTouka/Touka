"""/stats 依身分過濾（2026-10-28、10-29 安全修復）。

SECURITY.md「統計端點的背景工作與用量沒有依身分過濾」「統計端點的 token 用量看得到別的靈魂」。

背景紀錄（background_activity、interrupted_runs）只列呼叫者看得到的靈魂，規則是
``authorization_gateway.can_access_session``：admin（含代打）與 Debugger 全看、人類看自己擁有的、
代理只看自己、對不到靈魂的紀錄只給 admin／Debugger。早期用量紀錄（usage_history、total_usage）
與派工伺服器的 pid 只給 admin。token_usage（2026-10-29）在 ``list_all_agent_stats`` 給的範圍內
再用同一個判斷過濾：Owner 代理只看自己。

擁有權判斷全用真的；沙箱沒有 users.json，只替換 ``AuthService.get_user_by_id``。
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.models.user import User
from src.services.activity_registry import activity_registry
from src.services.auth_service import AuthService
from src.services.autonomy import autonomy_service
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from collections.abc import Iterator

USERS = {
    "user-a": User(id="user-a", email="a@test.com", hashed_password="...", owned_agents=["Alice"]),
    "user-b": User(id="user-b", email="b@test.com", hashed_password="...", owned_agents=["Bob"]),
    "user-c": User(id="user-c", email="c@test.com", hashed_password="...", owned_agents=[]),
}

HUMAN_ADMIN = AgentIdentity.for_role("admin-user", "admin", is_human=True)
# admin 代打：照 identity_router.py admin 分支的形狀（human_role="admin"、is_human=False）
ADMIN_ASSUMING = AgentIdentity(agent_id="Alice", human_role="admin", is_human=False)
# 一般使用者帶 X-Agent-ID 切到自己的靈魂：照 identity_router.py 非 admin 分支
# （靈魂經 tag_rules 得到 Owner）
OWNER_AGENT = AgentIdentity.for_role("Alice", "Owner")
DEBUGGER = AgentIdentity.for_role("DebugAgent", "Debugger")

ACTIVITY = {
    "sess-alice": {"kind": "heartbeat", "current": 1, "total": 3, "agent": "Alice"},
    "sess-bob": {"kind": "heartbeat", "current": 2, "total": 5, "agent": "Bob"},
}
RUNS = [
    {"session_id": "sess-alice-old", "agent": "Alice", "total": 3},
    {"session_id": "sess-bob-old", "agent": "Bob", "total": 4},
]


def _human(user_id: str) -> AgentIdentity:
    return AgentIdentity.for_role(
        user_id, "normal", is_human=True, owned_agents=list(USERS[user_id].owned_agents)
    )


class _FakeSupervisor:
    """有 pid 的派工伺服器替身（沙箱的派工伺服器是停用的，pid 本來就是 null）。"""

    def status(self) -> dict[str, Any]:
        return {"status": "running", "pid": 4321, "restarts": 2}


@pytest.fixture
def user_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把使用者查詢換成 USERS，並記下每次查的使用者 id。"""
    calls: list[str] = []

    def _get_user_by_id(self: AuthService, user_id: str) -> User | None:
        calls.append(user_id)
        return USERS.get(user_id)

    monkeypatch.setattr(AuthService, "get_user_by_id", _get_user_by_id)
    return calls


@pytest.fixture
def client(user_lookups: list[str]) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        # 啟動掃描會重設 _interrupted_runs，紀錄要在啟動後才放
        activity_registry.reload_all()
        autonomy_service._interrupted_runs = []
        try:
            yield test_client
        finally:
            activity_registry.reload_all()
            autonomy_service._interrupted_runs = []
            app.dependency_overrides.clear()


def _seed(activity: dict[str, Any] = ACTIVITY, runs: list[Any] = RUNS) -> None:
    for session_id, record in activity.items():
        activity_registry.set_activity(session_id, record)
    autonomy_service._interrupted_runs = list(runs)


def _stats(client: TestClient, identity: AgentIdentity) -> dict[str, Any]:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    resp = client.get("/v1/stats")
    assert resp.status_code == 200
    return resp.json()


def _seen(body: dict[str, Any]) -> tuple[set[str], set[str]]:
    """回傳（background_activity 的 session id、interrupted_runs 的 session id）。

    interrupted_runs 裡不是 dict 的項目原樣放進集合。
    """
    runs = {r.get("session_id") if isinstance(r, dict) else r for r in body["interrupted_runs"]}
    return set(body["background_activity"]), runs


def test_normal_user_sees_only_own_soul(client: TestClient) -> None:
    _seed()
    activity, runs = _seen(_stats(client, _human("user-a")))
    assert activity == {"sess-alice"}
    assert runs == {"sess-alice-old"}


@pytest.mark.parametrize(
    "identity", [HUMAN_ADMIN, ADMIN_ASSUMING, DEBUGGER], ids=["admin", "admin-assuming", "debugger"]
)
def test_admin_and_debugger_see_everything(client: TestClient, identity: AgentIdentity) -> None:
    _seed()
    activity, runs = _seen(_stats(client, identity))
    assert activity == {"sess-alice", "sess-bob"}
    assert runs == {"sess-alice-old", "sess-bob-old"}


def test_owner_agent_sees_only_itself(client: TestClient) -> None:
    """一般使用者切到自己的靈魂（Owner 代理）：只看那個靈魂，也拿不到 admin 才有的欄位。"""
    _seed()
    body = _stats(client, OWNER_AGENT)
    assert _seen(body) == ({"sess-alice"}, {"sess-alice-old"})
    assert body["usage_history"] == []
    assert body["total_usage"] == 0


def test_user_without_souls_sees_nothing(client: TestClient) -> None:
    _seed()
    body = _stats(client, _human("user-c"))
    assert body["background_activity"] == {}
    assert body["interrupted_runs"] == []


def test_records_without_agent_only_for_admin_and_debugger(client: TestClient) -> None:
    activity = {
        "sess-no-agent": {"kind": "heartbeat"},
        "sess-empty": {"kind": "heartbeat", "agent": ""},
        "sess-number": {"kind": "heartbeat", "agent": 42},
        "sess-not-dict": "heartbeat",
    }
    runs = [
        {"session_id": "run-no-agent"},
        {"session_id": "run-none", "agent": None},
        "run-not-dict",
    ]
    _seed(activity, runs)

    assert _seen(_stats(client, _human("user-a"))) == (set(), set())
    assert _seen(_stats(client, OWNER_AGENT)) == (set(), set())
    for identity in (HUMAN_ADMIN, DEBUGGER):
        assert _seen(_stats(client, identity)) == (
            set(activity),
            {"run-no-agent", "run-none", "run-not-dict"},
        )


def test_filtering_does_not_change_shared_state(client: TestClient) -> None:
    _seed()
    _stats(client, _human("user-a"))
    activity, runs = _seen(_stats(client, HUMAN_ADMIN))
    assert activity == {"sess-alice", "sess-bob"}
    assert runs == {"sess-alice-old", "sess-bob-old"}


def test_ownership_checked_once_per_soul(client: TestClient, user_lookups: list[str]) -> None:
    """右側欄每 8 秒讀一次：同一個靈魂的多筆紀錄只查一次使用者。"""
    _seed(
        {
            "sess-alice-1": {"kind": "heartbeat", "agent": "Alice"},
            "sess-alice-2": {"kind": "heartbeat", "agent": "Alice"},
        },
        [{"session_id": "sess-alice-old", "agent": "Alice"}],
    )
    user_lookups.clear()
    activity, runs = _seen(_stats(client, _human("user-a")))
    assert activity == {"sess-alice-1", "sess-alice-2"}
    assert runs == {"sess-alice-old"}
    assert user_lookups == ["user-a"]


@pytest.fixture
def usage_audit_csv() -> Iterator[Path]:
    """沙箱裡的全域稽核檔，兩行資料。"""
    path = Path(resource_manager.get_base_path()) / "stats" / "usage_audit.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("timestamp,agent_id,request_id\n1,Alice,req-1\n2,Bob,req-2\n", encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.parametrize("identity", [HUMAN_ADMIN, ADMIN_ASSUMING], ids=["admin", "admin-assuming"])
def test_usage_history_for_admin(
    client: TestClient, usage_audit_csv: Path, identity: AgentIdentity
) -> None:
    body = _stats(client, identity)
    assert body["total_usage"] == 2
    assert [row["request_id"] for row in body["usage_history"]] == ["req-1", "req-2"]


@pytest.mark.parametrize(
    "identity", [_human("user-a"), OWNER_AGENT, DEBUGGER], ids=["user", "owner-agent", "debugger"]
)
def test_usage_history_hidden_from_non_admin(
    client: TestClient,
    usage_audit_csv: Path,
    identity: AgentIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_paths: list[str] = []
    real_read = resource_manager.read_file_text

    def _recording_read(path: Any, *args: Any, **kwargs: Any) -> Any:
        read_paths.append(str(path))
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(resource_manager, "read_file_text", _recording_read)
    body = _stats(client, identity)
    assert body["usage_history"] == []
    assert body["total_usage"] == 0
    assert not any(p.endswith("usage_audit.csv") for p in read_paths)


@pytest.mark.parametrize(
    ("identity", "pid"),
    [
        (HUMAN_ADMIN, 4321),
        (ADMIN_ASSUMING, 4321),
        (_human("user-a"), None),
        (OWNER_AGENT, None),
        (DEBUGGER, None),
    ],
    ids=["admin", "admin-assuming", "user", "owner-agent", "debugger"],
)
def test_dispatch_supervisor_pid_only_for_admin(
    client: TestClient, identity: AgentIdentity, pid: int | None
) -> None:
    original = client.app.state.dispatch_supervisor
    client.app.state.dispatch_supervisor = _FakeSupervisor()
    try:
        field = _stats(client, identity)["dispatch_supervisor"]
    finally:
        client.app.state.dispatch_supervisor = original
    assert field == {"status": "running", "pid": pid, "restarts": 2}


@pytest.fixture
def token_usage_files() -> Iterator[dict[str, Path]]:
    """沙箱裡三個代理的 token_usage.json；結束後還原原本的內容。

    第三個用專屬名稱，不碰沙箱裡常用的 DebugAgent。
    """
    stats_root = Path(resource_manager.get_base_path()) / "stats"
    paths: dict[str, Path] = {}
    originals: dict[Path, bytes | None] = {}
    created_dirs: list[Path] = []
    for agent in ("Alice", "Bob", "StatsScopeOther"):
        path = stats_root / agent / "token_usage.json"
        originals[path] = path.read_bytes() if path.exists() else None
        if not path.parent.exists():
            path.parent.mkdir(parents=True)
            created_dirs.append(path.parent)
        path.write_text(f'{{"total_tokens": {len(agent)}}}', encoding="utf-8")
        paths[agent] = path
    try:
        yield paths
    finally:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        for directory in created_dirs:
            with contextlib.suppress(OSError):
                directory.rmdir()


@pytest.mark.parametrize("identity", [HUMAN_ADMIN, DEBUGGER], ids=["admin", "debugger"])
def test_token_usage_all_for_admin_and_debugger(
    client: TestClient, token_usage_files: dict[str, Path], identity: AgentIdentity
) -> None:
    usage = _stats(client, identity)["token_usage"]
    # 沙箱可能還有別的測試留下的用量檔，只核對這三個
    assert {agent: usage.get(agent) for agent in token_usage_files} == {
        agent: {"total_tokens": len(agent)} for agent in token_usage_files
    }


def test_token_usage_owner_agent_sees_only_itself(
    client: TestClient,
    token_usage_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner 代理只看自己的用量；別的靈魂的檔案不讀，名字也不出現在回應裡。

    同一次請求也有背景紀錄，確認兩邊共用的判斷結果互不影響。
    """
    _seed()
    read_paths: list[str] = []
    real_read = resource_manager.read_json

    def _recording_read(path: Any, *args: Any, **kwargs: Any) -> Any:
        read_paths.append(str(path))
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(resource_manager, "read_json", _recording_read)
    body = _stats(client, OWNER_AGENT)
    assert body["token_usage"] == {"Alice": {"total_tokens": 5}}
    assert _seen(body) == ({"sess-alice"}, {"sess-alice-old"})
    usage_reads = [p for p in read_paths if p.endswith("token_usage.json")]
    assert usage_reads == [str(token_usage_files["Alice"])]


@pytest.mark.parametrize(
    "identity",
    [_human("user-a"), ADMIN_ASSUMING],
    ids=["user", "admin-assuming"],
)
def test_token_usage_not_widened(
    client: TestClient, token_usage_files: dict[str, Path], identity: AgentIdentity
) -> None:
    """這次只收窄：原本拿不到用量的身分（一般使用者、admin 代打）仍然是空的。"""
    assert _stats(client, identity)["token_usage"] == {}
