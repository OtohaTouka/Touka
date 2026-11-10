"""和 Touka 之間的資料格式（ADR-0311）。

Touka 端的 ``ToukaProxy/src/models/local_bridge.py`` 是準；這裡另存一份是因為小幫手不能
載入 Touka 的程式。``tests/unit/test_local_protocol_parity.py`` 核對兩邊相同。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Final

PROTOCOL_VERSION: Final = 1
MAX_JOB_RESULT_BYTES: Final = 1024 * 1024
MAX_ERROR_MESSAGE_CHARS: Final = 2000
MAX_CODE_CHARS: Final = 64
MAX_REQUESTER_CHARS: Final = 128

ACTIONS: Final = ("list", "read", "create_folder", "create_file", "propose")
RESULT_STATUSES: Final = ("ok", "refused", "error")
POLL_REQUEST_FIELDS: Final = frozenset({"protocol", "helper_version"})
POLL_RESPONSE_FIELDS: Final = frozenset({"job", "cancelled", "host_mount_roots"})
JOB_FIELDS: Final = frozenset({"id", "action", "args", "requester", "decide_within"})
RESULT_FIELDS: Final = frozenset({"status", "code", "message", "data"})

JOB_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ProtocolError(Exception):
    """Touka 送來的內容不是這一版協議認得的。"""


@dataclass(frozen=True)
class Job:
    """一件工作。"""

    id: str
    action: str
    args: dict[str, Any]
    requester: str
    decide_within: float


@dataclass(frozen=True)
class PollResponse:
    """一次輪詢的回應。"""

    job: Job | None
    cancelled: tuple[str, ...]
    host_mount_roots: tuple[str, ...]


def _text_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProtocolError
    return tuple(value)


def _parse_job(value: object) -> Job:
    if not isinstance(value, dict) or set(value) != JOB_FIELDS:
        raise ProtocolError
    job_id, action, args = value["id"], value["action"], value["args"]
    requester, decide_within = value["requester"], value["decide_within"]
    if (
        not isinstance(job_id, str)
        or JOB_ID.fullmatch(job_id) is None
        or action not in ACTIONS
        or not isinstance(args, dict)
        or not isinstance(requester, str)
        or isinstance(decide_within, bool)
        or not isinstance(decide_within, int | float)
        or not math.isfinite(decide_within)
        or decide_within <= 0
    ):
        raise ProtocolError
    return Job(job_id, action, args, requester[:MAX_REQUESTER_CHARS], float(decide_within))


def parse_poll_response(payload: object) -> PollResponse:
    """解析輪詢回應，多一個或少一個欄位都拒絕。"""
    if not isinstance(payload, dict) or set(payload) != POLL_RESPONSE_FIELDS:
        raise ProtocolError
    raw_job = payload["job"]
    return PollResponse(
        job=None if raw_job is None else _parse_job(raw_job),
        cancelled=_text_list(payload["cancelled"]),
        host_mount_roots=_text_list(payload["host_mount_roots"]),
    )


def poll_request(helper_version: str) -> dict[str, Any]:
    """輪詢要帶的內容。"""
    return {"protocol": PROTOCOL_VERSION, "helper_version": helper_version}


def result_payload(
    status: str,
    *,
    code: str | None = None,
    message: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """組回報的內容，長度超過協議上限的不送出去。"""
    payload = {
        "status": status,
        "code": None if code is None else code[:MAX_CODE_CHARS],
        "message": None if message is None else message[:MAX_ERROR_MESSAGE_CHARS],
        "data": data,
    }
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_JOB_RESULT_BYTES:
        return {
            "status": "error",
            "code": "result_too_large",
            "message": "The result was too large to send back.",
            "data": None,
        }
    return payload
