"""ToukaDispatch 應用憑證生命週期（SRP；ADR-0201）。"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from dataclasses import dataclass
from typing import Any

from memory_server.config_utils import get_current_epoch

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS: int = 3600
FIXED_DISPATCH_AGENT_ID: str = "DispatchAgent"

_INTERNAL_METHODS = frozenset(
    {
        "dispatch_initialize_mission",
        "dispatch_update_task",
        "dispatch_get_board",
    }
)
_EXTERNAL_METHODS = frozenset(
    {
        "dispatch_update_task",
        "dispatch_get_board",
    }
)


@dataclass(frozen=True)
class SessionInfo:
    """不含 bearer 明文的短命 session 紀錄。"""

    token_hash: str
    agent_id: str
    created_at: float
    expires_at: float
    audience: str
    caller_type: str
    transport: str
    allowed_methods: frozenset[str]
    bound_mission_id: str | None = None
    mission_generation: int | None = None
    is_external: bool = False


@dataclass(frozen=True)
class MissionCredential:
    """不含 bearer 明文的外部 mission handshake 憑證紀錄。"""

    token_hash: str
    audience: str
    mission_id: str
    generation: int
    caller_type: str
    created_at: float
    expires_at: float


def _token_hash(token: str) -> str:
    """將高熵 bearer token 正規化為固定長度雜湊。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionManager:
    """管理 Dispatch 短命憑證；儲存端只保留雜湊與授權分面。"""

    def __init__(self, ttl: int = SESSION_TTL_SECONDS) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._mission_tokens: dict[str, MissionCredential] = {}
        self._ttl = ttl
        self._mission_generation = 0
        self._lock = threading.RLock()

    @staticmethod
    def _find_constant_time[T](
        candidate_hash: str,
        records: dict[str, T],
        hash_getter: Any,
    ) -> tuple[str | None, T | None]:
        """固定長度 digest 全表比對；bearer 明文不作 dict key。"""
        matched_key: str | None = None
        matched_record: T | None = None
        for key, record in records.items():
            if secrets.compare_digest(candidate_hash, hash_getter(record)):
                matched_key = key
                matched_record = record
        return matched_key, matched_record

    def create_session(
        self,
        caller_info: dict[str, Any],
        *,
        transport: str = "direct",
    ) -> str:
        """建立內部 session，回傳唯一一次出現的 bearer 明文。"""
        token = secrets.token_urlsafe(32)
        now = get_current_epoch()
        token_hash = _token_hash(token)
        info = SessionInfo(
            token_hash=token_hash,
            agent_id=FIXED_DISPATCH_AGENT_ID,
            created_at=now,
            expires_at=now + self._ttl,
            audience="dispatch-internal",
            caller_type="internal",
            transport=transport,
            allowed_methods=_INTERNAL_METHODS,
        )
        with self._lock:
            self._sessions[token_hash] = info
        logger.info(
            "Dispatch session created for caller=%s",
            caller_info.get("name", "unknown"),
        )
        return token

    def create_mission_token(self, mission_id: str) -> str:
        """建立目前 generation 的 mission handshake 憑證（相容 API）。"""
        with self._lock:
            return self._create_mission_token_unlocked(mission_id)

    def _create_mission_token_unlocked(self, mission_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = get_current_epoch()
        token_hash = _token_hash(token)
        self._mission_tokens[token_hash] = MissionCredential(
            token_hash=token_hash,
            audience="dispatch-mission",
            mission_id=mission_id,
            generation=self._mission_generation,
            caller_type="external",
            created_at=now,
            expires_at=now + self._ttl,
        )
        logger.info(
            "Mission credential created for generation=%d",
            self._mission_generation,
        )
        return token

    def rotate_mission_token(self, mission_id: str) -> str:
        """原子替換 mission generation，撤銷舊憑證與舊外部 session。"""
        with self._lock:
            self._mission_generation += 1
            self._mission_tokens.clear()
            self._purge_external_sessions_unlocked()
            return self._create_mission_token_unlocked(mission_id)

    def validate_mission_credential(self, token: str) -> MissionCredential | None:
        """逐次以 constant-time digest 比對驗證 mission 憑證。"""
        candidate_hash = _token_hash(token)
        now = get_current_epoch()
        with self._lock:
            _key, credential = self._valid_mission_credential_unlocked(
                candidate_hash,
                now,
            )
        return credential

    def consume_mission_credential(self, token: str) -> MissionCredential | None:
        """原子驗證並消耗一次性 mission handshake 憑證。"""
        candidate_hash = _token_hash(token)
        now = get_current_epoch()
        with self._lock:
            key, credential = self._valid_mission_credential_unlocked(
                candidate_hash,
                now,
            )
            if key is not None and credential is not None:
                self._mission_tokens.pop(key, None)
            return credential

    def _valid_mission_credential_unlocked(
        self,
        candidate_hash: str,
        now: float,
    ) -> tuple[str | None, MissionCredential | None]:
        key, credential = self._find_constant_time(
            candidate_hash,
            self._mission_tokens,
            lambda value: value.token_hash,
        )
        if credential is None:
            return None, None
        if (
            now > credential.expires_at
            or credential.generation != self._mission_generation
        ):
            if key is not None:
                self._mission_tokens.pop(key, None)
            return None, None
        return key, credential

    def validate_mission_token(self, token: str) -> str | None:
        """相容 API：有效時只投影 mission_id。"""
        credential = self.validate_mission_credential(token)
        return credential.mission_id if credential is not None else None

    def revoke_mission_token(self, token: str) -> None:
        """撤銷特定 mission credential。"""
        candidate_hash = _token_hash(token)
        with self._lock:
            key, _credential = self._find_constant_time(
                candidate_hash,
                self._mission_tokens,
                lambda value: value.token_hash,
            )
            if key is not None:
                self._mission_tokens.pop(key, None)
        logger.info("Mission credential revoked")

    def clear_mission_tokens(self) -> None:
        """撤銷全部 mission credential 與其外部 session。"""
        with self._lock:
            count = len(self._mission_tokens)
            self._mission_generation += 1
            self._mission_tokens.clear()
            self._purge_external_sessions_unlocked()
        if count:
            logger.info("Cleared %d mission credential(s)", count)

    def create_external_session(
        self,
        mission_id: str,
        caller_info: dict[str, Any],
        *,
        generation: int | None = None,
        transport: str = "direct",
    ) -> str:
        """建立綁定 mission generation 與 transport 的外部 session。"""
        token = secrets.token_urlsafe(32)
        now = get_current_epoch()
        token_hash = _token_hash(token)
        with self._lock:
            effective_generation = (
                self._mission_generation if generation is None else generation
            )
            self._sessions[token_hash] = SessionInfo(
                token_hash=token_hash,
                agent_id=FIXED_DISPATCH_AGENT_ID,
                created_at=now,
                expires_at=now + self._ttl,
                audience="dispatch-mission",
                caller_type="external",
                transport=transport,
                allowed_methods=_EXTERNAL_METHODS,
                bound_mission_id=mission_id,
                mission_generation=effective_generation,
                is_external=True,
            )
        logger.info(
            "External dispatch session created for caller=%s generation=%d",
            caller_info.get("name", "unknown"),
            effective_generation,
        )
        return token

    def validate(
        self,
        session_token: str,
        *,
        transport: str = "direct",
        method: str | None = None,
    ) -> SessionInfo | None:
        """逐 request 驗證 expiry、transport、audience、method 與 generation。"""
        candidate_hash = _token_hash(session_token)
        now = get_current_epoch()
        with self._lock:
            key, info = self._find_constant_time(
                candidate_hash,
                self._sessions,
                lambda value: value.token_hash,
            )
            if info is None:
                return None
            if not self._has_consistent_authorization_facets(info):
                if key is not None:
                    self._sessions.pop(key, None)
                return None
            expired = now > info.expires_at
            stale_generation = (
                info.is_external
                and info.mission_generation != self._mission_generation
            )
            if expired or stale_generation:
                if key is not None:
                    self._sessions.pop(key, None)
                return None
            if info.transport != transport:
                return None
            if method is not None and method not in info.allowed_methods:
                return None
            return info

    @staticmethod
    def _has_consistent_authorization_facets(info: SessionInfo) -> bool:
        """逐 request 重驗 server-owned audience／caller／method 分面。"""
        if info.agent_id != FIXED_DISPATCH_AGENT_ID:
            return False
        if info.is_external:
            return (
                info.audience == "dispatch-mission"
                and info.caller_type == "external"
                and bool(info.bound_mission_id)
                and info.mission_generation is not None
                and info.allowed_methods == _EXTERNAL_METHODS
            )
        return (
            info.audience == "dispatch-internal"
            and info.caller_type == "internal"
            and info.bound_mission_id is None
            and info.mission_generation is None
            and info.allowed_methods == _INTERNAL_METHODS
        )

    def _purge_external_sessions_unlocked(self) -> None:
        stale_keys = [
            key for key, info in self._sessions.items() if info.is_external
        ]
        for key in stale_keys:
            self._sessions.pop(key, None)
