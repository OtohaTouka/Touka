"""小幫手的配對資料（ADR-0312、ADR-0317）.

存權杖的雜湊、配對的人和蓋章用的鑰匙；檔案讀不到就算沒配對。鑰匙不出現在任何 API 回應。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from filelock import AsyncFileLock  # noqa: FILELOCK_ASYNC_SSOT
from pydantic import ValidationError

from src.models.local_bridge import LocalPairingRecord
from src.services.resource_manager import resource_manager
from src.utils.local_bridge_signing import (
    decision_key_from_hex,
    new_decision_key,
    pairing_code,
)
from src.utils.local_bridge_token import hash_token, new_token, token_matches

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class NewPairing:
    """剛產生的配對：權杖與鑰匙只在這裡出現一次，``code`` 給人貼到小幫手。"""

    # repr 不印出來：例外訊息或 log 帶到這個物件時不能洩漏
    token: str = field(repr=False)
    key: bytes = field(repr=False)

    @property
    def code(self) -> str:
        """配對碼：權杖.鑰匙。"""
        return pairing_code(self.token, self.key)


class LocalPairingStore:
    """一台 Touka 同時只有一組配對。每次換新或解除，世代號都往上加。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def _file(self) -> Path:
        if self._path is not None:
            return self._path
        return Path(resource_manager.get_base_path()) / "local_bridge" / "pairing.json"

    def _read(self) -> LocalPairingRecord | None:
        path = self._file()
        try:
            if not resource_manager.exists(path):
                return None
            return LocalPairingRecord.model_validate_json(resource_manager.read_file_text(path))
        except (OSError, ValueError, ValidationError):
            logger.warning("Local helper pairing file is unusable; treating it as not paired.")
            return None

    def generation(self) -> int:
        """目前的世代號，沒有檔案或檔案壞掉時是 0。"""
        record = self._read()
        return record.generation if record is not None else 0

    def current(self) -> LocalPairingRecord | None:
        """目前有效的配對。"""
        record = self._read()
        return record if record is not None and record.token_hash and record.human_id else None

    def decision_key(self) -> bytes | None:
        """蓋章用的鑰匙；沒配對、舊配對或格式不對都是 None，這時不送決定。"""
        record = self.current()
        return decision_key_from_hex(record.decision_key) if record is not None else None

    def authenticate(self, token: str) -> LocalPairingRecord | None:
        """權杖對得上就回配對資料。"""
        record = self.current()
        if record is None or not token_matches(token, record.token_hash or ""):
            return None
        return record

    def _lock(self) -> AsyncFileLock:
        path = self._file()
        resource_manager.make_dir(path.parent)
        return AsyncFileLock(f"{path}.lock", timeout=_LOCK_TIMEOUT_SECONDS)

    def _write(self, record: LocalPairingRecord) -> None:
        resource_manager.write_json(self._file(), record.model_dump())

    async def create(self, human_id: str) -> NewPairing:
        """產生新權杖與鑰匙（舊的立刻失效），只回傳這一次。"""
        created = NewPairing(token=new_token(), key=new_decision_key())
        async with self._lock():
            self._write(
                LocalPairingRecord(
                    generation=self.generation() + 1,
                    token_hash=hash_token(created.token),
                    human_id=human_id,
                    decision_key=created.key.hex(),
                )
            )
        return created

    async def revoke(self) -> None:
        """解除配對。"""
        async with self._lock():
            self._write(
                LocalPairingRecord(generation=self.generation() + 1, token_hash=None, human_id=None)
            )


@lru_cache(maxsize=1)
def get_local_pairing_store() -> LocalPairingStore:
    """全行程共用的配對資料。"""
    return LocalPairingStore()
