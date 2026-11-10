"""小幫手的配對資料（ADR-0311）：只存權杖的雜湊和配對的人，檔案讀不到就算沒配對."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from filelock import AsyncFileLock  # noqa: FILELOCK_ASYNC_SSOT
from pydantic import ValidationError

from src.models.local_bridge import LocalPairingRecord
from src.services.resource_manager import resource_manager
from src.utils.local_bridge_token import hash_token, new_token, token_matches

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_SECONDS = 30


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

    async def create(self, human_id: str) -> str:
        """產生新權杖（舊的立刻失效），只回傳這一次。"""
        token = new_token()
        async with self._lock():
            self._write(
                LocalPairingRecord(
                    generation=self.generation() + 1,
                    token_hash=hash_token(token),
                    human_id=human_id,
                )
            )
        return token

    async def revoke(self) -> int:
        """解除配對，回傳新的世代號。"""
        async with self._lock():
            generation = self.generation() + 1
            self._write(LocalPairingRecord(generation=generation, token_hash=None, human_id=None))
        return generation


@lru_cache(maxsize=1)
def get_local_pairing_store() -> LocalPairingStore:
    """全行程共用的配對資料。"""
    return LocalPairingStore()
