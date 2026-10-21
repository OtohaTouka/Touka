"""Legacy JSON to SQLite sync: public directory API, honest write failures, read-only views.

`legacy/` is protected from file writes, so a synced file stays in place and is recorded as
processed (file name + content hash) instead of being moved to backup/.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from memory_server.services import legacy_store as legacy_store_module
from memory_server.services.legacy_store import LegacyStore
from memory_server.services.legacy_sync import LegacySyncService
from memory_server.services.manager import MemoryManager
from src.models.identity import AgentIdentity

IDENTITY = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")


def _manager_for(directory: Path) -> Any:
    manager = create_autospec(MemoryManager, instance=True)
    manager.get_legacy_dir.return_value = directory
    return manager


def _write_session(directory: Path, name: str = "chat-a.json") -> Path:
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "id": "session-a",
                "title": "session a",
                "messages": [
                    {"id": "m1", "role": "user", "content": "hello"},
                    {"id": "m2", "role": "assistant", "content": "hi"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _locked(*_args: Any, **_kwargs: Any) -> None:
    raise legacy_store_module.sqlite3.OperationalError


def test_legacy_sync_uses_public_dir_api(tmp_path: Path) -> None:
    manager = _manager_for(tmp_path)

    store = LegacySyncService(manager).get_store(IDENTITY)

    assert Path(store.db_path).parent == tmp_path
    assert manager.get_legacy_dir.call_args.args[0] is IDENTITY


def test_legacy_sync_success_keeps_json_and_records_it(tmp_path: Path) -> None:
    source = _write_session(tmp_path)

    service = LegacySyncService(_manager_for(tmp_path))
    result = service.sync_all_json_to_db(IDENTITY)

    assert (result["synced"], result["errors"]) == (1, 0)
    assert source.exists()
    assert not (tmp_path / "backup").exists()
    assert result["total_in_db"] == {"session_count": 1, "message_count": 1}
    assert service.file_status(IDENTITY) == {"processed": 1, "pending": 0}


def test_legacy_sync_write_failure_keeps_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_session(tmp_path)
    monkeypatch.setattr(LegacyStore, "archive", _locked)

    service = LegacySyncService(_manager_for(tmp_path))
    result = service.sync_all_json_to_db(IDENTITY)

    assert (result["synced"], result["errors"]) == (0, 1)
    assert source.exists()
    assert service.file_status(IDENTITY) == {"processed": 0, "pending": 1}


def test_legacy_store_write_failure_is_reported(tmp_path: Path) -> None:
    unusable = tmp_path / "legacy_history.db"
    unusable.mkdir()
    store = LegacyStore(str(unusable))

    with pytest.raises(legacy_store_module.sqlite3.Error):
        store.upsert_session(
            session_id="s",
            identity=IDENTITY,
            filename="s.json",
            title="s",
            created_at=1,
            updated_at=1,
        )
    with pytest.raises(legacy_store_module.sqlite3.Error):
        store.upsert_messages([{"id": "m", "session_id": "s", "role": "user", "content": "x"}])


def test_legacy_store_read_does_not_create_db(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_history.db"
    store = LegacyStore(str(db_path))

    assert store.get_stats() == {"session_count": 0, "message_count": 0}
    assert store.get_random_messages(limit=3) == []
    assert not db_path.exists()


def test_legacy_sync_without_directory_creates_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "never-created"

    result = LegacySyncService(_manager_for(missing)).sync_all_json_to_db(IDENTITY)

    assert (result["synced"], result["errors"]) == (0, 0)
    assert not missing.exists()
