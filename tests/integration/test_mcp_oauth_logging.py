"""P8-A shipped ingress and application log secret-canary tests."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.protocols.mcp.oauth.protocol import OAuthCallbackPolicy
from src.protocols.mcp.oauth.service import OAuthStateInvalidError
from src.routers.mcp_oauth import get_mcp_oauth_runtime, router

_ROOT = Path(__file__).resolve().parents[2]


class _RejectingRuntime:
    @staticmethod
    def get_callback_policy(redirect_route: str):
        return OAuthCallbackPolicy(
            route=redirect_route,
            redirect_uri=f"https://proxy.example{redirect_route}",
            response_mode="form_post",
        )

    async def complete_callback(self, **_kwargs):
        raise OAuthStateInvalidError()


def test_all_shipped_uvicorn_entrypoints_disable_access_log():
    dockerfile = (_ROOT / "ToukaProxy" / "Dockerfile").read_text(encoding="utf-8")
    powershell = (_ROOT / "ToukaProxy" / "run_proxy.ps1").read_text(encoding="utf-8")
    main = (_ROOT / "ToukaProxy" / "src" / "main.py").read_text(encoding="utf-8")

    assert '"--no-access-log"' in dockerfile
    assert "--no-access-log" in powershell
    assert "access_log=False" in main

    for relative_path in ("docker-compose.yml", "ToukaProxy/docker-compose.yml"):
        compose = yaml.safe_load((_ROOT / relative_path).read_text(encoding="utf-8"))
        command = compose["services"]["touka-proxy"].get("command", [])
        assert "--no-access-log" in command, relative_path


def test_nginx_exact_callback_locations_disable_access_log():
    nginx = (_ROOT / "nginx" / "nginx.conf.example").read_text(encoding="utf-8")

    assert "location = /mcp/oauth/callback" in nginx
    assert "location = /v1/mcp/oauth/callback" in nginx
    assert nginx.count("access_log off;") >= 2


def test_callback_failure_does_not_emit_state_or_code_to_application_logs(caplog):
    state = "state-log-canary-7e91"
    code = "code-log-canary-3a44"
    runtime = _RejectingRuntime()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_mcp_oauth_runtime] = lambda: runtime

    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        response = client.post(
            "/mcp/oauth/callback",
            content=f"state={state}&code={code}",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 400
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert state not in rendered
    assert code not in rendered
