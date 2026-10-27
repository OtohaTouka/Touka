"""舊審查報告乙-3 第 2 輪：行程裡寫 .env 的地方共用一把鎖（ADR-0288）.

切換嵌入模型在記憶工作者上寫 .env，設定頁、首裝精靈與密鑰持久化多在主迴圈上寫；不互斥時，
後寫的可能用清空到一半的檔蓋掉前一個，密鑰會不見。這裡測試自己拿著鎖，每個寫入的地方都要
等鎖放開才寫（沒用鎖的會直接寫完而變紅）。全部寫在 tmp 的 .env（``TOUKA_ENV_FILE``），
不碰真的設定檔；讀寫都用 dotenv。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, wait
from typing import TYPE_CHECKING

import pytest
from dotenv import dotenv_values, set_key
from src import config
from src.models.identity import AgentIdentity
from src.routers import config as config_router
from src.services import embedding_service
from src.services.setup_service import SetupService

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

HOLD_S = 0.2  # 測試拿著鎖多久；用了鎖的寫入這段時間都在等
JOIN_TIMEOUT_S = 5.0
KEY, VALUE = "TOUKA_ENV_LOCK_TEST", "written"
CALLER = AgentIdentity(agent_id="LockAdmin")  # 角色由路由的 Depends 把關，這裡直接呼叫


def _settings_page(_env: Path) -> None:
    update = config_router.EnvUpdate(settings={KEY: VALUE})
    asyncio.run(config_router.update_env(update, CALLER))


def _one_secret(_env: Path) -> None:
    assert config.persist_generated_secret(KEY, VALUE)


def _secret_set(_env: Path) -> None:
    assert config.persist_generated_secrets({KEY: VALUE})


def _setup_wizard(env: Path) -> None:
    SetupService(env_file_path=str(env)).apply(embedding_model=VALUE)


def _embedding_switch(_env: Path) -> None:
    embedding_service.apply_switch(VALUE)


# 寫入的地方 → 它寫的 key
WRITERS: dict[str, tuple[Callable[[Path], None], str]] = {
    "settings_page": (_settings_page, KEY),
    "one_secret": (_one_secret, KEY),
    "secret_set": (_secret_set, KEY),
    "setup_wizard": (_setup_wizard, "EMBEDDING_MODEL"),
    "embedding_switch": (_embedding_switch, "EMBEDDING_MODEL"),
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".env"
    set_key(str(path), "KEPT", "secret", quote_mode="never")
    monkeypatch.setenv("TOUKA_ENV_FILE", str(path))
    monkeypatch.setattr(config, "is_bootstrap_enabled", lambda: True)  # 密鑰持久化才會寫
    # 不改這個行程的環境變數、不重置記憶單例
    monkeypatch.setattr(config, "apply_runtime_env", lambda _updates: None)
    monkeypatch.setattr(embedding_service, "apply_runtime_env", lambda _updates: None)
    monkeypatch.setattr(embedding_service, "_reset_memory_singletons", lambda: None)
    return path


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_each_env_writer_waits_for_the_shared_lock(name: str, env: Path) -> None:
    write, key = WRITERS[name]
    with ThreadPoolExecutor(max_workers=1) as pool:
        with config.ENV_FILE_LOCK:
            writing = pool.submit(write, env)
            done, _ = wait([writing], timeout=HOLD_S)
            assert not done, "wrote .env without taking the shared lock"
            assert dotenv_values(env) == {"KEPT": "secret"}
        writing.result(timeout=JOIN_TIMEOUT_S)
    assert dotenv_values(env) == {"KEPT": "secret", key: VALUE}
