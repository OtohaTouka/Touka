"""An unmigrated legacy facts.jsonl becomes a code-only memory notice (file names only)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from memory_server import factual as factual_module
from src import main as main_module
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import observability
from src.services.identity_router import get_verified_identity

if TYPE_CHECKING:
    from pathlib import Path


def _plant(vault: Path, agent: str, name: str = "facts.jsonl") -> Path:
    path = vault / agent / "fact" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"content": "old"}\n', encoding="utf-8")
    return path


@pytest.fixture
def isolated_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(factual_module, "_get_app_data_dir", lambda: str(tmp_path))
    return tmp_path / "vault"


def test_unmigrated_jsonl_shows_notice(
    isolated_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    planted = [_plant(isolated_vault, "Alice"), _plant(isolated_vault, "Bob")]
    _plant(isolated_vault, "Carol", "facts.jsonl.bak")
    before = [path.read_bytes() for path in planted]

    with caplog.at_level(logging.WARNING):
        notices = observability.collect_memory_notices()

    assert notices == [{"code": "unmigrated_legacy_facts", "level": "info", "count": 2}]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert sum("facts.jsonl" in record.getMessage() for record in warnings) == 1
    assert [path.read_bytes() for path in planted] == before


def test_no_unmigrated_jsonl_means_no_notice(isolated_vault: Path) -> None:
    _plant(isolated_vault, "Carol", "facts.jsonl.bak")

    assert observability.collect_memory_notices() == []


def test_memory_endpoint_exposes_notices_from_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    notices: list[dict[str, Any]] = [
        {"code": "unmigrated_legacy_facts", "level": "info", "count": 3}
    ]
    monkeypatch.setattr(main_module, "collect_memory_notices", lambda: notices)
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        agent_id="DebugAgent", role="Debugger"
    )
    try:
        with TestClient(app) as client:
            body = client.get("/v1/memory").json()
    finally:
        app.dependency_overrides.clear()

    assert body["memory_notices"] == notices
    assert {"system_degraded", "degraded_reason", "memory_tools_count"} <= body.keys()
