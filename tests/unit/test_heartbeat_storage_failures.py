"""Heartbeat storage faults must preserve owned configuration and surface failure."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from ToukaHeartbeat import storage_service
from ToukaHeartbeat.storage_service import StorageService


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b'{"heartbeat_token":"synthetic-retained",', "config_parse_failed"),
        (b'{"schedules":[],"extension":NaN}', "config_parse_failed"),
        (b'{"schedules":[],"extension":Infinity}', "config_parse_failed"),
        (b'{"schedules":[],"extension":-Infinity}', "config_parse_failed"),
        (b"[]", "config_invalid"),
        (b'{"schedules":null}', "config_invalid"),
        (b'{"schedules":"invalid"}', "config_invalid"),
        (b'{"schedules":[{"id":"missing-agent"}]}', "config_invalid"),
        (
            b'{"schedules":[{"id":"one","agent_id":"DebugAgent","interval_minutes":0}]}',
            "config_invalid",
        ),
        (
            b'{"schedules":[{"id":"one","agent_id":"DebugAgent","mode":"daily","daily_times":["30:70"]}]}',
            "config_invalid",
        ),
    ],
)
def test_existing_invalid_config_preserves_original_and_never_generates_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes, reason: str
) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(content)
    generator = Mock(return_value="must-not-be-used")
    monkeypatch.setattr(storage_service.secrets, "token_hex", generator)
    with pytest.raises(Exception) as error:
        StorageService(path).load_config()
    assert getattr(error.value, "reason", None) == reason
    assert "synthetic-retained" not in str(error.value)
    assert path.read_bytes() == content
    generator.assert_not_called()


@pytest.mark.parametrize("failure", [PermissionError, OSError])
def test_existing_unreadable_config_is_not_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[OSError]
) -> None:
    path = tmp_path / "config.json"
    original = b'{"heartbeat_token":"synthetic-retained","schedules":[]}'
    path.write_bytes(original)
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes

    def denied_text(candidate: Path, *args, **kwargs):
        if candidate == path:
            raise failure("synthetic-sensitive-diagnostic")
        return real_read_text(candidate, *args, **kwargs)

    def denied_bytes(candidate: Path):
        if candidate == path:
            raise failure("synthetic-sensitive-diagnostic")
        return real_read_bytes(candidate)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "read_text", denied_text)
        scoped.setattr(Path, "read_bytes", denied_bytes)
        with pytest.raises(Exception) as error:
            StorageService(path).load_config()
    assert getattr(error.value, "reason", None) == "config_read_failed"
    assert "synthetic-sensitive-diagnostic" not in str(error.value)
    assert path.read_bytes() == original


def test_save_encoding_failure_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    original = b'{"heartbeat_token":"synthetic-retained","schedules":[]}'
    path.write_bytes(original)
    monkeypatch.setattr(
        storage_service.json, "dumps", Mock(side_effect=OSError("synthetic-encode"))
    )
    with pytest.raises(Exception) as error:
        StorageService(path).save_config({"schedules": []})
    assert getattr(error.value, "reason", None) == "config_write_failed"
    assert getattr(error.value, "committed", None) is False
    assert path.read_bytes() == original


def test_roundtrip_keeps_unknown_fields_and_omitted_or_null_presence(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = {
        "heartbeat_token": "synthetic-retained",
        "unknown_extension": {"enabled": None},
        "schedules": [
            {"id": "one", "agent_id": "DebugAgent", "init_prompt": None, "custom": [1, 2]}
        ],
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    service = StorageService(path)
    assert service.load_config() == original
    service.save_config(service.load_config())
    assert json.loads(path.read_bytes()) == original


def test_only_missing_file_initializes_and_token_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    service = StorageService(path)
    first = service.load_config()
    assert first["schedules"] == []
    assert len(first["heartbeat_token"]) >= 16
    assert service.get_token() == first["heartbeat_token"]
    assert service.load_config() == first


@pytest.mark.parametrize(
    "phase", ["temp_create", "write", "flush", "file_fsync", "temp_close", "replace"]
)
def test_precommit_atomic_failure_keeps_bytes_and_only_cleans_owned_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    path = tmp_path / "config.json"
    original = b'{"heartbeat_token":"synthetic-retained","schedules":[]}'
    path.write_bytes(original)
    unrelated = tmp_path / "unrelated.tmp"
    unrelated.write_bytes(b"keep")
    real_temporary = tempfile.NamedTemporaryFile
    real_fsync = os.fsync
    real_replace = os.replace
    temporary_fds: set[int] = set()

    class FailingTemporary:
        def __init__(self, *args, **kwargs):
            if phase == "temp_create":
                raise OSError("owned-injected-temp-create")
            self.file = real_temporary(*args, **kwargs)
            self.name = self.file.name
            temporary_fds.add(self.file.fileno())

        def __enter__(self):
            return self

        def __exit__(self, *args):
            result = self.file.__exit__(*args)
            if phase == "temp_close":
                raise OSError("owned-injected-temp-close")
            return result

        def write(self, value):
            if phase == "write":
                raise OSError("owned-injected-write")
            return self.file.write(value)

        def flush(self):
            if phase == "flush":
                raise OSError("owned-injected-flush")
            return self.file.flush()

        def fileno(self):
            return self.file.fileno()

    def fsync(fd: int):
        if phase == "file_fsync" and fd in temporary_fds:
            raise OSError("owned-injected-file-fsync")
        return real_fsync(fd)

    def replace(source, destination):
        if phase == "replace" and Path(destination) == path:
            raise OSError("owned-injected-before-replace")
        return real_replace(source, destination)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", FailingTemporary)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(Exception) as error:
        StorageService(path).save_config({"heartbeat_token": "synthetic-new", "schedules": []})
    assert getattr(error.value, "reason", None) == "config_write_failed"
    assert getattr(error.value, "phase", None) == phase
    assert getattr(error.value, "committed", None) is False
    assert path.read_bytes() == original
    assert unrelated.read_bytes() == b"keep"
    assert {item.name for item in tmp_path.iterdir()} == {"config.json", "unrelated.tmp"}


def test_post_replace_directory_sync_failure_reports_visible_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(b'{"schedules":[]}')
    service = StorageService(path)
    monkeypatch.setattr(
        service, "_sync_directory", Mock(side_effect=OSError("owned-sync")), raising=False
    )
    with pytest.raises(Exception) as error:
        service.save_config({"schedules": [], "extension": "new"})
    assert getattr(error.value, "phase", None) == "directory_fsync"
    assert getattr(error.value, "committed", None) is True
    assert getattr(error.value, "durability", None) == "NOT_CONFIRMED"
    assert json.loads(path.read_bytes())["extension"] == "new"


@pytest.mark.parametrize("time_value", ["09:00", "9:00", "09:0", " 9:00 "])
def test_preexisting_daily_time_forms_roundtrip_without_normalization(
    tmp_path: Path, time_value: str
) -> None:
    path = tmp_path / "config.json"
    original = {
        "schedules": [
            {"id": "one", "agent_id": "DebugAgent", "mode": "daily", "daily_times": [time_value]}
        ]
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    service = StorageService(path)
    assert service.load_config() == original
    service.save_config(service.load_config())
    assert json.loads(path.read_bytes()) == original


def test_equivalent_daily_times_are_rejected_without_changing_original(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = json.dumps(
        {
            "schedules": [
                {
                    "id": "one",
                    "agent_id": "DebugAgent",
                    "mode": "daily",
                    "daily_times": ["9:00", "09:00"],
                }
            ]
        }
    ).encode()
    path.write_bytes(original)
    with pytest.raises(Exception) as error:
        StorageService(path).load_config()
    assert getattr(error.value, "reason", None) == "config_invalid"
    assert path.read_bytes() == original


def test_duplicate_schedule_ids_are_rejected_without_changing_original(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    schedule = {"id": "same", "agent_id": "DebugAgent"}
    original = json.dumps({"schedules": [schedule, schedule]}).encode()
    path.write_bytes(original)
    with pytest.raises(Exception) as error:
        StorageService(path).load_config()
    assert getattr(error.value, "reason", None) == "config_invalid"
    assert path.read_bytes() == original


def test_existing_entry_with_missing_target_is_not_initialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    original = b"owned existing-entry sentinel"
    path.write_bytes(original)
    real_read = Path.read_bytes
    generator = Mock(return_value="must-not-be-used")

    def missing_target(candidate: Path) -> bytes:
        if candidate == path:
            raise FileNotFoundError("owned_dangling_target_simulation")
        return real_read(candidate)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "read_bytes", missing_target)
        scoped.setattr(storage_service.secrets, "token_hex", generator)
        with pytest.raises(Exception) as error:
            StorageService(path).load_config()
    assert getattr(error.value, "reason", None) == "config_read_failed"
    generator.assert_not_called()
    assert path.read_bytes() == original


@pytest.mark.parametrize("result", ["visible_commit", "unreadable", "ambiguous"])
def test_replace_exception_classifies_commit_by_actual_readback(
    tmp_path, monkeypatch, result
) -> None:
    path = tmp_path / "config.json"
    original = b'{"schedules":[]}'
    path.write_bytes(original)
    real_replace = os.replace
    real_read = Path.read_bytes
    replaced = False

    def replace(source, target):
        nonlocal replaced
        real_replace(source, target)
        replaced = True
        if result == "ambiguous":
            path.write_bytes(b'{"schedules":[],"other":"unknown"}')
        raise OSError("owned-after-replace")

    def read(candidate):
        if candidate == path and replaced and result == "unreadable":
            raise PermissionError("owned-readback-unavailable")
        return real_read(candidate)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "replace", replace)
        scoped.setattr(Path, "read_bytes", read)
        with pytest.raises(Exception) as failure:
            StorageService(path).save_config({"schedules": [], "extension": "new"})
    assert getattr(failure.value, "reason", None) == "config_write_failed"
    assert getattr(failure.value, "phase", None) == "replace"
    assert getattr(failure.value, "committed", False) is (
        True if result == "visible_commit" else None
    )
    assert path.read_bytes() != original
