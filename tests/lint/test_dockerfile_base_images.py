"""Python 服務維持已釘選的 Debian／glibc／Python 3.12 與非 root runtime。"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# 有 Dockerfile 的 Python 服務（ToukaMemory 以掛載內嵌、ToukaDispatch 隨 proxy 單一映像
# 出貨（ADR-0082）——皆無獨立 Dockerfile；ToukaChat 為 Node）。
PYTHON_BASE_REF = (
    "python:3.12-slim-trixie@sha256:"
    "78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
)
PYTHON_BASE_BY_SERVICE = {
    "ToukaProxy": PYTHON_BASE_REF,
    "ToukaHeartbeat": PYTHON_BASE_REF,
}


@pytest.mark.parametrize("service, expected_base", PYTHON_BASE_BY_SERVICE.items())
def test_uses_selected_glibc_base_not_alpine(service: str, expected_base: str) -> None:
    """Python 服務必須用核准的 Debian ref，不得用 alpine。"""
    dockerfile = ROOT / service / "Dockerfile"
    assert dockerfile.exists(), f"{service}/Dockerfile 必須存在"
    content = dockerfile.read_text(encoding="utf-8")
    assert expected_base in re.findall(r"(?im)^FROM\s+(\S+)", content), (
        f"{service}/Dockerfile 應使用核准的 Debian Python ref"
    )
    assert "alpine" not in content, (
        f"{service}/Dockerfile 不得用 alpine（musl 無 manylinux wheel；"
        "Python 3.12 manylinux 相容条件保持）"
    )


@pytest.mark.parametrize("service", PYTHON_BASE_BY_SERVICE)
def test_runs_as_non_root_user(service: str) -> None:
    """Python 服務以 Debian useradd 建立 UID/GID 1001 的非 root appuser。"""
    dockerfile = ROOT / service / "Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")
    assert "USER appuser" in content, f"{service}/Dockerfile 必須切換至非 root appuser"
    assert "useradd" in content, f"{service}/Dockerfile 應使用已驗證的 useradd 建立 appuser"
    assert re.search(r"\bgroupadd\s+(?:-g|--gid)\s+1001\s+appuser\b", content)
    assert "-u 1001" in content and "-g 1001" in content


@pytest.mark.parametrize("service", PYTHON_BASE_BY_SERVICE)
def test_debian_python_minor_is_explicit_without_apk(service: str) -> None:
    content = (ROOT / service / "Dockerfile").read_text(encoding="utf-8")
    python_images = re.findall(r"(?im)^FROM\s+(python:\S+)", content)
    assert python_images, "Guard30 必須能從 tagged Python FROM 讀到實際 minor"
    assert all(
        re.fullmatch(r"python:3\.12-slim-trixie@sha256:[0-9a-f]{64}", image)
        for image in python_images
    )
    assert not re.search(r"\bapk\s+(?:add|upgrade|del)\b", content)


def test_proxy_installs_bash_for_the_real_terminal_probe() -> None:
    content = (ROOT / "ToukaProxy" / "Dockerfile").read_text(encoding="utf-8")
    commands = content.replace("\\\n", " ")
    assert re.search(r"\bapt-get\s+install\b[^\n;&]*\bbash\b", commands)
