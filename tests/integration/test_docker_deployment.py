import contextlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

# 定位專案根目錄下的 docker-compose.yml
WORKSPACE_ROOT = Path(__file__).parents[2].resolve()
DOCKER_COMPOSE_PATH = WORKSPACE_ROOT / "docker-compose.yml"


def is_docker_available() -> bool:
    """檢查本地 Docker CLI 及 Daemon 是否可用（SSOT 環境偵測）。"""
    try:
        # 1. 檢查 Docker CLI (指定 encoding="utf-8" 防止 Windows 預設 CP950 解碼崩潰)
        subprocess.run(
            ["docker", "compose", "version"],  # noqa: S607
            capture_output=True,
            encoding="utf-8",
            errors="ignore",
            check=True,
        )
        # 2. 檢查 Docker Daemon 連線
        subprocess.run(
            ["docker", "info"],  # noqa: S607
            capture_output=True,
            encoding="utf-8",
            errors="ignore",
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    else:
        return True


@pytest.fixture(autouse=True)
def require_docker_deployment(touka_docker_deployment_gate: tuple[str, str | None]) -> None:
    """Only probe the real deployment after explicit opt-in, never during collection."""
    variable_name, configured = touka_docker_deployment_gate
    if configured != "1":
        pytest.skip(f"NOT RUN: live Docker deployment; set {variable_name}=1 to opt in")
    if not is_docker_available():
        pytest.fail("Docker CLI or Docker Daemon is not available/accessible in this environment")


def test_docker_containers_health():
    """整合冒煙測試：動態解析 docker-compose.yml 並確認所有容器運行且健康。"""
    assert DOCKER_COMPOSE_PATH.exists(), f"docker-compose.yml not found at {DOCKER_COMPOSE_PATH}"

    # 1. 解析 docker-compose.yml 取得預期的 Services (SSOT 來源)
    with DOCKER_COMPOSE_PATH.open("r", encoding="utf-8") as f:
        compose_data = yaml.safe_load(f)

    expected_services = compose_data.get("services", {})
    assert expected_services, "No services defined in docker-compose.yml"

    # 2. 呼叫 docker compose ps 取得當前運行中的容器狀態
    res = subprocess.run(
        ["docker", "compose", "ps", "--format", "json"],  # noqa: S607
        capture_output=True,
        encoding="utf-8",
        errors="ignore",
        cwd=str(WORKSPACE_ROOT),
        check=True,
    )
    output = res.stdout.strip()

    # 3. 相容不同版本的 docker compose ps JSON 輸出格式 (NDJSON vs JSON Array)
    containers = []
    if output:
        try:
            # 嘗試解析為單一 JSON 陣列
            parsed = json.loads(output)
            containers = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            # 嘗試解析為每一行為一個 JSON 的格式 (NDJSON)
            for line in output.splitlines():
                if line.strip():
                    with contextlib.suppress(json.JSONDecodeError):
                        containers.append(json.loads(line))

    # 將容器資訊以 Service 名稱建立對照表
    container_by_service = {}
    for c in containers:
        srv_name = c.get("Service") or c.get("service")
        if srv_name:
            container_by_service[srv_name] = c

    failures = []

    # 4. 驗證每一個定義的服務狀態
    for service_name, service_cfg in expected_services.items():
        if service_name == "touka-init":
            continue
        if service_name not in container_by_service:
            failures.append(f"Service '{service_name}' has no active container found.")
            continue

        container = container_by_service[service_name]
        state = (container.get("State") or container.get("state", "")).lower()
        health = (container.get("Health") or container.get("health", "")).lower()

        # 每個被定義的服務都必須處於 running 狀態
        if "running" not in state:
            # 取得錯誤容器日誌以便 debug
            log_res = subprocess.run(  # noqa: S603
                ["docker", "compose", "logs", "--tail", "50", service_name],  # noqa: S607
                capture_output=True,
                encoding="utf-8",
                errors="ignore",
                cwd=str(WORKSPACE_ROOT),
            )
            failures.append(
                f"Service '{service_name}' state is '{state}' (expected 'running').\n"
                f"--- Container Logs for {service_name} ---\n{log_res.stdout}\n"
            )
            continue

        # 若 compose 中該服務有定義 healthcheck，其健康狀態必須為 healthy
        has_healthcheck = "healthcheck" in service_cfg
        if has_healthcheck and health != "healthy":
            # 取得不健康容器日誌以便 debug
            log_res = subprocess.run(  # noqa: S603
                ["docker", "compose", "logs", "--tail", "50", service_name],  # noqa: S607
                capture_output=True,
                encoding="utf-8",
                errors="ignore",
                cwd=str(WORKSPACE_ROOT),
            )
            failures.append(
                f"Service '{service_name}' health status is '{health}' (expected 'healthy').\n"
                f"--- Container Logs for {service_name} ---\n{log_res.stdout}\n"
            )

    # 5. 若有任何容器不符合預期，則 Assert 失敗，並印出搜集到的故障原因與日誌
    if failures:
        pytest.fail("\n".join(failures))
