"""測試換 data 根的方式（ADR-0293 T3）：設定、環境變數與 sandbox state 根一起換。

ResourceManager 的區內方法經它註冊的根來源讀這幾個值，所以區的根跟著換。conftest 的
``zone_data_root`` 用它；Linux 隔離測試的版面要用自己的 data 根時也用它。只在測試的
``tmp_path`` 裡用，不碰 ``data/``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src import config as runtime_config

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def point_zone_roots(monkeypatch: pytest.MonkeyPatch, data: Path, state: Path) -> None:
    """把 data 根換到 ``data``、sandbox state 根換到 ``state``（測試結束時還原）。"""
    monkeypatch.setattr(runtime_config.settings, "MEMORY_APP_DATA_DIR", str(data))
    monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(data))
    monkeypatch.setattr(runtime_config.settings, "TOUKA_SANDBOX_STATE_DIR", str(state))
