"""Proxy 內的排程程式整組刪除：引擎（ADR-0250 B4）之後，增刪改查與設定也刪。

Proxy 不做主動排程（AGENTS §1：排程歸 ToukaHeartbeat）。ADR-0250 B4 先刪了沒有呼叫點的
排程引擎；剩下的增刪改查（``routers/scheduler.py``、``services/scheduler_service.py``）沒有
掛載、走不到，``ENABLE_SCHEDULER`` 也沒有人讀，一併刪除。舊 ``.env`` 還留著這個鍵時設定照常
載入（``Settings`` 是 ``extra="ignore"``）。只讀 repo 原始碼與 tmp_path 的 .env，不碰實體
data/ 或真 .env。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from src import config as config_module
from src.config import Settings

ROOT = Path(__file__).resolve().parents[2]
_PROXY_SRC = ROOT / "ToukaProxy" / "src"
_REMOVED_FILES = (
    _PROXY_SRC / "routers" / "scheduler.py",
    _PROXY_SRC / "services" / "scheduler_service.py",
)
_FLAG = "ENABLE_SCHEDULER"


@pytest.mark.parametrize("path", _REMOVED_FILES, ids=lambda path: path.name)
def test_unmounted_scheduler_crud_files_are_gone(path: Path) -> None:
    assert not path.exists(), path.relative_to(ROOT).as_posix()


def test_enable_scheduler_is_not_a_setting() -> None:
    assert _FLAG not in Settings.model_fields
    assert not hasattr(config_module, _FLAG)


def test_proxy_env_example_no_longer_documents_the_flag() -> None:
    text = (ROOT / "ToukaProxy" / ".env.example").read_text(encoding="utf-8")
    assert not re.search(rf"^\s*#?\s*{_FLAG}\s*=", text, re.MULTILINE)


@pytest.mark.parametrize("value", ["true", "False", "not-a-bool"])
def test_old_env_file_with_the_flag_still_loads(tmp_path, monkeypatch, value) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    monkeypatch.delenv("PORT", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"{_FLAG}={value}\nPORT=8123\n", encoding="utf-8")

    loaded = Settings(_env_file=str(env_file))

    assert loaded.PORT == 8123
    assert not hasattr(loaded, _FLAG)
