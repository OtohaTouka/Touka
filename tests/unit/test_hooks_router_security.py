"""Hook 管理路由（ADR-0292）：只給人類管理員，存檔比對版本、先過路徑界線、查 CSRF Origin.

設定檔與 hook 服務都換成測試資料夾裡的替身；不執行任何 hook。只用合成 ID。
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import hooks as hooks_router
from src.services import settings_snapshot_service
from src.services.hooks.paths import HookZones
from src.services.hooks.runners import RunResult
from src.services.hooks.service import HOOKS_FILE_NAME, HookService
from src.services.identity_router import get_verified_identity

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from httpx import Response

ADMIN = AgentIdentity.for_role(agent_id="zz-hook-admin", role="admin", is_human=True)
ASSUMED_ADMIN = AgentIdentity.for_role(agent_id="zz-hook-assumed", role="admin")  # X-Agent-ID 代打
USER = AgentIdentity.for_role(agent_id="zz-hook-user", role="normal", is_human=True)
SOUL = AgentIdentity.for_role(agent_id="zz-hook-soul", role="Owner")
TRUSTED_ORIGIN = "http://localhost:3001"
ENDPOINTS = (("get", "/v1/hooks"), ("put", "/v1/hooks"), ("get", "/v1/hooks/runs"))


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "hooks").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "evil.py").write_text("", encoding="utf-8")
    return directory


@pytest.fixture
def service(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> HookService:
    async def zones() -> HookZones:
        return HookZones(config_dir=config_dir, forbidden=(config_dir.parent / "data",))

    async def http(_hook: object, _payload: bytes) -> RunResult:
        return RunResult(status="ok", duration_ms=3, http_status=200, stdout="")

    svc = HookService(
        config_path=lambda: config_dir / HOOKS_FILE_NAME, zones_provider=zones, http_runner=http
    )
    monkeypatch.setattr(hooks_router, "hook_service", svc)
    return svc


@pytest.fixture
def as_identity() -> Iterator[list[AgentIdentity | None]]:
    holder: list[AgentIdentity | None] = [ADMIN]
    app.dependency_overrides[get_verified_identity] = lambda: holder[0]
    try:
        yield holder
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _config(*hooks: dict, enabled: bool = True) -> dict:
    groups = [{"matcher": "bash", "hooks": list(hooks)}]
    return {"enabled": enabled, "hooks": {"PreToolUse": groups}}


def _http_hook(name: str = "audit") -> dict:
    return {"type": "http", "url": "https://hooks.example.test/audit", "name": name}


def _put(body: dict, origin: str | None = TRUSTED_ORIGIN) -> Response:
    headers = {"Origin": origin} if origin else {}
    return _client().put("/v1/hooks", json=body, headers=headers)


def _code(response: Response) -> str:
    return response.json()["detail"]["code"]


def test_r01_admin_reads_the_empty_config_and_the_wired_events(
    service: HookService, as_identity: list[AgentIdentity | None]
) -> None:
    response = _client().get("/v1/hooks")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["config"]["enabled"] is False
    assert body["config"]["hooks"] == {}
    assert body["health"] == {"status": "missing", "revision": None, "detail": None}
    assert body["wired_events"] == ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
    assert body["matcher_events"] == ["PreToolUse", "PostToolUse"]


@pytest.mark.parametrize("identity", [ASSUMED_ADMIN, USER, SOUL, None],
                         ids=["assumed-admin", "user", "soul", "anonymous"])
@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_r02_everyone_but_a_human_admin_is_refused(
    service: HookService, config_dir: Path, as_identity: list[AgentIdentity | None],
    identity: AgentIdentity | None, method: str, path: str,
) -> None:
    as_identity[0] = identity
    if identity is None:  # 沒帶憑證：走真的身分驗證
        app.dependency_overrides.pop(get_verified_identity, None)
    response = _client().request(
        method, path, json={"config": _config(_http_hook()), "revision": None},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert response.status_code in (401, 403), response.text
    assert not (config_dir / HOOKS_FILE_NAME).exists()


def test_r03_save_writes_the_file_and_returns_its_revision(
    service: HookService, config_dir: Path, as_identity: list[AgentIdentity | None]
) -> None:
    response = _put({"config": _config(_http_hook()), "revision": None})
    assert response.status_code == 200, response.text
    raw = (config_dir / HOOKS_FILE_NAME).read_bytes()
    revision = response.json()["revision"]
    assert revision == hashlib.sha256(raw).hexdigest()

    saved = json.loads(raw)
    assert saved["author"] == ADMIN.agent_id
    assert isinstance(saved["updated_at"], float)
    assert service.has_hooks("PreToolUse")

    after = _client().get("/v1/hooks").json()
    assert after["health"]["status"] == "ok"
    assert after["health"]["revision"] == revision
    assert after["config"]["hooks"]["PreToolUse"][0]["hooks"][0]["name"] == "audit"


def test_r04_client_cannot_choose_the_author(
    service: HookService, config_dir: Path, as_identity: list[AgentIdentity | None]
) -> None:
    body = {"config": {**_config(_http_hook()), "author": "someone-else", "updated_at": 1.0},
            "revision": None}
    assert _put(body).status_code == 200
    saved = json.loads((config_dir / HOOKS_FILE_NAME).read_bytes())
    assert saved["author"] == ADMIN.agent_id
    assert saved["updated_at"] != 1.0


def test_r05_stale_revision_is_refused_and_keeps_the_file(
    service: HookService, config_dir: Path, as_identity: list[AgentIdentity | None]
) -> None:
    first = _put({"config": _config(_http_hook("first")), "revision": None})
    assert first.status_code == 200
    before = (config_dir / HOOKS_FILE_NAME).read_bytes()

    for stale in (None, "0" * 64):
        response = _put({"config": _config(_http_hook("second")), "revision": stale})
        assert response.status_code == 409, response.text
        assert _code(response) == "HOOK_CONFIG_CONFLICT"
        assert response.json()["detail"]["revision"] == first.json()["revision"]
    assert (config_dir / HOOKS_FILE_NAME).read_bytes() == before

    newer = _put({"config": _config(_http_hook("second")), "revision": first.json()["revision"]})
    assert newer.status_code == 200, newer.text


def test_r06_command_in_an_agent_writable_place_is_refused_before_writing(
    service: HookService, config_dir: Path, as_identity: list[AgentIdentity | None]
) -> None:
    evil = str(config_dir.parent / "data" / "evil.py")
    hook = {"type": "command", "command": sys.executable, "args": [evil], "enabled": False}
    response = _put({"config": _config(hook), "revision": None})
    assert response.status_code == 422, response.text
    assert _code(response) == "HOOK_PATH_REJECTED"
    assert response.json()["detail"]["problems"][0]["event"] == "PreToolUse"
    assert not (config_dir / HOOKS_FILE_NAME).exists()


@pytest.mark.parametrize("origin", ["https://evil.example.test", "null"])
def test_r07_untrusted_origin_is_refused(
    service: HookService, config_dir: Path, as_identity: list[AgentIdentity | None], origin: str
) -> None:
    response = _put({"config": _config(_http_hook()), "revision": None}, origin=origin)
    assert response.status_code == 403, response.text
    assert not (config_dir / HOOKS_FILE_NAME).exists()


@pytest.mark.parametrize(
    "body",
    [
        {"config": _config(_http_hook())},  # 沒帶 revision
        {"config": _config(_http_hook()), "revision": None, "force": True},
        {"config": {"enabled": True, "hooks": {"SessionStart": []}}, "revision": None},
        {"config": _config({"type": "command", "command": "/bin/sh", "shell": True}),
         "revision": None},
        {"config": _config({**_http_hook(), "headers": {"Authorization": "Bearer ${TOKEN}"}}),
         "revision": None},
    ],
    ids=["no-revision", "extra-field", "unwired-event", "shell-field", "env-in-header"],
)
def test_r08_malformed_requests_are_refused(
    service: HookService, config_dir: Path, as_identity: list[AgentIdentity | None], body: dict
) -> None:
    response = _put(body)
    assert response.status_code == 422, response.text
    assert not (config_dir / HOOKS_FILE_NAME).exists()


async def test_r09_runs_are_listed_newest_first_and_incognito_keeps_only_the_result(
    service: HookService, config_dir: Path, as_identity: list[AgentIdentity | None]
) -> None:
    assert _put({"config": _config(_http_hook("audit")), "revision": None}).status_code == 200
    await service.dispatch("PreToolUse", {"tool_name": "bash"}, match=["bash"])
    await service.dispatch("PreToolUse", {"tool_name": "bash"}, match=["bash"], incognito=True)

    response = _client().get("/v1/hooks/runs")
    assert response.status_code == 200, response.text
    runs = response.json()["runs"]
    assert [run["incognito"] for run in runs] == [True, False]
    assert runs[0]["hook"] == "" and runs[0]["detail"] is None and runs[0]["http_status"] is None
    assert runs[1]["hook"] == "audit" and runs[1]["http_status"] == 200
    assert runs[1]["event"] == "PreToolUse" and runs[1]["outcome"] == "none"


def test_r10_broken_file_shows_the_last_good_config_and_its_own_revision(
    service: HookService, config_dir: Path, as_identity: list[AgentIdentity | None]
) -> None:
    assert _put({"config": _config(_http_hook("good")), "revision": None}).status_code == 200
    (config_dir / HOOKS_FILE_NAME).write_text("{broken", encoding="utf-8")

    body = _client().get("/v1/hooks").json()
    assert body["health"]["status"] == "error"
    assert body["config"]["hooks"]["PreToolUse"][0]["hooks"][0]["name"] == "good"
    broken_revision = hashlib.sha256(b"{broken").hexdigest()
    assert body["health"]["revision"] == broken_revision

    fixed = _put({"config": _config(_http_hook("fixed")), "revision": broken_revision})
    assert fixed.status_code == 200, fixed.text


def test_r11_routes_are_mounted_with_and_without_v1(
    service: HookService, as_identity: list[AgentIdentity | None]
) -> None:
    assert _client().get("/hooks").status_code == 200
    assert _client().get("/hooks/runs").status_code == 200


def test_r12_settings_snapshot_never_carries_the_hook_config() -> None:
    """匯入會繞過存檔的路徑檢查，所以 hooks.json 不在設定匯出／匯入裡。"""
    filenames = {spec.filename for spec in settings_snapshot_service.SECTIONS}
    assert HOOKS_FILE_NAME not in filenames
