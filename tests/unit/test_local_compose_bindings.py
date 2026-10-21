"""Parse shipped Compose bytes with real Docker CLI and exclusively owned inputs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PORTS = {"PROXY_PORT": 8000, "DISPATCH_PORT": 8083, "HEARTBEAT_PORT": 8082, "CHAT_PORT": 3001}


def _environment(inherited: dict[str, str], owned: Path) -> dict[str, str]:
    result = {
        key: inherited[key]
        for key in (
            "SystemRoot",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "PATH",
            "ProgramFiles",
            "ProgramFiles(x86)",
            "ProgramData",
        )
        if key in inherited
    }
    result.update(DOCKER_CONFIG=str(owned / "docker-config"), TEMP=str(owned), TMP=str(owned))
    return result


def _parse_compose(owned: Path, source: bytes, overrides: dict[str, int]) -> dict:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose unavailable; real source parsing NOT_RUN")
    owned.mkdir()
    docker_config = owned / "docker-config"
    docker_config.mkdir()
    (docker_config / "config.json").write_text("{}", encoding="utf-8")
    compose = owned / "compose.yml"
    compose.write_bytes(source)
    env_file = owned / ".env"
    env_file.write_text(
        "HEARTBEAT_TOKEN=synthetic-compose-only\n"
        + "".join(f"{key}={value}\n" for key, value in overrides.items()),
        encoding="utf-8",
    )
    command = [
        docker,
        "--config",
        str(docker_config),
        "compose",
        "--project-directory",
        str(owned),
        "--env-file",
        str(env_file),
        "-f",
        str(compose),
        "config",
        "--format",
        "json",
    ]
    env = _environment(os.environ, owned)  # noqa: ENV_SSOT_VIOLATION
    result = subprocess.run(  # noqa: S603
        command,
        cwd=owned,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    (owned / "command.json").write_text(
        json.dumps({"argv": command, "timeout_seconds": 30, "exit_code": result.returncode}),
        encoding="utf-8",
    )
    (owned / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (owned / "stderr.log").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    # Parsing resolves strings only; no container, mount, build or image operation occurs.
    for service in parsed["services"].values():
        for mount in service.get("volumes", []):
            if mount["type"] == "bind":
                assert Path(mount["source"]).is_relative_to(owned)
    return parsed


def _assert_local_bindings(parsed: dict, overrides: dict[str, int]) -> None:
    expected = {
        ("touka-proxy", 8000): overrides.get("PROXY_PORT", 8000),
        ("touka-proxy", 8083): overrides.get("DISPATCH_PORT", 8083),
        ("touka-heartbeat", 8082): overrides.get("HEARTBEAT_PORT", 8082),
        ("touka-chat", 3001): overrides.get("CHAT_PORT", 3001),
    }
    actual = {}
    assert sum(len(service.get("ports", [])) for service in parsed["services"].values()) == 4
    for name, service in parsed["services"].items():
        for port in service.get("ports", []):
            assert port.get("host_ip") == "127.0.0.1", "published host must be loopback"
            assert port.get("protocol", "tcp") == "tcp"
            actual[name, int(port["target"])] = int(port["published"])
    assert actual == expected
    proxy = parsed["services"]["touka-proxy"]
    assert proxy["environment"]["DISPATCH_BIND_HOST"] == "0.0.0.0"  # noqa: S104
    assert (
        parsed["services"]["touka-heartbeat"]["environment"]["API_BASE_URL"]
        == "http://touka-proxy:8000"
    )


@pytest.mark.parametrize("profile", ["docker-compose.yml", "docker-compose.ghcr.yml"])
@pytest.mark.parametrize("override", [False, True])
def test_local_compose_published_hosts_are_loopback(tmp_path, profile, override):
    """Both actual profiles preserve four targets and all port overrides."""
    values = {name: value + 10000 for name, value in PORTS.items()} if override else {}
    parsed = _parse_compose(tmp_path / "owned-compose", (ROOT / profile).read_bytes(), values)
    _assert_local_bindings(parsed, values)
    assert (
        parsed["services"]["touka-heartbeat"]["environment"]["HEARTBEAT_TOKEN"]
        == "synthetic-compose-only"  # noqa: S105
    )


@pytest.mark.parametrize("profile", ["docker-compose.yml", "docker-compose.ghcr.yml"])
def test_real_parser_and_binding_check_reject_wildcard_regression(tmp_path, profile):
    """A source fixture reverted to wildcard must fail the same final assertion."""
    changed = yaml.safe_load((ROOT / profile).read_bytes())
    changed["services"]["touka-proxy"]["ports"][0] = "0.0.0.0:${PROXY_PORT:-8000}:8000"
    parsed = _parse_compose(tmp_path / "owned-negative", yaml.safe_dump(changed).encode(), {})
    with pytest.raises(AssertionError, match="published host must be loopback"):
        _assert_local_bindings(parsed, {})


def test_compose_child_cannot_inherit_product_or_docker_control_settings(tmp_path):
    """Only fixed OS keys survive, and Docker config/temp are always owned."""
    inherited = {
        "SystemRoot": "synthetic-os",
        "PATH": "synthetic-path",
        "DOCKER_CONFIG": "private-config",
        "COMPOSE_FILE": "private-compose",
        "COMPOSE_ENV_FILES": "private-env",
        "SANCTUARY_DATA_PATH": "private-data",
        "HEARTBEAT_TOKEN": "private-token",
        "USERPROFILE": "private-home",
    }
    result = _environment(inherited, tmp_path)
    assert set(result) == {"SystemRoot", "PATH", "DOCKER_CONFIG", "TEMP", "TMP"}
    assert result["DOCKER_CONFIG"] == str(tmp_path / "docker-config")
    assert not any("private" in value for value in result.values())
