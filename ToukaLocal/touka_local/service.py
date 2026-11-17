"""小幫手的主流程（ADR-0312、ADR-0317、ADR-0320）.

拿工作、分派、等人核准（或核對 Touka 照模式的決定）、確認之後才動手、回報。
開放請求一律跳視窗，人按核准就加進名單，不經 Touka 確認。
"""

from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Protocol

from touka_local.connection import AuthLost, ConnectionLost, ProtocolMismatch
from touka_local.errors import Refused
from touka_local.floors import plan_entries, window_reason
from touka_local.open_request import (
    OPEN_DECISION_SECONDS,
    REASON_INTERNAL,
    REASON_UNREADABLE,
    REMEMBERED_NONCES,
    STATE_NOT_OPENABLE,
    STATE_OPENABLE,
    OpenPause,
    OpenRequest,
    checking,
    verify_open_request,
)
from touka_local.operations import ApplyLimits, Plan
from touka_local.protocol import DECIDED_BY_HELPER_WINDOW, ProtocolError, result_payload
from touka_local.signing import decision_signature_ok, text_fingerprint
from touka_local.view import preview_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from touka_local.approval import ApprovalGate, PendingApproval
    from touka_local.fsops import Clock
    from touka_local.grants import GrantBook
    from touka_local.operations import Operations
    from touka_local.protocol import Job, PollResponse

logger = logging.getLogger(__name__)

HELPER_EXECUTION_LIMIT_SECONDS: Final = 300.0
RETRY_SECONDS: Final = 3.0
# 和 Touka 的佇列上限相同
MAX_JOB_THREADS: Final = 8
# 取消可能比工作先到；工作最多排 8 件，記 32 件夠了
REMEMBERED_CANCELS: Final = 32
# 預覽的一次性號碼只在記憶體、只用一次；比 Touka 等決定的時間短（ADR-0317）
PREVIEW_LIFETIME_SECONDS: Final = 540.0
MAX_PREVIEWS: Final = 8

STATE_STARTING: Final = "starting"
STATE_ONLINE: Final = "online"
STATE_OFFLINE: Final = "offline"
STATE_UNPAIRED: Final = "unpaired"
STATE_UPGRADE_NEEDED: Final = "upgrade_needed"
STATE_EXPOSED: Final = "exposed"

NOTICE_NOT_CONFIRMED: Final = "not_confirmed"
NOTICE_NOT_DELIVERED: Final = "result_not_delivered"
NOTICE_FINISHED: Final = "finished"
NOTICE_APPLY_FAILED: Final = "apply_failed"
NOTICE_PARTLY_DONE: Final = "partly_done"

CLOSE_NOW: Final = "now"
CLOSE_WAIT: Final = "wait"
CLOSE_BLOCKED: Final = "blocked"

_ACTIONS_WITH_DECISION: Final = frozenset({"propose", "request_open"})

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
    "file_name": (
        "That name cannot be used for something new: it starts with a dot, contains a "
        "character that looks like a path separator, a space or nothing at all, or is "
        "a file other tools read automatically."
    ),
    "odd_name_inside": (
        "Something inside that folder has a name this tool cannot handle safely, "
        "so the folder cannot be moved or deleted as a whole."
    ),
    "extension_changed": "A move cannot change the file extension.",
    "cross_drive": "Moves cannot cross drives.",
    "conflict": "Items in the list overlap each other.",
    "changed": "The files changed while this was being prepared.",
    "user_rejected": "The user rejected this list. Nothing was changed.",
    "approval_busy": "Another list is waiting for the user. Wait for that answer first.",
    "approval_cooldown": "A list was just closed. Do not send another one right away.",
    "helper_busy": "The local helper is handling too many requests.",
    "not_supported": "This version of the local helper does not do that yet.",
    "bad_signature": "The local helper could not verify that this request came from Touka.",
    "needs_new_pairing": "The local helper has no key for this pairing. Pair again.",
    "open_paused": "The local helper is not taking folder requests for a while.",
    "not_opened": "The user did not open that folder.",
    "not_openable": "That place cannot be opened. The reason is shown in the helper window.",
    "recently_vacated": (
        "Something was moved away from or deleted at that place in the last 7 days. "
        "To avoid quietly replacing it, ask the user to create it themselves or pick "
        "another name."
    ),
    "journal_unreadable": (
        "The local helper cannot read one of its batch records, so it will not create "
        "anything. The user has to check the helper window."
    ),
    "helper_exposed": (
        "The local helper stopped because its own files are inside a folder Touka can reach. "
        "The user has to fix this on their computer."
    ),
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


@dataclass(frozen=True)
class _Preview:
    """一份預覽：一次性號碼對到的清單與顯示文字的指紋。"""

    plan: Plan
    shown: str
    expires: float
    # 預覽時就碰到的底線：卡片會說「小幫手還會跳視窗」，之後原因消失也照樣跳
    reason: str | None = None


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


def _check_supported(job: Job) -> None:
    """決定只能出現在搬移、刪除清單，以及開放請求的章（ADR-0320）。"""
    if job.decision is not None and job.action not in _ACTIONS_WITH_DECISION:
        raise Refused("bad_request")


def _is_plan(plan: object) -> bool:
    return isinstance(plan, Plan)


def _is_open_request(plan: object) -> bool:
    return isinstance(plan, OpenRequest)


def _is_openable(plan: object) -> bool:
    return isinstance(plan, OpenRequest) and plan.openable


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
        decision_key: Callable[[], bytes | None] = lambda: None,
        following: Callable[[], bool] = lambda: False,
        mode_reset: Callable[[], float | None] = lambda: None,
    ) -> None:
        self._client = client
        self._operations = operations
        self._gate = gate
        self._grants = grants
        self._clock = clock
        self._spawn = spawn
        self._decision_key = decision_key
        self._following = following
        self._mode_reset = mode_reset
        self._previews: dict[str, _Preview] = {}
        self._lock = threading.Lock()
        self._job_slots = threading.BoundedSemaphore(MAX_JOB_THREADS)
        self._running: _Running | None = None
        self._cancelled: list[str] = []
        # 開放請求（ADR-0320）：用過的一次性號碼與停收規則，都只在 self._lock 裡讀寫
        self._open_nonces: list[str] = []
        self._open_pause = OpenPause()
        self.state = STATE_STARTING if client is not None else STATE_UNPAIRED
        self.notice = ""
        self.last_report: dict[str, Any] | None = None
        # 最近一次開放請求核准後的結果：(代碼, 名稱)，主視窗顯示用；不含 Touka 送來的路徑
        self.open_outcome: tuple[str, str | None] | None = None

    @property
    def open_paused_until(self) -> float | None:
        """開放請求停收到什麼時候（小幫手時鐘）；沒有停收回 None。"""
        with self._lock:
            return self._open_pause.paused_until(self._clock())

    def resume_open_requests(self) -> None:
        """人在主視窗按「現在恢復」。"""
        with self._lock:
            self._open_pause.resume()

    def _count_open_miss(self) -> None:
        """開放請求沒核准一次。呼叫時不能持有 gate 的鎖或自己的鎖。"""
        with self._lock:
            self._open_pause.missed(self._clock())

    @property
    def running(self) -> bool:
        """正在執行一批。"""
        with self._lock:
            return self._running is not None

    def replace_client(self, client: Client | None) -> None:
        """配對換了。舊配對時發出的預覽號碼一起作廢。"""
        with self._lock:
            self._previews.clear()
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
        self._grants.set_mount_roots(response.host_mount_roots)
        exposed = self._grants.helper_is_exposed()
        self.state = STATE_EXPOSED if exposed else STATE_ONLINE
        if exposed:
            self._abandon_work()
        for job_id in response.cancelled:
            self._cancel(job_id)
        if response.job is None:
            return
        if exposed:
            self._send(response.job.id, _refused_payload(Refused("helper_exposed")))
        else:
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
        """配對失效或小幫手被看見：等待中的視窗收掉、不回報，執行中的停下來。

        收掉的是開放請求就算一次沒核准，Touka 才不能反覆讓視窗一閃就收（ADR-0320）。
        """
        closed = self._gate.abandon()
        if closed is not None and _is_open_request(closed.plan):
            self._count_open_miss()
        self.stop_running()

    def _cancel(self, job_id: str) -> None:
        """Touka 撤回。開放請求不理會撤回，視窗留到人決定或時間到（ADR-0320）。"""
        with self._lock:
            self._cancelled = [*self._cancelled, job_id][-REMEMBERED_CANCELS:]
            if self._gate.cancel(job_id, _is_plan) is not None:
                return
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
            _check_supported(job)
            if job.action == "preview":
                self._send(job.id, result_payload("ok", data=self._preview(job)))
                return
            if job.action == "propose":
                if job.decision is None:
                    self._offer(job)
                else:
                    self._decide(job)
                return
            if job.action == "request_open":
                self._request_open(job)
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
                result_payload("error", code="io_error", message="The file could not be accessed."),
            )
        except Exception:
            logger.exception("unexpected failure")
            self._send(
                job.id,
                result_payload("error", code="internal", message="The helper hit a problem."),
            )

    def _request_open(self, job: Job) -> None:
        """開放請求（ADR-0320）：先佔住視窗、再檢查路徑；能不能開放都跳視窗，人決定前不回報。

        撤回不理會，視窗時間用小幫手自己的 120 秒，不用 Touka 給的。
        """
        with self._lock:
            if self._open_pause.paused_until(self._clock()) is not None:
                raise Refused("open_paused")
        path = verify_open_request(job, self._decision_key())
        assert job.decision is not None  # verify_open_request 已確認
        with self._lock:
            if job.decision.nonce in self._open_nonces:
                raise Refused("bad_signature")
            self._open_nonces = [*self._open_nonces, job.decision.nonce][-REMEMBERED_NONCES:]
            self._gate.offer(
                job.id, checking(path), OPEN_DECISION_SECONDS, requester=job.requester
            )
        self._gate.fill(job.id, _is_open_request, self._checked(path))

    def _checked(self, path: str) -> OpenRequest:
        """路徑檢查。任何失敗都變成「不能開放」的視窗，原因只顯示在小幫手這邊。"""
        base = checking(path)
        try:
            folder = self._grants.check_can_add(path)
        except Refused as exc:
            return replace(base, state=STATE_NOT_OPENABLE, reason=exc.code)
        except OSError:
            return replace(base, state=STATE_NOT_OPENABLE, reason=REASON_UNREADABLE)
        except Exception:
            logger.exception("checking a folder request failed")
            return replace(base, state=STATE_NOT_OPENABLE, reason=REASON_INTERNAL)
        return replace(base, state=STATE_OPENABLE, folder=folder)

    def _offer(self, job: Job) -> None:
        if self.running:
            raise Refused("approval_busy")
        plan = self._operations.plan(job.args)
        with self._lock:
            # 建清單時可能有照模式的批開始跑，跳視窗那一刻再看一次
            if self._running is not None:
                raise Refused("approval_busy")
            if job.id not in self._cancelled:
                self._gate.offer(job.id, plan, job.decide_within, requester=job.requester)

    def _window_reason(self, plan: Plan, *, decision_ok: bool) -> str | None:
        now = self._operations.wall_time()
        history = replace(self._operations.history(now), reset_at=self._mode_reset())
        return window_reason(
            plan, history, following=self._following(), decision_ok=decision_ok, now=now
        )

    def _preview(self, job: Job) -> dict[str, Any]:
        """建清單、記一次性號碼，回給 Touka 顯示在卡片上（ADR-0317）。"""
        plan = self._operations.plan(job.args)
        text = preview_text(plan)
        nonce = secrets.token_urlsafe(16)
        reason = self._window_reason(plan, decision_ok=self._decision_key() is not None)
        now = self._clock()
        with self._lock:
            self._previews = {
                key: kept for key, kept in self._previews.items() if kept.expires > now
            }
            while len(self._previews) >= MAX_PREVIEWS:
                del self._previews[next(iter(self._previews))]
            self._previews[nonce] = _Preview(
                plan=plan,
                shown=text_fingerprint(text),
                expires=now + PREVIEW_LIFETIME_SECONDS,
                reason=reason,
            )
        return {
            "nonce": nonce,
            "digest": plan.digest,
            "text": text,
            "items": len(plan.items),
            "entries": plan_entries(plan),
            "marks": sorted({mark for item in plan.items for mark in item.unusual}),
            "window_reason": reason,
        }

    def _decision_ok(self, job: Job, preview: _Preview | None) -> bool:
        """章、號碼、清單指紋、顯示文字指紋都要對；任何一項不對就當作沒有決定。"""
        decision = job.decision
        return (
            decision is not None
            and preview is not None
            and preview.expires > self._clock()
            and decision.digest == preview.plan.digest
            and decision.shown == preview.shown
            and decision_signature_ok(self._decision_key(), job.id, decision)
        )

    def _decide(self, job: Job) -> None:
        """Touka 照模式決定的清單：核對、重掃、看底線；碰到底線就改跳視窗。

        一次只處理一份：在鎖裡先佔住執行的位置，同時送來的第二份回忙碌，總量與 7 天
        檢查才不會兩份都看到「還沒有我」。跳視窗前在同一把鎖裡放掉位置，人才按得了。
        """
        running = _Running(job.id, threading.Event())
        decision = job.decision
        with self._lock:
            if self._running is not None or self._gate.current is not None:
                raise Refused("approval_busy")
            self._running = running
            preview = self._previews.pop(decision.nonce, None) if decision else None
        try:
            decision_ok = self._decision_ok(job, preview)
            plan = self._operations.plan(job.args)
            if decision_ok and preview is not None and plan.digest != preview.plan.digest:
                raise Refused("changed")
            reason = self._window_reason(plan, decision_ok=decision_ok)
            if reason is None and preview is not None:
                reason = preview.reason
            if reason is not None:
                with self._lock:
                    if job.id not in self._cancelled:
                        self._gate.offer(
                            job.id,
                            plan,
                            job.decide_within,
                            requester=job.requester,
                            reason=reason,
                        )
                    self._running = None
                return
            assert decision is not None  # decision_ok 已確認
            self._execute(running, plan, decided_by=decision.decided_by, by_mode=True)
        finally:
            with self._lock:
                if self._running is running:
                    self._running = None

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
            plan = self._gate.approve(job_id, digest, _is_plan)
            self._running = running
        try:
            self._spawn(lambda: self._execute(running, plan))
        except RuntimeError as exc:
            with self._lock:
                self._running = None
            raise Refused("busy") from exc

    def approve_open(self, job_id: str, digest: str, *, writable: bool) -> None:
        """人在開放視窗按了核准：加進名單、回報 Touka。人看著完整路徑決定的，不經 Touka 確認。"""
        request: OpenRequest = self._gate.approve(job_id, digest, _is_openable)
        self._spawn(lambda: self._add_folder(job_id, request, writable=writable))

    def _add_folder(self, job_id: str, request: OpenRequest, *, writable: bool) -> None:
        try:
            grant = self._grants.add(request.folder or request.path, writable=writable)
        except Refused as exc:
            # 核准前資料夾被換掉之類：原因只留在小幫手這邊，Touka 只知道沒開成
            self.open_outcome = (exc.code, None)
            self._count_open_miss()
            self._send(job_id, _refused_payload(Refused("not_openable")))
            return
        except OSError:
            logger.exception("the open folders list could not be saved")
            self.open_outcome = ("io_error", None)
            self._count_open_miss()
            self._send(
                job_id,
                result_payload(
                    "error", code="io_error", message="The open folders list could not be saved."
                ),
            )
            return
        with self._lock:
            self._open_pause.resume()
        self.open_outcome = ("opened", grant.name)
        self._send(
            job_id, result_payload("ok", data={"name": grant.name, "writable": grant.writable})
        )

    def reject(self, job_id: str) -> None:
        """人按了拒絕；開放請求的「知道了」也是這個，兩種開放視窗回同一個代碼。"""
        closed = self._gate.reject(job_id)
        if closed is None:
            return
        if _is_open_request(closed.plan):
            self._count_open_miss()
            payload = _refused_payload(Refused("not_opened"))
        else:
            payload = _refused_payload(Refused("user_rejected"))
        self._spawn(lambda: self._send(job_id, payload))

    def tick(self) -> None:
        """時間到了自動拒絕。"""
        expired: PendingApproval | None = self._gate.expire_due()
        if expired is not None:
            if _is_open_request(expired.plan):
                self._count_open_miss()
            job_id = expired.job_id
            self._spawn(lambda: self._send(job_id, result_payload("refused", code="no_decision")))

    def close_blocked(self) -> bool:
        """有清單在等人決定時不能關。只看不動，不會停掉執行中的批。"""
        return self._gate.current is not None

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

    def _execute(
        self,
        running: _Running,
        plan: Plan,
        *,
        decided_by: str = DECIDED_BY_HELPER_WINDOW,
        by_mode: bool = False,
    ) -> None:
        self.last_report = None
        try:
            client = self._client
            if client is None or not client.confirm(running.job_id):
                self.notice = NOTICE_NOT_CONFIRMED
                return
            payload = self._apply(running, plan, by_mode=by_mode)
            if payload["status"] == "ok":
                payload["data"]["decided_by"] = decided_by
            self.last_report = payload["data"]
            delivered = self._send(running.job_id, payload)
            self.notice = _notice_after(payload, delivered=delivered)
        except Exception:
            logger.exception("the approved list did not finish")
            self.notice = NOTICE_APPLY_FAILED
        finally:
            with self._lock:
                self._running = None

    def _apply(self, running: _Running, plan: Plan, *, by_mode: bool) -> dict[str, Any]:
        limits = ApplyLimits(
            should_stop=running.stop.is_set,
            deadline=self._clock() + HELPER_EXECUTION_LIMIT_SECONDS,
        )
        try:
            return result_payload("ok", data=self._operations.apply(plan, limits, by_mode=by_mode))
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
