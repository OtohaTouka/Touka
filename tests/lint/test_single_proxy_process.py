"""Proxy 只有一個 server 行程（ADR-0207；ADR-0275 §1）。

同意帳本、每個 skill 的鎖與 MCP 隔離設定檔的 ``threading.Lock`` 都以「單一 proxy 行程」為前提；
過去只有 compose 裡的註解。這裡解析 Dockerfile 的 CMD、四份 compose 裡 proxy 服務的
``command``／``environment``／``deploy``，以及 ``src`` 裡的 ``uvicorn.run(``：

- 沒有 ``--workers``、沒有 ``WEB_CONCURRENCY``；
- 沒有 ``deploy.replicas`` 或 ``scale`` 大於 1；
- ``uvicorn.run(`` 沒帶 ``workers=``。

要改成多行程，先把上面三者換成跨行程的鎖，再改這個測試。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "ToukaProxy" / "Dockerfile"
COMPOSE_FILES = (
    ROOT / "docker-compose.yml",
    ROOT / "ToukaProxy" / "docker-compose.yml",
    ROOT / "docker-compose.ghcr.yml",
    ROOT / "docker-compose.cloud.yml",
)
PROXY_SERVICE = "touka-proxy"
SRC = ROOT / "ToukaProxy" / "src"


def _dockerfile_cmd() -> list[str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.startswith("CMD")]
    assert len(lines) == 1, lines
    return json.loads(lines[0].removeprefix("CMD").strip())


def test_dockerfile_cmd_runs_one_uvicorn_process() -> None:
    cmd = _dockerfile_cmd()
    assert cmd[0] == "uvicorn"
    assert not any(arg.startswith("--workers") for arg in cmd)


def _proxy_service(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    service = (document.get("services") or {}).get(PROXY_SERVICE)
    assert isinstance(service, dict), f"{path.name} 沒有 {PROXY_SERVICE}"
    return service


def _environment_names(service: dict[str, Any]) -> set[str]:
    env = service.get("environment") or {}
    if isinstance(env, dict):
        return set(env)
    return {str(item).split("=", 1)[0] for item in env}


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_compose_runs_a_single_proxy_process(path: Path) -> None:
    service = _proxy_service(path)
    command = service.get("command") or []
    words = command.split() if isinstance(command, str) else [str(c) for c in command]

    assert not any(word.startswith("--workers") for word in words)
    assert "WEB_CONCURRENCY" not in _environment_names(service)
    replicas = (service.get("deploy") or {}).get("replicas", 1)
    assert int(replicas) <= 1
    assert int(service.get("scale", 1)) <= 1


def test_uvicorn_run_never_passes_workers() -> None:
    calls = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "uvicorn.run":
                calls.append(path)
                assert all(kw.arg != "workers" for kw in node.keywords), path
    assert calls, "找不到 uvicorn.run(；測試範圍可能失效"
