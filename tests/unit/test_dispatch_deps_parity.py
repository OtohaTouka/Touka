"""ADR-0082/0117：dispatch 依賴與 proxy 執行環境一致性守衛（防漂移）.

dispatch 子程序在 proxy 的 Python 環境上執行（單一映像）。proxy 以精確四個產品
package 佈建（含 dispatch/memory 的鎖定依賴）。兩條守衛釘住該結構：
  1. proxy Dockerfile 的 uv sync 必須精確包含四個產品 package。
  2. dispatch 成員 pyproject 的每個直接依賴必須釘於單一 uv.lock（Guard #16 的
     per-member 沙盒匯入測試另行證明可匯入）。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DOCKERFILE = PROJECT_ROOT / "ToukaProxy" / "Dockerfile"
DISPATCH_PYPROJECT = PROJECT_ROOT / "ToukaDispatch" / "pyproject.toml"
UV_LOCK = PROJECT_ROOT / "uv.lock"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _dispatch_direct_deps() -> set[str]:
    with DISPATCH_PYPROJECT.open("rb") as f:
        doc = tomllib.load(f)
    names: set[str] = set()
    for req in doc["project"]["dependencies"]:
        m = re.match(r"^([A-Za-z0-9._-]+)", req.split(";")[0].strip())
        if m:
            names.add(_norm(m.group(1)))
    return names


def test_proxy_image_installs_exact_product_workspace_packages():
    """Proxy must install exactly the four product workspace packages."""
    content = PROXY_DOCKERFILE.read_text(encoding="utf-8")
    packages = re.findall(r"--package (\S+)", content)
    assert sorted(packages) == ["toukadispatch", "toukaheartbeat", "toukamemory", "toukaproxy"]
    assert "--all-packages" not in content


def test_dispatch_deps_pinned_in_lock():
    """dispatch 每個直接依賴必須釘版於單一 uv.lock（runtime 保證）。"""
    with UV_LOCK.open("rb") as f:
        lock = tomllib.load(f)
    locked_names = {
        _norm(pkg["name"])
        for pkg in lock.get("package", [])
        if "virtual" not in (pkg.get("source") or {})
    }
    missing = sorted(_dispatch_direct_deps() - locked_names)
    assert not missing, f"dispatch 直接依賴未釘版於 uv.lock：{missing}（跑 `uv lock`）"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
