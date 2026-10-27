"""舊對話歸檔直接寫舊對話庫；同步不搬檔、以 (檔名, 內容雜湊) 記已處理。

背景：`legacy/` 受 `resource_manager.is_protected_path` 保護，系統先寫 JSON 中繼檔再搬檔的
做法在正式環境一律被擋。改成經 LegacyStore 一個交易寫資料庫；保護不放寬。

Memory 端用 tmp_path；Proxy 端點在 conftest 沙盒的真實 ResourceManager 上跑，不換假檔案層。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi.testclient import TestClient
from memory_server.services import legacy_store as legacy_store_module
from memory_server.services import legacy_sync as legacy_sync_module
from memory_server.services.legacy_store import LegacyStore
from memory_server.services.legacy_sync import LegacySyncService
from memory_server.services.manager import MemoryManager
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager

sqlite3 = legacy_store_module.sqlite3  # 測試只讀寫 tmp_path 的庫（同其他舊對話測試）
IDENTITY = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
_REPO = Path(__file__).resolve().parents[2]


def _service(directory: Path) -> LegacySyncService:
    manager = create_autospec(MemoryManager, instance=True)
    manager.get_legacy_dir.return_value = directory
    return LegacySyncService(manager)


def _payload(*messages: dict[str, Any], session_id: str = "session-a") -> dict[str, Any]:
    return {"id": session_id, "title": "session a", "messages": list(messages)}


def _write(directory: Path, data: Any, name: str = "chat-a.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _rows(db: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchall()


def _messages(directory: Path) -> dict[str, str]:
    return dict(_rows(directory / "legacy_history.db", "SELECT id, content FROM messages"))


def _with_meta(directory: Path) -> list[tuple[str, str, dict]]:
    rows = _rows(directory / "legacy_history.db", "SELECT id, content, metadata FROM messages")
    return [(msg_id, content, json.loads(meta or "{}")) for msg_id, content, meta in rows]


def _originals(directory: Path) -> dict[str, str]:
    """不是版本列的訊息：id → 內容。"""
    return {i: c for i, c, meta in _with_meta(directory) if "version_of" not in meta}


def _versions(directory: Path) -> dict[str, list[str]]:
    """版本列：原 id → 內容（依內容排序）。"""
    out: dict[str, list[str]] = {}
    for _, content, meta in _with_meta(directory):
        if "version_of" in meta:
            out.setdefault(meta["version_of"], []).append(content)
    return {k: sorted(v) for k, v in out.items()}


_HELLO = {"id": "m1", "role": "user", "content": "hello"}
_HI = {"id": "m2", "role": "assistant", "content": "hi"}


# ── LegacyStore.archive ─────────────────────────────────────────────────────


_SESSION = {"id": "s", "filename": "f", "title": "old", "created_at": 1, "updated_at": 1}


def _counts(db: Path) -> list[list[tuple]]:
    return [_rows(db, sql) for sql in ("SELECT COUNT(*) FROM sessions",
                                       "SELECT COUNT(*) FROM messages",
                                       "SELECT COUNT(*) FROM processed_files")]


def test_archive_is_one_transaction(tmp_path: Path) -> None:
    """任一列寫不進去（role 是 NOT NULL）→ sessions、messages、已處理紀錄全部不留。"""
    store = LegacyStore(str(tmp_path / "legacy_history.db"))
    with pytest.raises(sqlite3.IntegrityError):
        store.archive(
            IDENTITY,
            [_SESSION],
            [{**_HELLO, "session_id": "s"}, {"id": "bad", "session_id": "s", "role": None,
                                            "content": "x"}],
            source=("f.json", "0" * 64),
        )
    assert _counts(tmp_path / "legacy_history.db") == [[(0,)], [(0,)], [(0,)]]


def test_archive_keeps_a_changed_message_as_another_version(tmp_path: Path) -> None:
    """同 id、不同內容再歸檔一次：原內容不變，新內容另存成這則訊息的版本（舊審查報告乙-1）.

    版本列的 id 由原 id 與內容算出，metadata 記 version_of；重跑不多存一份；session 標題照常更新。
    """
    db = tmp_path / "legacy_history.db"
    store = LegacyStore(str(db))
    store.archive(IDENTITY, [_SESSION], [{**_HELLO, "session_id": "s"}])
    renamed = {**_SESSION, "title": "new", "updated_at": 2}
    changed = [{**_HELLO, "content": "changed", "session_id": "s"}]
    first = store.archive(IDENTITY, [renamed], changed)
    again = store.archive(IDENTITY, [renamed], changed)

    assert (first.confirmed, first.versions) == (1, 1)
    assert (again.confirmed, again.versions) == (1, 1)
    assert _originals(tmp_path) == {"m1": "hello"}
    assert _versions(tmp_path) == {"m1": ["changed"]}
    assert _rows(db, "SELECT title FROM sessions") == [("new",)]


@pytest.mark.parametrize(
    "change", [{"session_id": "other"}, {"role": "assistant"}], ids=["session", "role"]
)
def test_same_id_in_another_session_or_role_is_also_a_version(
    tmp_path: Path, change: dict[str, str]
) -> None:
    """messages.id 是全域主鍵：同 id 但 session 或角色不同，也另存成版本，不當成同一則。"""
    db = tmp_path / "legacy_history.db"
    store = LegacyStore(str(db))
    store.archive(IDENTITY, [_SESSION], [{**_HELLO, "session_id": "s"}])
    outcome = store.archive(IDENTITY, [_SESSION], [{**_HELLO, "session_id": "s", **change}])

    assert outcome.versions == 1
    assert _rows(db, "SELECT COUNT(*) FROM messages") == [(2,)]


def test_archiving_the_same_messages_again_confirms_them(tmp_path: Path) -> None:
    """已經在庫、內容相同的訊息算確認在庫（不是「這次插入幾筆」）.

    剪裁存 session 失敗後重剪靠這個。
    """
    store = LegacyStore(str(tmp_path / "legacy_history.db"))
    batch = [{**_HELLO, "session_id": "s"}, {**_HI, "session_id": "s"}]
    store.archive(IDENTITY, [_SESSION], batch)
    outcome = store.archive(IDENTITY, [_SESSION], batch)

    assert (outcome.confirmed, outcome.versions) == (2, 0)
    assert _messages(tmp_path) == {"m1": "hello", "m2": "hi"}


def test_a_version_id_that_reads_back_different_rolls_everything_back(tmp_path: Path) -> None:
    """版本列寫不進去（同一個版本 id 已存著別的內容）→ 丟錯、整筆 rollback，不回成功。"""
    db = tmp_path / "legacy_history.db"
    store = LegacyStore(str(db))
    store.archive(IDENTITY, [_SESSION], [{**_HELLO, "session_id": "s"}])
    changed = {**_HELLO, "content": "changed", "session_id": "s"}
    version_id = legacy_store_module._version_row(legacy_store_module._message_row(changed))[0]
    store.upsert_messages([{"id": version_id, "session_id": "s", "role": "user",
                            "content": "squatter"}])
    before = _counts(db)

    with pytest.raises(legacy_store_module.LegacyArchiveMismatchError):
        store.archive(IDENTITY, [{**_SESSION, "title": "new"}],
                      [changed, {**_HI, "session_id": "s"}], source=("f.json", "1" * 64))

    assert _counts(db) == before
    assert _rows(db, "SELECT title FROM sessions") == [("old",)]
    assert "m2" not in _messages(tmp_path)


@pytest.mark.parametrize(
    ("content", "stored"),
    [(None, ""), ([{"type": "text", "text": "hi"}], '[{"type": "text", "text": "hi"}]'),
     ({"k": 1}, '{"k": 1}')],
    ids=["none", "list", "dict"],
)
def test_content_that_is_not_text_is_stored_as_text(
    tmp_path: Path, content: Any, stored: str
) -> None:
    """節點內容不是字串（None、多模態 list）也能歸檔：None 存空字串、其他存成 JSON 文字."""
    store = LegacyStore(str(tmp_path / "legacy_history.db"))
    store.archive(IDENTITY, [_SESSION], [{**_HELLO, "content": content, "session_id": "s"}])
    assert _messages(tmp_path) == {"m1": stored}


def test_deterministic_ids_still_use_the_original_content(tmp_path: Path) -> None:
    """沒有 id 的訊息：決定性 id 仍由原始內容算（正規化之前），舊紀錄的 id 不變。"""
    parts = [{"type": "text", "text": "hi"}]
    _service(tmp_path).archive_payload(
        IDENTITY, _payload({"role": "user", "content": parts}), "x.json"
    )
    expected = legacy_sync_module._stable_message_id("session-a", 0, parts)
    assert list(_messages(tmp_path)) == [expected]


def test_upsert_messages_behaviour_is_unchanged(tmp_path: Path) -> None:
    """既有 upsert_messages（其他呼叫端用）照舊覆寫內容。"""
    store = LegacyStore(str(tmp_path / "legacy_history.db"))
    store.upsert_messages([{**_HELLO, "session_id": "s"}])
    store.upsert_messages([{**_HELLO, "content": "changed", "session_id": "s"}])
    assert _messages(tmp_path) == {"m1": "changed"}


@pytest.mark.parametrize("state", ["missing", "old_db"])
def test_processed_keys_is_read_only(tmp_path: Path, state: str) -> None:
    """沒有庫、或還沒有新表的舊庫：回空集合，不建檔、不建表。"""
    db = tmp_path / "legacy_history.db"
    if state == "old_db":
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    assert LegacyStore(str(db)).processed_keys() == set()
    if state == "missing":
        assert not db.exists()
    else:
        tables = {name for (name,) in _rows(db, "SELECT name FROM sqlite_master")}
        assert "processed_files" not in tables


# ── 同步：不搬檔、記已處理、冪等 ────────────────────────────────────────────


def test_sync_keeps_json_in_place_and_records_it(tmp_path: Path) -> None:
    source = _write(tmp_path, _payload(_HELLO, _HI))
    result = _service(tmp_path).sync_all_json_to_db(IDENTITY)
    assert (result["synced"], result["skipped"], result["errors"]) == (1, 0, 0)
    assert source.exists() and not (tmp_path / "backup").exists()
    assert _messages(tmp_path) == {"m1": "hello", "m2": "hi"}
    assert _service(tmp_path).file_status(IDENTITY) == {"processed": 1, "pending": 0}


@pytest.mark.parametrize(
    ("second", "synced", "skipped", "conflicts", "versions"),
    [
        # 同一個檔重跑：跳過、零寫入
        (None, 0, 1, 0, {}),
        # 同名改內容：重做；同 id 的不被覆寫，改過的另存成版本，新訊息加進來（乙-1）
        (_payload({**_HELLO, "content": "edited"}, _HI, {"id": "m3", "role": "user",
                                                          "content": "more"}),
         1, 0, 1, {"m1": ["edited"]}),
    ],
)
def test_sync_rerun_is_idempotent(
    tmp_path: Path, second: dict | None, synced: int, skipped: int, conflicts: int,
    versions: dict,
) -> None:
    _write(tmp_path, _payload(_HELLO, _HI))
    service = _service(tmp_path)
    assert service.sync_all_json_to_db(IDENTITY)["conflicts"] == 0
    if second is not None:
        _write(tmp_path, second)
    result = service.sync_all_json_to_db(IDENTITY)
    assert (result["synced"], result["skipped"], result["errors"]) == (synced, skipped, 0)
    assert result["conflicts"] == conflicts
    expected = {"m1": "hello", "m2": "hi"} | ({"m3": "more"} if second else {})
    assert _originals(tmp_path) == expected
    assert _versions(tmp_path) == versions


def test_messages_without_id_get_deterministic_ids(tmp_path: Path) -> None:
    """沒 id 的訊息用決定性 id：同內容換檔名再同步一次，不多一份。"""
    data = _payload({"role": "user", "content": "no id"}, {"role": "assistant", "content": "x"})
    _write(tmp_path, data, "a.json")
    service = _service(tmp_path)
    service.sync_all_json_to_db(IDENTITY)
    _write(tmp_path, data, "b.json")
    assert service.sync_all_json_to_db(IDENTITY)["synced"] == 1
    assert len(_messages(tmp_path)) == 2


def test_sync_failure_leaves_no_record_and_can_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, _payload(_HELLO))
    service = _service(tmp_path)

    def _locked(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError

    monkeypatch.setattr(LegacyStore, "archive", _locked)
    assert service.sync_all_json_to_db(IDENTITY)["errors"] == 1
    monkeypatch.undo()
    assert service.file_status(IDENTITY) == {"processed": 0, "pending": 1}
    result = service.sync_all_json_to_db(IDENTITY)
    assert (result["synced"], result["errors"]) == (1, 0)


def test_file_status_counts_an_unreadable_file_as_pending(tmp_path: Path) -> None:
    """不是 UTF-8 的檔讀不出來：設定頁統計算成待處理，不讓整個統計失敗（乙-1）。"""
    _write(tmp_path, _payload(_HELLO, _HI))
    (tmp_path / "broken.json").write_bytes(bytes([0xFF, 0xFE, 0x00, 0x7B]))
    service = _service(tmp_path)
    service.sync_all_json_to_db(IDENTITY)
    assert service.file_status(IDENTITY) == {"processed": 1, "pending": 1}


def test_archive_payload_counts_versions_as_archived(tmp_path: Path) -> None:
    """剪裁與匯出端點收到的是確認在庫的筆數：改過的那則另存成版本，也算在內。"""
    service = _service(tmp_path)
    service.archive_payload(IDENTITY, [_payload(_HELLO, _HI)], "a.json")
    count = service.archive_payload(
        IDENTITY, [_payload({**_HELLO, "content": "changed"}, _HI)], "b.json"
    )
    assert count == 2
    assert _versions(tmp_path) == {"m1": ["changed"]}


def test_archive_payload_writes_without_any_file(tmp_path: Path) -> None:
    """剪裁與匯出端點的入口：直接寫庫、回寫入訊息數，不在 legacy 目錄產生 JSON。"""
    count = _service(tmp_path).archive_payload(IDENTITY, [_payload(_HELLO, _HI)], "label.json")
    assert count == 2
    assert _messages(tmp_path) == {"m1": "hello", "m2": "hi"}
    assert not list(tmp_path.glob("*.json"))
    assert _rows(tmp_path / "legacy_history.db", "SELECT filename FROM sessions") == [
        ("label.json",)
    ]


# ── Proxy 端點（沙盒真實 ResourceManager）與保護不放寬 ─────────────────────────


@pytest.fixture
def owner_client() -> Any:
    agent = AgentIdentity.for_role(
        agent_id=f"T4Arch{uuid.uuid4().hex[:6]}", role="Base", is_human=False, capabilities=[]
    )
    app.dependency_overrides[get_verified_identity] = lambda: agent
    with TestClient(app) as client:
        yield client, agent
    app.dependency_overrides.clear()


def test_export_endpoint_archives_into_the_database(owner_client: Any) -> None:
    client, agent = owner_client
    body = {"agent_id": agent.agent_id, "filename": f"{agent.agent_id}_2026/10/15.json",
            "json_content": [{"id": "ui-1", "title": "t", "chat": {"history": {"messages": {
                "n1": {"id": "n1", "role": "user", "content": "hi", "timestamp": 1_760_486_400}
            }}}}]}
    response = client.post("/v1/ui/sessions/legacy/export", json=body)
    assert response.status_code == 200
    assert response.json() == {"status": "success", "archived": 1}  # 不再回實體路徑
    legacy_dir = Path(resource_manager.get_vault_path("legacy", agent))
    assert not [p for p in legacy_dir.rglob("*") if p.suffix == ".json"]
    assert _messages(legacy_dir) == {"n1": "hi"}


def test_export_with_nothing_to_archive_is_not_success(owner_client: Any) -> None:
    """沒有可歸檔的訊息不能回成功（前端收到成功會把那段剪掉）。"""
    client, agent = owner_client
    body = {"agent_id": agent.agent_id, "filename": "x.json", "json_content": [{"id": "s"}]}
    response = client.post("/v1/ui/sessions/legacy/export", json=body)
    assert response.status_code >= 400
    legacy_dir = Path(resource_manager.get_vault_path("legacy", agent))
    assert not (legacy_dir / "legacy_history.db").exists()


def test_settings_sync_and_stats_endpoints(owner_client: Any) -> None:
    """設定頁：放進來的檔顯示待處理；手動同步成功（不再因搬檔被擋）；之後算已處理。"""
    client, agent = owner_client
    legacy_dir = Path(resource_manager.get_vault_path("legacy", agent))
    legacy_dir.mkdir(parents=True, exist_ok=True)
    _write(legacy_dir, _payload(_HELLO, _HI))

    def counts() -> tuple[int, int]:
        body = client.get(f"/v1/stats/legacy/{agent.agent_id}").json()
        return body["archived_files"], body["pending_files"]

    assert counts() == (0, 1)
    response = client.post(f"/v1/stats/legacy/{agent.agent_id}/sync")
    assert response.status_code == 200
    assert response.json()["result"]["synced"] == 1
    assert counts() == (1, 0)
    assert (legacy_dir / "chat-a.json").exists()


def test_protection_is_not_relaxed(owner_client: Any) -> None:
    _, agent = owner_client
    legacy_file = Path(resource_manager.get_vault_path("legacy", agent)) / "x.json"
    assert resource_manager.is_protected_path(legacy_file)
    with pytest.raises(PermissionError):
        resource_manager.write_json(legacy_file, {})


def test_no_tool_can_reach_the_archive_entry_points() -> None:
    """新的寫入口只給滑動視窗、匯出端點、手動同步；兩個 tools 目錄都不得引用。"""
    tool_dirs = (_REPO / "ToukaProxy/src/tools", _REPO / "ToukaMemory/src/memory_server/tools")
    offenders = [
        str(path.relative_to(_REPO))
        for directory in tool_dirs
        for path in directory.rglob("*.py")
        if any(name in path.read_text(encoding="utf-8")
               for name in ("archive_payload", "sync_all_json_to_db", ".archive("))
    ]
    assert offenders == []
