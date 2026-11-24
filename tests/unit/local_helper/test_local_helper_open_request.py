"""從 Touka 請小幫手開放資料夾（ADR-0320）。

驗章通過的開放請求，不論位置能不能開放都跳視窗；人動手或 120 秒到之前，Touka 收不到任何和
位置有關的回報。撤回與 Touka 給的等待時間都不理會。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest
from touka_local.approval import APPROVE_DELAY_SECONDS, ApprovalGate
from touka_local.connection import AuthLost
from touka_local.errors import Refused
from touka_local.grants import Environment, GrantBook
from touka_local.open_request import (
    OPEN_DECISION_SECONDS,
    OPEN_PAUSE_SECONDS,
    OPEN_REFUSALS_BEFORE_PAUSE,
    STATE_NOT_OPENABLE,
    STATE_OPENABLE,
    OpenRequest,
    checking,
)
from touka_local.operations import Operations
from touka_local.protocol import DECIDED_BY_OPEN_REQUEST, Decision, Job, PollResponse
from touka_local.service import MAX_JOB_THREADS, HelperService
from touka_local.signing import sign_decision, sign_open_request, text_fingerprint
from touka_local.view import (
    OPEN_DEFAULT_WRITABLE,
    WINDOW_OPEN,
    WINDOW_PLAN,
    approval_window_kind,
    open_outcome_text,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from touka_local.grants import Grant

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
HELPER_DIR = "C:\\Users\\Me\\AppData\\Local\\ToukaLocal"
WANTED = "D:\\Photos"


@dataclass
class _Client:
    responses: list[object] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    results: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def poll(self) -> PollResponse:
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]

    def confirm(self, job_id: str) -> bool:
        self.confirmed.append(job_id)
        return True

    def result(self, job_id: str, payload: dict[str, Any]) -> bool:
        self.results.append((job_id, payload))
        return True

    def report_folders(self, _payload: dict[str, Any]) -> bool:
        return True


@dataclass
class _Key:
    value: bytes | None = KEY


class _SpyBook(GrantBook):
    """記下路徑檢查那一刻視窗佔住了沒有；也可以讓檢查丟錯或改動時鐘。"""

    def __init__(self, *args: Any, gate: ApprovalGate, clock: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.spy_gate = gate
        self.spy_clock = clock
        self.seen_pending: list[object] = []
        self.raise_on_check: BaseException | None = None
        self.raise_on_add: BaseException | None = None
        self.advance_during_check = 0.0

    def check_can_add(self, path: str) -> str:
        pending = self.spy_gate.current
        self.seen_pending.append(pending.plan if pending is not None else None)
        self.spy_clock.now += self.advance_during_check
        if self.raise_on_check is not None:
            raise self.raise_on_check
        return super().check_can_add(path)

    def add(self, path: str, *, writable: bool) -> Grant:
        if self.raise_on_add is not None:
            raise self.raise_on_add
        return super().add(path, writable=writable)


@pytest.fixture
def client() -> _Client:
    return _Client()


@pytest.fixture
def key() -> _Key:
    return _Key()


@pytest.fixture
def gate(clock) -> ApprovalGate:
    return ApprovalGate(clock=clock)


ENV = Environment(
    user_profile="C:\\Users\\Me",
    windows_dir="C:\\Windows",
    program_files=("C:\\Program Files",),
    program_data="C:\\ProgramData",
    app_data="C:\\Users\\Me\\AppData\\Roaming",
    local_app_data="C:\\Users\\Me\\AppData\\Local",
    helper_dir=HELPER_DIR,
    code_paths=("C:\\Python",),
    touka_folder="D:\\Touka",
)


@pytest.fixture
def book(fs, tmp_path: Path, gate: ApprovalGate, clock) -> _SpyBook:
    fs.add_dir("D:\\Open")
    fs.add_dir(WANTED)
    spy = _SpyBook(
        tmp_path / "g.json",
        fs=fs,
        env=ENV,
        drive_type=lambda _a: "fixed",
        gate=gate,
        clock=clock,
    )
    spy.add("D:\\Open", writable=True)
    return spy


@dataclass
class _Parts:
    client: _Client
    fs: Any
    book: _SpyBook
    trash: Any
    clock: Any
    gate: ApprovalGate
    tmp_path: Path
    key: _Key

    def service(self, spawn: Callable[[Callable[[], None]], None]) -> HelperService:
        operations = Operations(
            fs=self.fs,
            grants=self.book,
            trash=self.trash,
            clock=self.clock,
            journal_dir=self.tmp_path / "j",
        )
        return HelperService(
            client=self.client,
            operations=operations,
            gate=self.gate,
            grants=self.book,
            clock=self.clock,
            spawn=spawn,
            decision_key=lambda: self.key.value,
        )


@pytest.fixture
def parts(
    client: _Client, fs, book: _SpyBook, trash, clock, gate: ApprovalGate, tmp_path: Path, key
) -> _Parts:
    return _Parts(client, fs, book, trash, clock, gate, tmp_path, key)


@pytest.fixture
def service(parts: _Parts) -> HelperService:
    return parts.service(lambda work: work())


_NONCES = iter(f"{n:022d}" for n in range(10**6))


def _decision(
    path: str, *, job_id: str, key: bytes = KEY, nonce: str | None = None, **changes: str
) -> Decision:
    fields = {
        "nonce": nonce if nonce is not None else next(_NONCES),
        "digest": text_fingerprint(path),
        "shown": text_fingerprint(path),
        "decided_by": DECIDED_BY_OPEN_REQUEST,
    }
    fields.update(changes)
    signature = sign_open_request(key, job_id=job_id, nonce=fields["nonce"], path=path)
    return Decision(signature=signature, **fields)


def _open_job(
    path: str = WANTED,
    *,
    job_id: str = "o1",
    decision: Decision | None | str = "sign",
    args: dict[str, Any] | None = None,
    decide_within: float = OPEN_DECISION_SECONDS,
) -> Job:
    if decision == "sign":
        decision = _decision(path, job_id=job_id)
    return Job(
        id=job_id,
        action="request_open",
        args={"path": path} if args is None else args,
        requester="owner",
        decide_within=decide_within,
        decision=decision,  # type: ignore[arg-type]
    )


def _poll(
    service: HelperService,
    client: _Client,
    job: Job | None = None,
    *,
    cancelled: tuple[str, ...] = (),
    mounts: tuple[str, ...] = (),
) -> None:
    client.responses.append(PollResponse(job=job, cancelled=cancelled, host_mount_roots=mounts))
    service.poll_once()


def _codes(client: _Client) -> list[str | None]:
    return [payload["code"] for _job_id, payload in client.results]


def _pending_request(gate: ApprovalGate) -> OpenRequest:
    pending = gate.current
    assert pending is not None
    assert isinstance(pending.plan, OpenRequest)
    return pending.plan


def _approve(service: HelperService, gate: ApprovalGate, clock, *, writable: bool) -> None:
    request = _pending_request(gate)
    clock.now += APPROVE_DELAY_SECONDS
    service.approve_open(gate.current.job_id, request.digest, writable=writable)


def _refuse_three_times(service: HelperService, client: _Client, gate: ApprovalGate, clock) -> None:
    for number in range(OPEN_REFUSALS_BEFORE_PAUSE):
        _poll(service, client, _open_job(job_id=f"r{number}"))
        service.reject(f"r{number}")
        clock.now += 10


# --- 驗章：不對的都不跳視窗 ---


@pytest.mark.parametrize(
    "job",
    [
        _open_job(decision=None),
        _open_job(decision=_decision(WANTED, job_id="o1", key=OTHER_KEY)),
        _open_job(decision=_decision(WANTED, job_id="other")),
        _open_job(decision=_decision("D:\\Else", job_id="o1")),
        _open_job(
            decision=_decision(WANTED, job_id="o1", shown=text_fingerprint("D:\\Else")),
        ),
        _open_job(decision=_decision(WANTED, job_id="o1", decided_by="yolo")),
        _open_job(args={"path": WANTED, "writable": True}),
        _open_job(args={}),
        _open_job(args={"path": 7}),
        _open_job("D:\\" + "a" * 1022),
        _open_job(""),
    ],
    ids=[
        "no-decision",
        "other-key",
        "other-job",
        "other-path-fingerprint",
        "digest-not-shown",
        "mode-decided-by",
        "extra-arg",
        "no-path",
        "path-not-text",
        "path-too-long",
        "empty-path",
    ],
)
def test_a_bad_stamp_never_opens_a_window(service, client, gate, job: Job) -> None:
    _poll(service, client, job)
    assert _codes(client) == ["bad_signature"]
    assert gate.current is None


def test_a_mode_decision_stamp_is_not_an_open_stamp(service, client, gate) -> None:
    nonce = next(_NONCES)
    digest = text_fingerprint(WANTED)
    signature = sign_decision(
        KEY, job_id="o1", nonce=nonce, digest=digest, shown=digest, decided_by="yolo"
    )
    decision = Decision(nonce, digest, digest, DECIDED_BY_OPEN_REQUEST, signature)
    _poll(service, client, _open_job(decision=decision))
    assert _codes(client) == ["bad_signature"]
    assert gate.current is None


def test_an_open_stamp_is_not_a_mode_decision(service, client, gate, fs) -> None:
    fs.add_file("D:\\Open\\a.txt")
    nonce = next(_NONCES)
    digest = text_fingerprint("x")
    signature = sign_open_request(KEY, job_id="m1", nonce=nonce, path="x")
    job = Job(
        id="m1",
        action="propose",
        args={"items": [{"op": "delete", "path": "Open/a.txt"}], "reason": "r"},
        requester="soul",
        decide_within=540.0,
        decision=Decision(nonce, digest, digest, DECIDED_BY_OPEN_REQUEST, signature),
    )
    _poll(service, client, job)
    # 不是照模式決定，所以改跳視窗、什麼都沒動
    assert gate.current is not None and gate.current.job_id == "m1"
    assert fs.exists("D:\\Open\\a.txt")


def test_without_a_key_it_asks_for_a_new_pairing(service, client, gate, key) -> None:
    key.value = None
    _poll(service, client, _open_job())
    assert _codes(client) == ["needs_new_pairing"]
    assert gate.current is None


def test_a_used_number_is_refused(service, client, gate) -> None:
    nonce = next(_NONCES)
    _poll(service, client, _open_job(decision=_decision(WANTED, job_id="o1", nonce=nonce)))
    service.reject("o1")
    client.results.clear()
    _poll(
        service,
        client,
        _open_job(job_id="o2", decision=_decision(WANTED, job_id="o2", nonce=nonce)),
    )
    assert _codes(client) == ["bad_signature"]


def test_bad_stamps_do_not_count_toward_the_pause(service, client, gate) -> None:
    for number in range(OPEN_REFUSALS_BEFORE_PAUSE + 1):
        _poll(service, client, _open_job(job_id=f"b{number}", decision=None))
    client.results.clear()
    _poll(service, client, _open_job(job_id="ok"))
    assert client.results == []
    assert gate.current is not None


# --- 先佔住視窗，再檢查路徑 ---


def test_the_window_is_held_before_the_path_is_checked(service, client, gate, book) -> None:
    book.seen_pending.clear()  # 佈置時開放 D:\Open 也會檢查一次
    _poll(service, client, _open_job())
    assert len(book.seen_pending) == 1
    held = book.seen_pending[0]
    assert isinstance(held, OpenRequest) and held.state == "checking"


@pytest.mark.parametrize("path", [WANTED, "D:\\Missing"])
def test_a_busy_window_answers_the_same_for_any_path(service, client, gate, fs, path) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _poll(
        service,
        client,
        Job(
            id="w1",
            action="propose",
            args={"items": [{"op": "delete", "path": "Open/a.txt"}], "reason": "r"},
            requester="soul",
            decide_within=540.0,
        ),
    )
    _poll(service, client, _open_job(path))
    assert _codes(client) == ["approval_busy"]


@pytest.mark.parametrize("path", [WANTED, "D:\\Missing"])
def test_a_cooldown_answers_the_same_for_any_path(service, client, gate, path) -> None:
    _poll(service, client, _open_job(job_id="first"))
    service.reject("first")
    client.results.clear()
    _poll(service, client, _open_job(path, job_id="second"))
    assert _codes(client) == ["approval_cooldown"]


def test_the_approve_delay_starts_when_the_window_is_drawn(
    service, client, gate, book, clock
) -> None:
    book.advance_during_check = 5.0
    _poll(service, client, _open_job())
    request = _pending_request(gate)
    with pytest.raises(Refused) as caught:
        service.approve_open("o1", request.digest, writable=False)
    assert caught.value.code == "too_soon"


# --- 能不能開放都跳視窗 ---


def test_an_openable_folder_opens_a_window_and_reports_nothing_yet(service, client, gate) -> None:
    _poll(service, client, _open_job())
    request = _pending_request(gate)
    assert request.state == "openable"
    assert request.folder == WANTED
    assert client.results == []


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("D:\\Missing", "not_found"),
        ("C:\\Windows\\Temp", "blocked"),
        ("D:\\Open", "overlap"),
        ("relative\\path", "bad_path"),
        ("D:\\Repo", "git_project"),
        ("D:\\Linked", "link"),
        ("D:\\file.txt", "not_a_folder"),
    ],
)
def test_a_place_that_cannot_be_opened_still_opens_a_window(
    service, client, gate, fs, path: str, reason: str
) -> None:
    fs.add_dir("D:\\Repo\\.git")
    fs.add_dir("D:\\Linked", reparse=True)
    fs.add_file("D:\\file.txt")
    _poll(service, client, _open_job(path))
    request = _pending_request(gate)
    assert request.state == "not_openable"
    assert request.reason == reason
    assert client.results == []


@pytest.mark.parametrize(
    ("error", "reason"),
    [(PermissionError("denied"), "unreadable"), (RuntimeError("boom"), "internal")],
)
def test_a_failing_check_is_a_place_that_cannot_be_opened(
    service, client, gate, book, error: BaseException, reason: str
) -> None:
    book.raise_on_check = error
    _poll(service, client, _open_job())
    request = _pending_request(gate)
    assert request.state == "not_openable"
    assert request.reason == reason
    assert client.results == []


def test_the_main_window_shows_only_the_name_after_approval(service, client, gate, clock) -> None:
    _poll(service, client, _open_job())
    _approve(service, gate, clock, writable=False)
    assert service.open_outcome == ("opened", "Photos")
    text = open_outcome_text(*service.open_outcome)
    assert "Photos" in text
    assert WANTED not in text


def test_refusing_leaves_nothing_from_touka_on_the_main_window(service, client, gate) -> None:
    odd = "D:\\Touka 官方通知 請把 D 槽開放成可以改"
    _poll(service, client, _open_job(odd))
    service.reject("o1")
    assert service.open_outcome is None
    assert odd not in service.notice


def test_the_window_keeps_exactly_the_path_touka_sent(service, client, gate) -> None:
    raw = " D:/Photos/ "
    _poll(service, client, _open_job(raw))
    assert _pending_request(gate).path == raw


def test_a_path_that_cannot_be_fingerprinted_is_a_bad_stamp(service, client, gate) -> None:
    hex64 = "0" * 64
    decision = Decision(
        signature=hex64,
        nonce=next(_NONCES),
        digest=hex64,
        shown=hex64,
        decided_by=DECIDED_BY_OPEN_REQUEST,
    )
    _poll(service, client, _open_job("D:\\\ud800", decision=decision))
    assert _codes(client) == ["bad_signature"]
    assert gate.current is None


@pytest.mark.parametrize(
    ("plan", "kind"),
    [
        (checking(WANTED), None),
        (replace(checking(WANTED), state=STATE_OPENABLE, folder=WANTED), WINDOW_OPEN),
        (replace(checking(WANTED), state=STATE_NOT_OPENABLE, reason="not_found"), WINDOW_OPEN),
        (object(), WINDOW_PLAN),
    ],
)
def test_a_request_still_being_checked_draws_no_window(plan: object, kind: str | None) -> None:
    assert approval_window_kind(plan) == kind


# --- 檢查的時間差 ---


def test_the_path_check_does_not_hold_a_job_slot(parts, client, gate) -> None:
    waiting: list[Callable[[], None]] = []
    service = parts.service(waiting.append)
    _poll(service, client, _open_job())
    waiting.pop(0)()  # 開放請求本身：佔住視窗，把路徑檢查交給另一條執行緒
    assert len(waiting) == 1  # 路徑檢查還沒做
    for number in range(MAX_JOB_THREADS):
        listing = Job(
            id=f"l{number}",
            action="list",
            args={},
            requester="owner",
            decide_within=OPEN_DECISION_SECONDS,
            decision=None,
        )
        _poll(service, client, listing)
    assert "helper_busy" not in _codes(client)
    waiting.pop(0)()
    assert _pending_request(gate).state == STATE_OPENABLE


def test_a_slow_disk_does_not_hold_the_list_lock(fs, tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()
    slow = {"on": False}

    def drive_type(_anchor: str) -> str:
        if slow["on"]:
            entered.set()
            release.wait(5)
        return "fixed"

    fs.add_dir(WANTED)
    book = GrantBook(tmp_path / "g.json", fs=fs, env=ENV, drive_type=drive_type)
    slow["on"] = True
    checker = threading.Thread(target=book.check_can_add, args=(WANTED,))
    checker.start()
    try:
        assert entered.wait(5)
        other = threading.Thread(target=lambda: (book.set_mount_roots([]), book.grants()))
        other.start()
        other.join(2)
        assert not other.is_alive()  # 輪詢與主視窗不用等磁碟
    finally:
        release.set()
        checker.join(5)


def test_when_the_check_cannot_start_the_window_still_opens(parts, client, gate) -> None:
    started: list[int] = []

    def spawn(work: Callable[[], None]) -> None:
        if started:
            raise RuntimeError
        started.append(1)
        work()

    service = parts.service(spawn)
    _poll(service, client, _open_job())
    request = _pending_request(gate)
    assert request.state == STATE_NOT_OPENABLE
    assert request.reason == "internal"
    assert client.results == []


# --- 回報 ---


@pytest.mark.parametrize("writable", [False, True])
def test_approving_adds_the_folder_with_the_chosen_permission(
    service, client, gate, book, clock, writable: bool
) -> None:
    _poll(service, client, _open_job())
    _approve(service, gate, clock, writable=writable)
    payload = client.results[-1][1]
    assert payload["status"] == "ok"
    assert payload["data"] == {"name": "Photos", "writable": writable}
    grant = book.find("Photos")
    assert grant is not None and grant.writable is writable
    assert client.confirmed == []


def test_the_permission_starts_as_read_only() -> None:
    assert OPEN_DEFAULT_WRITABLE is False


def test_a_folder_swapped_for_a_link_before_approval_is_not_added(
    service, client, gate, book, fs, clock
) -> None:
    _poll(service, client, _open_job())
    fs.nodes[WANTED.casefold()].reparse = True
    _approve(service, gate, clock, writable=False)
    assert _codes(client) == ["not_openable"]
    assert book.find("Photos") is None


def test_a_list_that_cannot_be_saved_reports_io_error_and_counts(
    service, client, gate, book, clock
) -> None:
    book.raise_on_add = OSError("disk full")
    for number in range(OPEN_REFUSALS_BEFORE_PAUSE):
        _poll(service, client, _open_job(job_id=f"a{number}"))
        _approve(service, gate, clock, writable=False)
    assert _codes(client) == ["io_error"] * OPEN_REFUSALS_BEFORE_PAUSE
    assert service.open_outcome == ("io_error", None)
    assert service.open_paused_until is not None


def test_approving_when_no_thread_can_start_is_refused_and_counts(
    parts, client, gate, book, clock
) -> None:
    allowed = {"left": 0}

    def spawn(work: Callable[[], None]) -> None:
        if allowed["left"] == 0:
            raise RuntimeError
        allowed["left"] -= 1
        work()

    service = parts.service(spawn)
    for number in range(OPEN_REFUSALS_BEFORE_PAUSE):
        allowed["left"] = 2  # 輪詢到的工作、路徑檢查；核准後要開的那一條開不了
        _poll(service, client, _open_job(job_id=f"t{number}"))
        with pytest.raises(Refused) as caught:
            _approve(service, gate, clock, writable=False)
        assert caught.value.code == "open_failed"
        assert gate.current is None
    assert book.find("Photos") is None
    assert client.results == []
    assert service.open_paused_until is not None


@pytest.mark.parametrize("path", [WANTED, "D:\\Missing"])
def test_rejecting_either_window_reports_the_same_code(service, client, gate, path) -> None:
    _poll(service, client, _open_job(path))
    service.reject("o1")
    assert _codes(client) == ["not_opened"]
    assert gate.current is None


@pytest.mark.parametrize("path", [WANTED, "D:\\Missing"])
def test_time_running_out_reports_the_same_code(service, client, gate, clock, path) -> None:
    _poll(service, client, _open_job(path))
    clock.now += OPEN_DECISION_SECONDS + 1
    service.tick()
    assert _codes(client) == ["no_decision"]


def test_touka_cannot_shorten_the_window(service, client, gate, clock) -> None:
    _poll(service, client, _open_job(decide_within=0.001))
    pending = gate.current
    assert pending is not None
    assert pending.deadline - pending.offered_at == pytest.approx(OPEN_DECISION_SECONDS, abs=1)
    clock.now += 60
    service.tick()
    assert gate.current is not None


def test_a_cancel_in_the_same_reply_is_ignored(service, client, gate) -> None:
    _poll(service, client, _open_job(), cancelled=("o1",))
    assert gate.current is not None and gate.current.job_id == "o1"


@pytest.mark.parametrize("path", [WANTED, "D:\\Missing"])
def test_a_cancel_after_the_window_is_ignored(service, client, gate, path) -> None:
    _poll(service, client, _open_job(path))
    _poll(service, client, cancelled=("o1",))
    assert gate.current is not None and gate.current.job_id == "o1"
    assert client.results == []


def test_a_cancel_still_closes_a_list_window(service, client, gate, fs) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _poll(
        service,
        client,
        Job(
            id="w1",
            action="propose",
            args={"items": [{"op": "delete", "path": "Open/a.txt"}], "reason": "r"},
            requester="soul",
            decide_within=540.0,
        ),
    )
    _poll(service, client, cancelled=("w1",))
    assert gate.current is None


# --- 換配對或小幫手被看見：收掉、不回報、計數 ---


@pytest.mark.parametrize("path", [WANTED, "D:\\Missing"])
def test_being_exposed_closes_the_window_without_a_report(service, client, gate, path) -> None:
    _poll(service, client, _open_job(path))
    _poll(service, client, mounts=(HELPER_DIR,))
    assert gate.current is None
    assert client.results == []


def test_closing_by_exposure_or_lost_pairing_counts(service, client, gate, clock) -> None:
    for number in range(OPEN_REFUSALS_BEFORE_PAUSE):
        _poll(service, client, _open_job(job_id=f"x{number}"))
        if number % 2:
            client.responses.append(AuthLost())
            service.poll_once()
        else:
            _poll(service, client, mounts=(HELPER_DIR,))
        clock.now += 10
    _poll(service, client, _open_job(job_id="after"))
    assert _codes(client)[-1] == "open_paused"


# --- 停收 ---


def test_three_refusals_pause_open_requests(service, client, gate, clock) -> None:
    _refuse_three_times(service, client, gate, clock)
    client.results.clear()
    _poll(service, client, _open_job(job_id="later"))
    assert _codes(client) == ["open_paused"]
    assert gate.current is None
    assert service.open_paused_until is not None


def test_the_pause_ends_by_itself(service, client, gate, clock) -> None:
    _refuse_three_times(service, client, gate, clock)
    clock.now += OPEN_PAUSE_SECONDS + 1
    client.results.clear()
    _poll(service, client, _open_job(job_id="later"))
    assert client.results == []
    assert gate.current is not None


def test_the_person_can_resume_now(service, client, gate, clock) -> None:
    _refuse_three_times(service, client, gate, clock)
    service.resume_open_requests()
    assert service.open_paused_until is None
    client.results.clear()
    _poll(service, client, _open_job(job_id="later"))
    assert gate.current is not None


def test_an_approval_resets_the_count(service, client, gate, fs, clock) -> None:
    for number in range(OPEN_REFUSALS_BEFORE_PAUSE - 1):
        _poll(service, client, _open_job(job_id=f"r{number}"))
        service.reject(f"r{number}")
        clock.now += 10
    _poll(service, client, _open_job(job_id="yes"))
    _approve(service, gate, clock, writable=False)
    clock.now += 10
    fs.add_dir("D:\\More")
    for number in range(OPEN_REFUSALS_BEFORE_PAUSE - 1):
        _poll(service, client, _open_job("D:\\More", job_id=f"s{number}"))
        service.reject(f"s{number}")
        clock.now += 10
    client.results.clear()
    _poll(service, client, _open_job("D:\\More", job_id="still"))
    assert client.results == []


def test_timeouts_count_toward_the_pause(service, client, gate, clock) -> None:
    for number in range(OPEN_REFUSALS_BEFORE_PAUSE):
        _poll(service, client, _open_job(job_id=f"t{number}"))
        clock.now += OPEN_DECISION_SECONDS + 1
        service.tick()
        clock.now += 10
    client.results.clear()
    _poll(service, client, _open_job(job_id="later"))
    assert _codes(client) == ["open_paused"]


# --- 視窗種類對不上 ---


def test_approving_a_list_as_a_folder_is_refused(service, client, gate, fs, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _poll(
        service,
        client,
        Job(
            id="w1",
            action="propose",
            args={"items": [{"op": "delete", "path": "Open/a.txt"}], "reason": "r"},
            requester="soul",
            decide_within=540.0,
        ),
    )
    pending = gate.current
    assert pending is not None
    clock.now += APPROVE_DELAY_SECONDS
    with pytest.raises(Refused) as caught:
        service.approve_open("w1", pending.plan.digest, writable=True)
    assert caught.value.code == "wrong_window"
    assert gate.current is pending


def test_approving_a_folder_as_a_list_is_refused(service, client, gate, clock, book) -> None:
    _poll(service, client, _open_job())
    request = _pending_request(gate)
    clock.now += APPROVE_DELAY_SECONDS
    with pytest.raises(Refused) as caught:
        service.approve("o1", request.digest)
    assert caught.value.code == "wrong_window"
    assert gate.current is not None
    assert book.find("Photos") is None
    assert client.confirmed == []


def test_a_place_that_cannot_be_opened_cannot_be_approved(service, client, gate, clock) -> None:
    _poll(service, client, _open_job("D:\\Missing"))
    request = _pending_request(gate)
    clock.now += APPROVE_DELAY_SECONDS
    with pytest.raises(Refused) as caught:
        service.approve_open("o1", request.digest, writable=False)
    assert caught.value.code == "wrong_window"
    assert gate.current is not None
