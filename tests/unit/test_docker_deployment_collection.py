"""Docker deployment collection and explicit execution boundary regression tests."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType


DEPLOYMENT_TEST = Path(__file__).resolve().parents[1] / "integration/test_docker_deployment.py"
OPT_IN_VARIABLE = "TOUKA_RUN_DOCKER_DEPLOYMENT_TESTS"


def _load_deployment() -> ModuleType:
    spec = importlib.util.spec_from_file_location("docker_deployment_under_test", DEPLOYMENT_TEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def docker_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


@pytest.mark.parametrize("configured", [None, "1"])
def test_import_never_probes_docker(
    monkeypatch: pytest.MonkeyPatch,
    docker_calls: list[list[str]],
    configured: str | None,
) -> None:
    if configured is None:
        monkeypatch.delenv(OPT_IN_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(OPT_IN_VARIABLE, configured)
    _load_deployment()
    assert docker_calls == []


@pytest.mark.parametrize("configured", [None, "0", "true"])
def test_unselected_execution_skips_without_docker(
    docker_calls: list[list[str]], configured: str | None
) -> None:
    deployment = _load_deployment()
    with pytest.raises(pytest.skip.Exception, match="NOT RUN"):
        deployment.require_docker_deployment.__wrapped__((OPT_IN_VARIABLE, configured))
    assert docker_calls == []


def test_selected_execution_checks_cli_then_daemon(docker_calls: list[list[str]]) -> None:
    deployment = _load_deployment()
    deployment.require_docker_deployment.__wrapped__((OPT_IN_VARIABLE, "1"))
    assert docker_calls == [["docker", "compose", "version"], ["docker", "info"]]


@pytest.mark.parametrize("failure_command", [["docker", "compose", "version"], ["docker", "info"]])
def test_selected_unavailable_docker_fails_visibly(
    monkeypatch: pytest.MonkeyPatch,
    docker_calls: list[list[str]],
    failure_command: list[str],
) -> None:
    deployment = _load_deployment()
    docker_calls.clear()

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        docker_calls.append(command)
        if command == failure_command:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(pytest.fail.Exception, match="Docker CLI or Docker Daemon"):
        deployment.require_docker_deployment.__wrapped__((OPT_IN_VARIABLE, "1"))
    assert docker_calls[-1] == failure_command


@pytest.mark.parametrize("health", ["healthy", "unhealthy", "starting"])
def test_selected_health_assertion_rejects_unhealthy_container(
    monkeypatch: pytest.MonkeyPatch,
    docker_calls: list[list[str]],
    tmp_path: Path,
    health: str,
) -> None:
    deployment = _load_deployment()
    docker_calls.clear()
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  example:\n    healthcheck: {}\n", encoding="utf-8")
    monkeypatch.setattr(deployment, "DOCKER_COMPOSE_PATH", compose)
    monkeypatch.setattr(deployment, "WORKSPACE_ROOT", tmp_path)

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        docker_calls.append(command)
        output = (
            json.dumps([{"Service": "example", "State": "running", "Health": health}])
            if command == ["docker", "compose", "ps", "--format", "json"]
            else "synthetic diagnostic"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    if health == "healthy":
        deployment.test_docker_containers_health()
        assert not any("logs" in command for command in docker_calls)
    else:
        with pytest.raises(pytest.fail.Exception, match=f"health status is '{health}'"):
            deployment.test_docker_containers_health()
        assert docker_calls[-1] == ["docker", "compose", "logs", "--tail", "50", "example"]
