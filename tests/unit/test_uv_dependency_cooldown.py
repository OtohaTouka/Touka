"""依賴冷卻期（ADR-0299、禁令 69）：uv、npm、Dependabot 的數字要一致。

冷卻期寫在五個地方，工具各讀各的，這裡釘住它們不會分岔。
緊急放行的規定見 ``docs/rules/deploy.md`` 禁令 69：
Python 的例外寫在 ``pyproject.toml``，要同時列進下面的清單，PR 上看得到；
npm 只在更新 lock 的那條命令帶例外，不寫進 ``.npmrc``。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COOLDOWN_DAYS = 3
# 緊急放行的 Python 套件 -> 放行到的時間（寫法見禁令 69）。這個時間加冷卻期就該拿掉，
# 每日檢查（scripts/dep_audit_report.py）會提醒；這裡不依日期變紅，否則 CI 會在某一天突然紅。
ALLOWED_UV_EXCEPTIONS: dict[str, str] = {}


def _npmrc(project: str) -> dict[str, str]:
    lines = (ROOT / project / ".npmrc").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def test_pyproject_sets_the_cooldown() -> None:
    uv = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]
    assert uv.get("exclude-newer") == f"{COOLDOWN_DAYS} days"
    assert uv.get("exclude-newer-package", {}) == ALLOWED_UV_EXCEPTIONS


def test_lockfile_records_the_cooldown_span() -> None:
    options = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8")).get("options", {})
    assert options.get("exclude-newer-span") == f"P{COOLDOWN_DAYS}D"
    assert set(options.get("exclude-newer-package", {})) == set(ALLOWED_UV_EXCEPTIONS)


@pytest.mark.parametrize("project", ["ToukaChat", "ToukaIDE"])
def test_npm_projects_set_the_cooldown(project: str) -> None:
    npmrc = _npmrc(project)
    assert npmrc.get("min-release-age") == str(COOLDOWN_DAYS)
    assert "min-release-age-exclude" not in npmrc


def test_dependabot_sets_the_cooldown() -> None:
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    assert config["updates"]
    for update in config["updates"]:
        assert update["cooldown"]["default-days"] == COOLDOWN_DAYS
