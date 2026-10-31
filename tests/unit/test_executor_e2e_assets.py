"""iso-c2 C2-2：執行器端到端資產（tests/docker/isolation.compose.yml 與兩支檢查程式）不會悄悄失效.

- ``iso-exec`` 的設定和正式的 ``touka-executor`` 相同：只有映像、資料來源、volume 與網路名稱、
  ``restart`` 不同（測的才是正式的形狀）；
- 兩個網路都是 internal（不連網），``iso-exec`` 和 Touka 內部服務的替身 ``iso-internal``
  不共用網路；
- ``iso-client`` 是 proxy 的形狀，和執行器共用同一個 ipc volume、同一份資料；
- 映像名稱與資料夾可以從外面給（CI 用 ``ghcr.io/ci/touka-proxy:ci``、
  ``$RUNNER_TEMP/compose/data``）；
- 檢查程式一律經 ``sandboxed.py`` 先開 smoke 沙盒才載入專案程式（ADR-0214、禁令 63）。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path, PurePosixPath

import pytest
import yaml

from tests.unit.test_executor_compose_security import (
    DOCKERFILE,
    EXECUTOR,
    Mount,
    _compose,
    _mounts,
    _proxy_data,
)

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "tests" / "docker"
E2E = "tests/docker/isolation.compose.yml"
PRODUCTION = "docker-compose.yml"
EXEC, CLIENT, INTERNAL, LANDLOCK = "iso-exec", "iso-client", "iso-internal", "iso-landlock"
IMAGE = "${TOUKA_ISO_IMAGE:-touka-iso-test-proxy:c2}"
DATA = "${TOUKA_ISO_DATA:?set TOUKA_ISO_DATA to the fake data folder}"
LAUNCHER = "/iso/tests/docker/sandboxed.py"
CHECKS = {CLIENT: "tests.docker.executor_check", LANDLOCK: "tests.docker.landlock_proxy_check"}
# 和正式設定逐鍵相同的部分（安全邊界本身）
MIRRORED = (
    "command", "user", "init", "read_only", "tmpfs", "cap_drop", "security_opt",
    "mem_limit", "pids_limit", "cpus", "environment", "sysctls", "healthcheck", "pull_policy",
)
# 正式設定有、測試設定刻意沒有的鍵：container_name（專案名稱 touka-iso-*）、depends_on（沒有 init）
NOT_MIRRORED = frozenset({"container_name", "depends_on"})


def _e2e() -> dict:
    return _compose(E2E)


def _service(name: str) -> dict:
    return _e2e()["services"][name]


def _production() -> dict:
    return _compose(PRODUCTION)["services"][EXECUTOR]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names}
    names |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    return {name.split(".")[0] for name in names}


def test_e2e_has_exactly_the_four_services():
    assert set(_e2e()["services"]) == {EXEC, CLIENT, INTERNAL, LANDLOCK}


def test_iso_exec_mirrors_the_production_executor():
    production, iso = _production(), _service(EXEC)
    assert set(iso) == (set(production) - NOT_MIRRORED)
    assert {key: iso[key] for key in MIRRORED} == {key: production[key] for key in MIRRORED}
    shape = {(m.kind, m.target, m.read_only) for m in _mounts(production)}
    assert {(m.kind, m.target, m.read_only) for m in _mounts(iso)} == shape


def test_iso_exec_data_comes_from_the_fake_data_folder():
    """資料根底下的掛載照相對路徑（``mcp_servers/managed``），其他照最後一段名稱."""
    data = _proxy_data(PRODUCTION)
    for mount in _mounts(_service(EXEC)):
        if mount.kind == "bind":
            target = PurePosixPath(mount.target)
            rel = target.relative_to(data) if target.is_relative_to(data) else target.name
            assert mount.source == f"{DATA}/{rel}"


def test_every_service_uses_the_given_image_and_never_restarts():
    for name, service in _e2e()["services"].items():
        assert service["image"] == IMAGE, name
        assert service["pull_policy"] == "never", name
        assert service["restart"] == "no", name
        assert "container_name" not in service, name


def test_networks_are_internal_and_the_executor_shares_none_with_touka():
    compose = _e2e()
    assert all(net == {"internal": True} for net in compose["networks"].values())
    exec_nets = set(compose["services"][EXEC]["networks"])
    assert exec_nets == {"exec"}
    assert set(compose["services"][INTERNAL]["networks"]) == {"touka"}
    assert set(compose["services"][CLIENT]["networks"]) == {"touka"}
    assert compose["services"][LANDLOCK]["network_mode"] == "none"


def test_client_is_proxy_shaped_and_shares_the_executor_ipc_and_data():
    client, iso = _service(CLIENT), _service(EXEC)
    ipc = iso["command"][iso["command"].index("--ipc-dir") + 1]
    assert client["user"] == "1001:1001"
    assert Mount("volume", "ipc", ipc, False) in _mounts(client)
    assert Mount("bind", DATA, str(_proxy_data(PRODUCTION)), False) in _mounts(client)
    assert client["depends_on"][EXEC] == {"condition": "service_healthy"}
    args = client["command"]
    assert args[args.index("--ipc") + 1] == ipc
    assert "env_file" not in client


@pytest.mark.parametrize("name", [CLIENT, LANDLOCK])
def test_checks_run_through_the_sandbox_launcher_from_a_read_only_repo(name):
    service = _service(name)
    assert service["command"][:4] == ["python", "-B", LAUNCHER, CHECKS[name]]
    assert Mount("bind", "${TOUKA_ISO_REPO:-../..}", "/iso", True) in _mounts(service)
    image_path = re.search(r"(?m)^ENV PYTHONPATH=(\S+)$", DOCKERFILE)[1]  # 映像的 src，不是 repo 的
    assert f"PYTHONPATH=/iso:{image_path}" in service["environment"]


def test_landlock_check_is_a_profile_with_fast_open_off():
    service = _service(LANDLOCK)
    assert service["profiles"] == ["landlock"]
    assert service["sysctls"] == {"net.ipv4.tcp_fastopen": 0}


def test_landlock_check_takes_its_sandbox_state_from_the_settings():
    """ST 只有一個來源（RM，ADR-0293 P7）：檢查程式不自組 ST 與 data 根.

    ST 由 compose 的設定指到這個服務自己的 tmpfs。
    """
    service = _service(LANDLOCK)
    states = [
        entry.split("=", 1)[1]
        for entry in service["environment"]
        if entry.startswith("TOUKA_SANDBOX_STATE_DIR=")
    ]
    assert len(states) == 1
    tmpfs = [PurePosixPath(entry.split(":", 1)[0]) for entry in service["tmpfs"]]
    assert any(PurePosixPath(states[0]).is_relative_to(mount) for mount in tmpfs)
    text = (DOCKER / "landlock_proxy_check.py").read_text(encoding="utf-8")
    assert "state_root=" not in text
    assert "data_root=" not in text


def test_launcher_loads_no_project_code_before_the_sandbox():
    imports = _imports(DOCKER / "sandboxed.py")
    assert imports <= set(sys.stdlib_module_names) | {"__future__", "tests"}
    text = (DOCKER / "sandboxed.py").read_text(encoding="utf-8")
    assert text.index("enable_smoke_sandbox(") < text.index("runpy.run_module(")


@pytest.mark.parametrize("module", sorted(CHECKS.values()))
def test_check_modules_refuse_to_run_outside_the_sandbox(module):
    text = (ROOT / Path(*module.split("."))).with_suffix(".py").read_text(encoding="utf-8")
    assert "network_guard.is_installed()" in text


def test_compose_documents_the_ci_parameters():
    text = (ROOT / E2E).read_text(encoding="utf-8")
    for needle in ("TOUKA_ISO_IMAGE", "ghcr.io/ci/touka-proxy:ci", "TOUKA_ISO_DATA", "touka-iso-"):
        assert needle in text
    assert yaml.safe_load(text)["volumes"] == {"ipc": None, "exec-state": None}
