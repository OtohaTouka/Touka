"""小幫手的主流程（ADR-0311）：拿工作、分派、等人核准、確認之後才動手、回報。"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from touka_local.connection import AuthLost, ConnectionLost, ProtocolMismatch
from touka_local.errors import Refused
from touka_local.operations import ApplyLimits
from touka_local.protocol import ProtocolError, result_payload

if TYPE_CHECKING:
    from collections.abc import Callable

    from touka_local.approval import ApprovalGate, PendingApproval
    from touka_local.fsops import Clock
    from touka_local.grants import GrantBook
    from touka_local.operations import Operations, Plan
    from touka_local.protocol import Job, PollResponse

logger = logging.getLogger(__name__)

HELPER_EXECUTION_LIMIT_SECONDS: Final = 300.0
RETRY_SECONDS: Final = 3.0
# 和 Touka 的佇列上限相同
MAX_JOB_THREADS: Final = 8

STATE_STARTING: Final = "starting"
STATE_ONLINE: Final = "online"
STATE_OFFLINE: Final = "offline"
STATE_UNPAIRED: Final = "unpaired"
STATE_UPGRADE_NEEDED: Final = "upgrade_needed"

NOTICE_NOT_CONFIRMED: Final = "not_confirmed"
NOTICE_NOT_DELIVERED: Final = "result_not_delivered"
NOTICE_FINISHED: Final = "finished"
NOTICE_APPLY_FAILED: Final = "apply_failed"
NOTICE_PARTLY_DONE: Final = "partly_done"

CLOSE_NOW: Final = "now"
CLOSE_WAIT: Final = "wait"
CLOSE_BLOCKED: Final = "blocked"

_FALLBACK_MESSAGE: Final = "The local helper refused this request."
_MODEL_MESSAGES: Final = {
    "bad_path": "That path is not valid. Use folder name/relative path with forward slashes.",
    "bad_request": "The request was malformed.",
    "bad_target": "That action needs a file or folder inside an open folder.",
    "not_open": "That name is not one of the folders the user opened.",
    "read_only": "The user opened that folder as read only.",
    "protected": "That folder is protected and cannot be used.",
    "git_project": "Git projects cannot be used through this tool.",
    "link": "Links and cloud placeholders cannot be used through this tool.",
    "not_found": "Nothing exists at that path.",
    "exists": "Something already exists there; nothing is ever overwritten.",
    "not_a_file": "That path is not a file.",
    "not_a_folder": "That path is not a folder.",
    "not_text": "That file is not plain text.",
    "too_large": "That is too large.",
    "too_many": "That is too many items for one request.",
    "secret_name": "That file looks like a secret, so it is not read.",
    "file_type": "Only .txt, .md and .log files can be created.",
    "file_name": "Files with that name cannot be created or moved into place.",
    "extension_changed": "A move cannot change the file extension.",
    "cross_drive": "Moves cannot cross drives.",
    "conflict": "Items in the list overlap each other.",
    "changed": "The files changed while this was being prepared.",
    "user_rejected": "The user rejected this list. Nothing was changed.",
    "approval_busy": "Another list is waiting for the user. Wait for that answer first.",
    "approval_cooldown": "A list was just closed. Do not send another one right away.",
    "helper_busy": "The local helper is handling too many requests.",
}


class Client(Protocol):
    """Touka 的連線。"""

    def poll(self) -> PollResponse:
        """拿下一件工作。"""

    def confirm(self, job_id: str) -> bool:
        """確認工作還在。"""

    def result(self, job_id: str, payload: dict[str, Any]) -> bool:
        """回報結果。"""


@dataclass
class _Running:
    job_id: str
    stop: threading.Event


def _refused_payload(exc: Refused) -> dict[str, Any]:
    message = _MODEL_MESSAGES.get(exc.code, _FALLBACK_MESSAGE)
    if exc.detail:
        message = f"{message} ({exc.detail})"
    return result_payload("refused", code=exc.code, message=message)


def _notice_after(payload: dict[str, Any], *, delivered: bool) -> str:
    """做完之後要告訴人的話：只有每一項都做了才能說做完。"""
    if payload["status"] != "ok":
        return NOTICE_APPLY_FAILED
    if not delivered:
        return NOTICE_NOT_DELIVERED
    report = payload["data"]
    complete = all(item["status"] == "done" for item in report["items"])
    if complete and not report.get("journal_incomplete"):
        return NOTICE_FINISHED
    return NOTICE_PARTLY_DONE


class HelperService:
    """把連線、動作和核准關卡接起來。"""

    def __init__(
        self,
        *,
        client: Client | None,
        operations: Operations,
        gate: ApprovalGate,
        grants: GrantBook,
        clock: Clock,
        spawn: Callable[[Callable[[], None]], None],
    ) -> None:
        self._client = client
        self._operations = operations
        self._gate = gate
        self._grants = grants
        self._clock = clock
        self._spawn = spawn
        self._lock = threading.Lock()
        self._job_slots = threading.BoundedSemaphore(MAX_JOB_THREADS)
        self._running: _Running | None = None
        self.state = STATE_STARTING if client is not None else STATE_UNPAIRED
        self.notice = ""
        self.last_report: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        """正在執行一批。"""
        with self._lock:
            return self._running is not None

    def replace_client(self, client: Client | None) -> None:
        """配對換了。"""
        self._client = client
        self.state = STATE_STARTING if client is not None else STATE_UNPAIRED

    # --- 輪詢 ---

    def run(self, stop: threading.Event) -> None:
        """一直拿工作，直到視窗關閉。"""
        while not stop.is_set():
            if not self.poll_once():
                stop.wait(RETRY_SECONDS)

    def poll_once(self) -> bool:
        """拿一次工作。回傳這次有沒有連上。"""
        client = self._client
        if client is None:
            return False
        try:
            self._handle(client.poll())
        except AuthLost:
            self.state = STATE_UNPAIRED
            self._abandon_work()
            return False
        except (ProtocolMismatch, ProtocolError):
            self.state = STATE_UPGRADE_NEEDED
            return False
        except ConnectionLost:
            self.state = STATE_OFFLINE
            return False
        except Exception:
            logger.exception("polling failed")
            self.state = STATE_OFFLINE
            return False
        return True

    def _handle(self, response: PollResponse) -> None:
        self.state = STATE_ONLINE
        self._grants.set_mount_roots(response.host_mount_roots)
        for job_id in response.cancelled:
            self._cancel(job_id)
        if response.job is not None:
            self._start_job(response.job)

    def _start_job(self, job: Job) -> None:
        if not self._job_slots.acquire(blocking=False):
            self._send(job.id, _refused_payload(Refused("helper_busy")))
            return
        try:
            self._spawn(lambda: self._run_job(job))
        except RuntimeError:
            self._job_slots.release()
            raise

    def _abandon_work(self) -> None:
        """配對失效了：等待中的視窗收掉，執行中的停下來。"""
        pending = self._gate.current
        if pending is not None:
            self._gate.cancel(pending.job_id)
        self.stop_running()

    def _cancel(self, job_id: str) -> None:
        if self._gate.cancel(job_id):
            return
        with self._lock:
            if self._running is not None and self._running.job_id == job_id:
                self._running.stop.set()

    # --- 工作 ---

    def _run_job(self, job: Job) -> None:
        try:
            self._dispatch(job)
        finally:
            self._job_slots.release()

    def _dispatch(self, job: Job) -> None:
        try:
            if job.action == "propose":
                self._offer(job)
                return
            operations = self._operations
            handlers = {
                "list": operations.list,
                "read": operations.read,
                "create_folder": operations.create_folder,
                "create_file": operations.create_file,
            }
            self._send(job.id, result_payload("ok", data=handlers[job.action](job.args)))
        except Refused as exc:
            self._send(job.id, _refused_payload(exc))
        except OSError:
            logger.exception("file operation failed")
            self._send(
                job.id,
                result_payload(
                    "error", code="io_error", message="The file could not be accessed."
                ),
            )
        except Exception:
            logger.exception("unexpected failure")
            self._send(
                job.id,
                result_payload("error", code="internal", message="The helper hit a problem."),
            )

    def _offer(self, job: Job) -> None:
        if self.running:
            raise Refused("approval_busy")
        plan = self._operations.plan(job.args)
        self._gate.offer(job.id, plan, job.decide_within, requester=job.requester)

    def _send(self, job_id: str, payload: dict[str, Any]) -> bool:
        client = self._client
        return client is not None and client.result(job_id, payload)

    # --- 人的決定 ---

    def approve(self, job_id: str, digest: str) -> None:
        """人按了核准：先問 Touka 工作還在不在，確定了才動手。"""
        running = _Running(job_id, threading.Event())
        with self._lock:
            if self._running is not None:
                raise Refused("busy")
            plan = self._gate.approve(job_id, digest)
            self._running = running
        try:
            self._spawn(lambda: self._execute(running, plan))
        except RuntimeError as exc:
            with self._lock:
                self._running = None
            raise Refused("busy") from exc

    def reject(self, job_id: str) -> None:
        """人按了拒絕。"""
        if self._gate.reject(job_id):
            payload = _refused_payload(Refused("user_rejected"))
            self._spawn(lambda: self._send(job_id, payload))

    def tick(self) -> None:
        """時間到了自動拒絕。"""
        expired: PendingApproval | None = self._gate.expire_due()
        if expired is not None:
            job_id = expired.job_id
            self._spawn(lambda: self._send(job_id, result_payload("refused", code="no_decision")))

    def request_close(self) -> str:
        """人要關視窗。等人決定的清單不能默默丟掉，執行中的要先停。"""
        if self._gate.current is not None:
            return CLOSE_BLOCKED
        if self.running:
            self.stop_running()
            return CLOSE_WAIT
        return CLOSE_NOW

    def stop_running(self) -> None:
        """人按了停止：做完手上這項就停。"""
        with self._lock:
            if self._running is not None:
                self._running.stop.set()

    def undo(self, batch: str) -> dict[str, int]:
        """復原一批的搬移，只能從視窗按。"""
        if self.running:
            raise Refused("busy")
        return self._operations.undo(batch)

    def _execute(self, running: _Running, plan: Plan) -> None:
        self.last_report = None
        try:
            client = self._client
            if client is None or not client.confirm(running.job_id):
                self.notice = NOTICE_NOT_CONFIRMED
                return
            payload = self._apply(running, plan)
            self.last_report = payload["data"]
            delivered = self._send(running.job_id, payload)
            self.notice = _notice_after(payload, delivered=delivered)
        except Exception:
            logger.exception("the approved list did not finish")
            self.notice = NOTICE_APPLY_FAILED
        finally:
            with self._lock:
                self._running = None

    def _apply(self, running: _Running, plan: Plan) -> dict[str, Any]:
        limits = ApplyLimits(
            should_stop=running.stop.is_set,
            deadline=self._clock() + HELPER_EXECUTION_LIMIT_SECONDS,
        )
        try:
            return result_payload("ok", data=self._operations.apply(plan, limits))
        except Refused as exc:
            return _refused_payload(exc)
        except OSError:
            logger.exception("the list could not be applied")
            return result_payload(
                "error", code="io_error", message="The list could not be applied."
            )
        except Exception:
            logger.exception("the list could not be applied")
            return result_payload("error", code="internal", message="The helper hit a problem.")
