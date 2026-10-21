"""Human-owned schedule management through the fixed Heartbeat service origin."""

import asyncio
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from pydantic import TypeAdapter, ValidationError

from src.core.exceptions import ErrorCode, ToukaException
from src.models.heartbeat_schedule import HeartbeatSchedule, schedules_revision

_SCHEDULES = TypeAdapter(list[HeartbeatSchedule])
_OUTBOUND_TIMEOUT_SECONDS = 10.0
_SAFE_PHASES = frozenset(
    {
        "read",
        "read_before_write",
        "inspect_entry",
        "decode",
        "validate",
        "encode",
        "temp_create",
        "write",
        "flush",
        "file_fsync",
        "temp_close",
        "replace",
        "directory_fsync",
        "plan",
        "engine",
        "startup",
        "compensation",
        "revision",
        "request_validation",
    }
)
_SAFE_STATES = frozenset({"OLD", "NEW", "CURRENT", "UNKNOWN", "PAUSED", "STOPPED"})


class HeartbeatScheduleBridge:
    """Translate human authority to one service request without exposing its key."""

    def __init__(self, origin: str | None, token: str, can_manage: Callable[[str], bool]) -> None:
        self.token = token.strip()
        self.can_manage = can_manage
        try:
            parsed = urlsplit(origin or "")
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ToukaException(ErrorCode.HEARTBEAT_UPSTREAM_FAILED)
            _ = parsed.port  # Property access validates a supplied port.
        except ValueError:
            raise ToukaException(ErrorCode.HEARTBEAT_UPSTREAM_FAILED) from None
        self.origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    async def _request(
        self,
        method: str,
        path: str = "/v1/schedules",
        *,
        revision: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        if not self.token:
            raise ToukaException(ErrorCode.HEARTBEAT_SHARED_CREDENTIAL_NOT_READY)
        headers = {"Authorization": f"Bearer {self.token}"}
        if revision is not None:
            headers["If-Match"] = revision
        try:
            async with asyncio.timeout(_OUTBOUND_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    timeout=_OUTBOUND_TIMEOUT_SECONDS, follow_redirects=False
                ) as client:
                    response = await client.request(
                        method, self.origin + path, headers=headers, json=body
                    )
        except (TimeoutError, httpx.TimeoutException):
            code = (
                ErrorCode.HEARTBEAT_UPSTREAM_FAILED
                if method == "GET"
                else ErrorCode.HEARTBEAT_OUTCOME_UNKNOWN
            )
            raise ToukaException(code) from None
        except httpx.HTTPError:
            code = (
                ErrorCode.HEARTBEAT_UPSTREAM_FAILED
                if method == "GET"
                else ErrorCode.HEARTBEAT_OUTCOME_UNKNOWN
            )
            raise ToukaException(code) from None
        if not 200 <= response.status_code < 300:
            self._raise_upstream_failure(response)
        return response

    @staticmethod
    def _raise_upstream_failure(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise ToukaException(ErrorCode.HEARTBEAT_UPSTREAM_FAILED)
        try:
            detail = response.json().get("detail", {})
            code = detail.get("code")
            if not isinstance(code, str) or not code.startswith("HEARTBEAT_"):
                raise ToukaException(ErrorCode.HEARTBEAT_UPSTREAM_FAILED)
            selected = ErrorCode[code]
            safe: dict[str, Any] = {}
            if detail.get("phase") in _SAFE_PHASES:
                safe["phase"] = detail["phase"]
            if detail.get("committed") is None or isinstance(detail.get("committed"), bool):
                safe["committed"] = detail.get("committed")
            for name in ("persisted", "engine"):
                if detail.get(name) in _SAFE_STATES:
                    safe[name] = detail[name]
            if detail.get("durability") in {
                "CONFIRMED",
                "NOT_CONFIRMED",
                "DIRECTORY_FSYNC_UNSUPPORTED",
            }:
                safe["durability"] = detail["durability"]
        except (ValueError, KeyError, AttributeError, TypeError):
            raise ToukaException(ErrorCode.HEARTBEAT_UPSTREAM_FAILED) from None
        raise ToukaException(selected, **safe)

    async def _read(self) -> tuple[list[HeartbeatSchedule], str]:
        response = await self._request("GET")
        try:
            body = response.json()
            schedules = _SCHEDULES.validate_python(body)
            identifiers = [schedule.id for schedule in schedules]
            if any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(
                identifiers
            ):
                raise ToukaException(ErrorCode.HEARTBEAT_UPSTREAM_FAILED)
            revision = response.headers.get("ETag")
            if revision != schedules_revision(body):
                raise ToukaException(ErrorCode.HEARTBEAT_UPSTREAM_FAILED)
        except (ValueError, TypeError, ValidationError):
            raise ToukaException(ErrorCode.HEARTBEAT_UPSTREAM_FAILED) from None
        return schedules, revision

    async def list(self) -> tuple[list[HeartbeatSchedule], str]:
        """Filter the validated service list by current human ownership."""
        schedules, revision = await self._read()
        return [schedule for schedule in schedules if self.can_manage(schedule.agent_id)], revision

    @staticmethod
    def _require_revision(revision: str | None) -> str:
        if not revision:
            raise ToukaException(ErrorCode.HEARTBEAT_REVISION_REQUIRED)
        return revision

    async def upsert(self, schedule: HeartbeatSchedule, revision: str | None) -> HeartbeatSchedule:
        """Authorize old and new owners, then mutate once with the exact revision."""
        expected = self._require_revision(revision)
        schedules, current = await self._read()
        existing = next((item for item in schedules if item.id == schedule.id), None)
        if existing is not None and not self.can_manage(existing.agent_id):
            raise ToukaException(ErrorCode.HEARTBEAT_SCHEDULE_NOT_FOUND)
        if not self.can_manage(schedule.agent_id):
            raise ToukaException(ErrorCode.HEARTBEAT_UNAUTHORIZED_AGENT)
        if expected != current:
            raise ToukaException(ErrorCode.HEARTBEAT_REVISION_CONFLICT)
        response = await self._request(
            "POST", revision=expected, body=schedule.model_dump(exclude_unset=True)
        )
        try:
            return HeartbeatSchedule.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ToukaException(ErrorCode.HEARTBEAT_OUTCOME_UNKNOWN) from None

    async def delete(self, identifier: str, revision: str | None) -> None:
        """Delete only a currently visible identifier with a matching revision."""
        expected = self._require_revision(revision)
        schedules, current = await self._read()
        existing = next((item for item in schedules if item.id == identifier), None)
        if existing is None or not self.can_manage(existing.agent_id):
            raise ToukaException(ErrorCode.HEARTBEAT_SCHEDULE_NOT_FOUND)
        if expected != current:
            raise ToukaException(ErrorCode.HEARTBEAT_REVISION_CONFLICT)
        escaped = quote(identifier, safe="").replace(".", "%2E")
        await self._request("DELETE", f"/v1/schedules/{escaped}", revision=expected)
