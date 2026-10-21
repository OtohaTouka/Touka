"""沙盒環境變數還原的 TDD 測試（防「測試寫進版控目錄」回歸）。

``setup_test_environment`` 會注入 ``TOUKA_CONFIG_DIR`` / ``MEMORY_APP_DATA_DIR``；
其 teardown 的單例重置（``skill_service.reset_for_test`` 等）會**就地重解析並快取**
config 路徑。若 fixture 被以自備沙盒直呼（test_dependency_override_*）而 teardown
不還原 env，快取住的就是那個外來路徑——下一個測試的 setup 只修 env、修不到已快取的
單例，於是技能檔會寫到外來（曾是 repo 相對）目錄，弄髒版控。

本檔釘死：teardown 必須把 env 還原到 session 沙盒，**且早於**單例重置。
env 一律透過 ``src.config`` 的 SSOT 窗口讀取（`get_config_dir` / `get_full_env`），
不直接碰 ``os.environ``。
"""
from __future__ import annotations

import contextlib
from pathlib import Path

from src.config import get_config_dir, get_full_env
from src.services.skill_service import skill_service

from tests.conftest import setup_test_environment


def test_teardown_restores_sandbox_env_and_singletons(tmp_path) -> None:
    """以外來沙盒跑完一輪 fixture 後，env 與單例快取都必須回到 session 沙盒。"""
    session_config_dir = get_config_dir()
    session_data_dir = get_full_env()["MEMORY_APP_DATA_DIR"]

    foreign_sandbox = tmp_path / "foreign_sandbox"
    foreign_sandbox.mkdir()

    fixture_gen = setup_test_environment.__wrapped__(str(foreign_sandbox))
    next(fixture_gen)

    # setup 階段確實切到外來沙盒（否則本測試沒在測東西）
    assert get_config_dir() == foreign_sandbox / "config"

    with contextlib.suppress(StopIteration):
        next(fixture_gen)

    # teardown 後 env 必須完全還原
    assert get_config_dir() == session_config_dir
    assert get_full_env()["MEMORY_APP_DATA_DIR"] == session_data_dir

    # 關鍵：單例快取的技能根目錄不得停在外來沙盒（config_path 為公開契約屬性）
    assert Path(skill_service.config_path) == session_config_dir / "skills"
    assert foreign_sandbox not in Path(skill_service.config_path).parents
