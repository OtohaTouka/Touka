"""小幫手主流程（ADR-0311）：工作分派、按核准才 confirm、confirm 成功才動手、取消、逾時。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from touka_local.approval import APPROVE_DELAY_SECONDS, ApprovalGate
from touka_local.connection import AuthLost, ConnectionLost, ProtocolMismatch
from touka_local.errors import Refused
from touka_local.grants import Environment, GrantBook
from touka_local.operations import Operations
from touka_local.protocol import Job, PollResponse, ProtocolError
from touka_local.service import MAX_JOB_THREADS, REMEMBERED_CANCELS, HelperService

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _Client:
    responses: list[object] = field(default_factory=list)
    confirm_answer: bool = True
    deliver_answer: bool = True
    confirmed: list[str] = field(default_factory=list)
    results: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def poll(self) -> PollResponse:
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]

    def confirm(self, job_id: str) -> bool:
        self.confirmed.append(job_id)
        self.events.append("confirm")
        return self.confirm_answer

    def result(self, job_id: str, payload: dict[str, Any]) -> bool:
        self.results.append((job_id, payload))
        return self.deliver_answer


def _job(action: str, args: dict[str, Any], job_id: str = "j1") -> Job:
    return Job(id=job_id, action=action, args=args, requester="soul", decide_within=540.0)


def _response(
    job: Job | None = None, cancelled: tuple[str, ...] = (), mounts: tuple[str, ...] = ()
) -> PollResponse:
    return PollResponse(job=job, cancelled=cancelled, host_mount_roots=mounts)


@pytest.fixture
def client() -> _Client:
    return _Client()


@pytest.fixture
def book(fs, tmp_path: Path) -> GrantBook:
    env = Environment(
        user_profile="C:\\Users\\Me",
        windows_dir="C:\\Windows",
        program_files=("C:\\Program Files",),
        program_data="C:\\ProgramData",
        app_data="C:\\Users\\Me\\AppData\\Roaming",
        local_app_data="C:\\Users\\Me\\AppData\\Local",
        helper_dir="C:\\Users\\Me\\AppData\\Local\\ToukaLocal",
        python_dir="C:\\Python",
        touka_folder="D:\\Touka",
    )
    fs.add_dir("D:\\Open")
    book = GrantBook(tmp_path / "g.json", fs=fs, env=env, drive_type=lambda _a: "fixed")
    book.add("D:\\Open", writable=True)
    return book


@pytest.fixture
def gate(clock) -> ApprovalGate:
    return ApprovalGate(clock=clock)


@pytest.fixture
def service(
    client: _Client, fs, book: GrantBook, trash, clock, gate: ApprovalGate, tmp_path: Path
) -> HelperService:
    operations = Operations(
        fs=fs, grants=book, trash=trash, clock=clock, journal_dir=tmp_path / "j"
    )
    return HelperService(
        client=client,
        operations=operations,
        gate=gate,
        grants=book,
        clock=clock,
        spawn=lambda work: work(),
    )


def _run(service: HelperService, client: _Client, response: object) -> None:
    client.responses.append(response)
    service.poll_once()


def _propose(
    service: HelperService, client: _Client, items: list[dict[str, str]], job_id: str = "j1"
) -> None:
    _run(service, client, _response(_job("propose", {"items": items, "reason": "整理"}, job_id)))


def _approve(service: HelperService, gate: ApprovalGate, clock, job_id: str = "j1") -> None:
    pending = gate.current
    assert pending is not None
    clock.now += APPROVE_DELAY_SECONDS
    service.approve(job_id, pending.plan.digest)


def test_read_job_returns_data(service, client, fs) -> None:
    fs.add_file("D:\\Open\\a.txt", b"hi")
    _run(service, client, _response(_job("read", {"path": "Open/a.txt"})))
    job_id, payload = client.results[0]
    assert job_id == "j1"
    assert payload["status"] == "ok"
    assert payload["data"]["text"] == "hi"
    assert service.state == "online"


def test_create_job(service, client, fs) -> None:
    _run(service, client, _response(_job("create_file", {"path": "Open/a.txt", "content": "x"})))
    assert client.results[0][1]["status"] == "ok"
    assert fs.exists("D:\\Open\\a.txt")


def test_refused_job_reports_code_without_absolute_paths(service, client) -> None:
    _run(service, client, _response(_job("read", {"path": "Open/none.txt"})))
    payload = client.results[0][1]
    assert payload["status"] == "refused"
    assert payload["code"] == "not_found"
    assert "Open/none.txt" in payload["message"]
    assert "D:\\" not in payload["message"]


def test_os_error_is_reported_without_the_path(service, client, fs, monkeypatch) -> None:
    fs.add_file("D:\\Open\\a.txt")

    def boom(_path: str, _limit: int) -> bytes:
        raise PermissionError(13, "denied", "D:\\Open\\a.txt")

    monkeypatch.setattr(fs, "read", boom)
    _run(service, client, _response(_job("read", {"path": "Open/a.txt"})))
    payload = client.results[0][1]
    assert payload["status"] == "error"
    assert payload["code"] == "io_error"
    assert "D:\\" not in payload["message"]


def test_unexpected_error_is_reported_and_loop_survives(service, client, monkeypatch) -> None:
    def boom(_args: dict[str, Any]) -> dict[str, Any]:
        raise KeyError("D:\\secret")

    monkeypatch.setattr(service._operations, "list", boom)
    _run(service, client, _response(_job("list", {})))
    payload = client.results[0][1]
    assert (payload["status"], payload["code"]) == ("error", "internal")
    assert "secret" not in (payload["message"] or "")


def test_mount_roots_reach_the_grant_book(service, client, book: GrantBook) -> None:
    _run(service, client, _response(mounts=("D:/Open",)))
    assert book.paused_names() == ["Open"]


def test_propose_waits_for_a_person(service, client, gate, fs, trash) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    assert gate.current is not None
    assert client.results == []
    assert client.confirmed == []
    assert trash.sent == []


def test_refused_propose_never_reaches_the_person(service, client, gate) -> None:
    _propose(service, client, [{"op": "delete", "path": "Open/none.txt"}])
    assert gate.current is None
    assert client.results[0][1]["code"] == "not_found"


def test_second_propose_is_refused(service, client, gate, fs) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}], "j1")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}], "j2")
    assert gate.current.job_id == "j1"
    assert client.results == [("j2", client.results[0][1])]
    assert client.results[0][1]["code"] == "approval_busy"


def test_approve_confirms_before_touching_anything(service, client, gate, fs, trash, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    original_send = trash.send

    def send(path: str) -> str:
        client.events.append("trash")
        return original_send(path)

    trash.send = send
    _approve(service, gate, clock)
    assert client.events == ["confirm", "trash"]
    job_id, payload = client.results[0]
    assert job_id == "j1"
    assert payload["status"] == "ok"
    assert payload["data"]["items"] == [{"path": "Open/a.txt", "status": "done"}]
    assert service.last_report == payload["data"]


def test_failed_confirm_touches_nothing(service, client, gate, fs, trash, clock) -> None:
    client.confirm_answer = False
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    _approve(service, gate, clock)
    assert trash.sent == []
    assert client.results == []
    assert fs.exists("D:\\Open\\a.txt")
    assert service.notice == "not_confirmed"


def test_reject_reports_and_touches_nothing(service, client, gate, fs, trash) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    service.reject("j1")
    assert client.confirmed == []
    assert trash.sent == []
    payload = client.results[0][1]
    assert (payload["status"], payload["code"]) == ("refused", "user_rejected")
    assert "Nothing was changed" in payload["message"]


def test_tick_rejects_after_the_limit(service, client, gate, fs, trash, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    service.tick()
    assert client.results == []
    clock.now += 540
    service.tick()
    assert gate.current is None
    assert client.results[0][1]["code"] == "no_decision"
    assert client.confirmed == []
    assert trash.sent == []


def test_cancel_closes_the_window(service, client, gate, fs, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    _run(service, client, _response(cancelled=("j1",)))
    assert gate.current is None
    assert client.results == []
    assert client.confirmed == []


def test_cancel_while_running_finishes_current_item_only(
    service, client, gate, fs, trash, clock
) -> None:
    for name in "abc":
        fs.add_file(f"D:\\Open\\{name}.txt")
    _propose(service, client, [{"op": "delete", "path": f"Open/{n}.txt"} for n in "abc"])
    original_send = trash.send

    def send(path: str) -> str:
        outcome = original_send(path)
        client.responses.append(_response(cancelled=("j1",)))
        service.poll_once()
        return outcome

    trash.send = send
    _approve(service, gate, clock)
    assert [i["status"] for i in client.results[0][1]["data"]["items"]] == [
        "done",
        "not_done",
        "not_done",
    ]


def test_stop_button_during_run(service, client, gate, fs, trash, clock) -> None:
    for name in "ab":
        fs.add_file(f"D:\\Open\\{name}.txt")
    _propose(service, client, [{"op": "delete", "path": f"Open/{n}.txt"} for n in "ab"])
    original_send = trash.send

    def send(path: str) -> str:
        outcome = original_send(path)
        service.stop_running()
        return outcome

    trash.send = send
    _approve(service, gate, clock)
    assert [i["status"] for i in client.results[0][1]["data"]["items"]] == ["done", "not_done"]


def test_result_not_delivered_is_visible(service, client, gate, fs, clock) -> None:
    client.deliver_answer = False
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    _approve(service, gate, clock)
    assert service.notice == "result_not_delivered"


def test_no_new_window_or_undo_while_running(service, client, gate, fs, trash, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    fs.add_file("D:\\Open\\b.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    original_send = trash.send
    seen: dict[str, Any] = {}

    def send(path: str) -> str:
        client.responses.append(
            _response(
                _job(
                    "propose",
                    {"items": [{"op": "delete", "path": "Open/b.txt"}], "reason": "x"},
                    "j2",
                )
            )
        )
        service.poll_once()
        with pytest.raises(Refused) as caught:
            service.undo("0" * 32)
        seen["undo"] = caught.value.code
        return original_send(path)

    trash.send = send
    _approve(service, gate, clock)
    assert seen["undo"] == "busy"
    assert client.results[0] == ("j2", client.results[0][1])
    assert client.results[0][1]["code"] == "approval_busy"


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (AuthLost(), "unpaired"),
        (ProtocolMismatch(), "upgrade_needed"),
        (ProtocolError(), "upgrade_needed"),
        (ConnectionLost(), "offline"),
    ],
)
def test_poll_failures_set_state(service, client, error: Exception, state: str) -> None:
    _run(service, client, error)
    assert service.state == state


def test_replacing_the_client_resets_state(service, client) -> None:
    _run(service, client, AuthLost())
    replacement = _Client(responses=[_response()])
    service.replace_client(replacement)
    assert service.state == "starting"
    service.poll_once()
    assert service.state == "online"


def test_without_a_client_nothing_polls(service) -> None:
    service.replace_client(None)
    assert service.state == "unpaired"
    assert service.poll_once() is False


# --- 出錯時要照實說 ---


def test_a_list_that_failed_is_not_called_finished(
    service, client, gate, fs, clock, monkeypatch
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])

    def boom(_plan: Any, _limits: Any) -> dict[str, Any]:
        raise OSError(28, "disk full")

    monkeypatch.setattr(service._operations, "apply", boom)
    _approve(service, gate, clock)
    assert service.notice == "apply_failed"
    assert service.last_report is None
    assert client.results[0][1]["code"] == "io_error"
    assert service.running is False


def test_an_unexpected_failure_is_reported_and_clears_the_batch(
    service, client, gate, fs, clock, monkeypatch
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])

    def boom(_plan: Any, _limits: Any) -> dict[str, Any]:
        raise KeyError("D:\\secret")

    monkeypatch.setattr(service._operations, "apply", boom)
    _approve(service, gate, clock)
    payload = client.results[0][1]
    assert (payload["status"], payload["code"]) == ("error", "internal")
    assert "secret" not in payload["message"]
    assert service.notice == "apply_failed"
    assert service.running is False


def test_a_confirm_that_blows_up_touches_nothing(
    service, client, gate, fs, trash, clock, monkeypatch
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])

    def boom(_job_id: str) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "confirm", boom)
    _approve(service, gate, clock)
    assert trash.sent == []
    assert service.notice == "apply_failed"
    assert service.running is False


def test_an_old_report_is_cleared_when_a_new_list_starts(service, client, gate, fs, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    fs.add_file("D:\\Open\\b.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    _approve(service, gate, clock)
    assert service.last_report is not None
    client.confirm_answer = False
    _propose(service, client, [{"op": "delete", "path": "Open/b.txt"}], "j2")
    _approve(service, gate, clock, "j2")
    assert service.last_report is None


def test_polling_survives_an_unexpected_error(service, client) -> None:
    _run(service, client, RuntimeError("boom"))
    assert service.state == "offline"
    _run(service, client, _response())
    assert service.state == "online"


def test_losing_the_pairing_closes_the_window(service, client, gate, fs) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    _run(service, client, AuthLost())
    assert gate.current is None
    assert service.state == "unpaired"


def test_losing_the_pairing_stops_a_running_batch(service, client, gate, fs, trash, clock) -> None:
    for name in "ab":
        fs.add_file(f"D:\\Open\\{name}.txt")
    _propose(service, client, [{"op": "delete", "path": f"Open/{n}.txt"} for n in "ab"])
    original_send = trash.send

    def send(path: str) -> str:
        outcome = original_send(path)
        _run(service, client, AuthLost())
        return outcome

    trash.send = send
    _approve(service, gate, clock)
    assert [i["status"] for i in client.results[0][1]["data"]["items"]] == ["done", "not_done"]


def test_job_threads_are_capped(client, fs, book, trash, clock, gate, tmp_path) -> None:
    started: list[Any] = []
    operations = Operations(
        fs=fs, grants=book, trash=trash, clock=clock, journal_dir=tmp_path / "capped"
    )
    service = HelperService(
        client=client,
        operations=operations,
        gate=gate,
        grants=book,
        clock=clock,
        spawn=started.append,
    )
    for number in range(MAX_JOB_THREADS):
        _run(service, client, _response(_job("list", {}, f"j{number}")))
    assert len(started) == MAX_JOB_THREADS
    _run(service, client, _response(_job("list", {}, "extra")))
    assert len(started) == MAX_JOB_THREADS
    assert client.results[-1][0] == "extra"
    assert client.results[-1][1]["code"] == "helper_busy"
    started.pop(0)()
    _run(service, client, _response(_job("list", {}, "next")))
    assert len(started) == MAX_JOB_THREADS


# --- 關視窗 ---


def test_closing_is_refused_while_a_list_is_waiting(service, client, gate, fs) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    assert service.request_close() == "blocked"
    assert gate.current is not None


def test_closing_during_a_batch_stops_it_first(service, client, gate, fs, trash, clock) -> None:
    for name in "ab":
        fs.add_file(f"D:\\Open\\{name}.txt")
    _propose(service, client, [{"op": "delete", "path": f"Open/{n}.txt"} for n in "ab"])
    original_send = trash.send
    answers: list[str] = []

    def send(path: str) -> str:
        outcome = original_send(path)
        answers.append(service.request_close())
        return outcome

    trash.send = send
    _approve(service, gate, clock)
    assert answers == ["wait"]
    assert [i["status"] for i in client.results[0][1]["data"]["items"]] == ["done", "not_done"]


def test_closing_when_idle_is_immediate(service) -> None:
    assert service.request_close() == "now"


# --- 沒有全部做完，不能說做完了 ---


def test_a_fully_done_list_is_finished(service, client, gate, fs, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    _approve(service, gate, clock)
    assert service.notice == "finished"


def test_a_stopped_list_is_not_called_finished(service, client, gate, fs, trash, clock) -> None:
    for name in "ab":
        fs.add_file(f"D:\\Open\\{name}.txt")
    _propose(service, client, [{"op": "delete", "path": f"Open/{n}.txt"} for n in "ab"])
    original_send = trash.send

    def send(path: str) -> str:
        outcome = original_send(path)
        service.stop_running()
        return outcome

    trash.send = send
    _approve(service, gate, clock)
    assert service.notice == "partly_done"


def test_a_list_with_an_incomplete_record_is_not_called_finished(
    service, client, gate, fs, clock, monkeypatch
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])

    def incomplete(_plan: Any, _limits: Any) -> dict[str, Any]:
        return {
            "batch": "b",
            "items": [{"path": "Open/a.txt", "status": "done"}],
            "journal_incomplete": True,
        }

    monkeypatch.setattr(service._operations, "apply", incomplete)
    _approve(service, gate, clock)
    assert service.notice == "partly_done"


# --- 開不了執行緒 ---


def _service_with(spawn: Any, client, fs, book, trash, clock, gate, tmp_path) -> HelperService:
    operations = Operations(
        fs=fs, grants=book, trash=trash, clock=clock, journal_dir=tmp_path / "threads"
    )
    return HelperService(
        client=client, operations=operations, gate=gate, grants=book, clock=clock, spawn=spawn
    )


def test_a_job_slot_comes_back_when_a_thread_cannot_start(
    client, fs, book, trash, clock, gate, tmp_path
) -> None:
    def no_threads(_work: Any) -> None:
        raise RuntimeError

    service = _service_with(no_threads, client, fs, book, trash, clock, gate, tmp_path)
    for number in range(MAX_JOB_THREADS + 1):
        _run(service, client, _response(_job("list", {}, f"j{number}")))
    assert client.results == []


def test_a_batch_that_cannot_start_does_not_jam_the_helper(
    client, fs, book, trash, clock, gate, tmp_path
) -> None:
    started: list[bool] = []

    def spawn(work: Any) -> None:
        if started:
            raise RuntimeError
        started.append(True)
        work()

    service = _service_with(spawn, client, fs, book, trash, clock, gate, tmp_path)
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}])
    pending = gate.current
    assert pending is not None
    clock.now += APPROVE_DELAY_SECONDS
    with pytest.raises(Refused) as caught:
        service.approve("j1", pending.plan.digest)
    assert caught.value.code == "busy"
    assert service.running is False


def test_cooldown_message_does_not_say_the_user_rejected(service, client, gate, fs, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}], "j1")
    _run(service, client, _response(cancelled=("j1",)))
    _propose(service, client, [{"op": "delete", "path": "Open/a.txt"}], "j2")
    assert client.results[-1][1]["code"] == "approval_cooldown"
    assert "rejected" not in client.results[-1][1]["message"]


EXPOSED_MOUNTS = ("C:\\Users\\Me",)


def _propose_args() -> dict[str, Any]:
    return {"items": [{"op": "delete", "path": "Open/a.txt"}], "reason": "r"}


def test_a_job_cancelled_before_it_starts_never_reaches_the_person(
    service: HelperService, client: _Client, fs, gate: ApprovalGate
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    client.responses = [_response(cancelled=("j1",)), _response(_job("propose", _propose_args()))]
    service.poll_once()
    service.poll_once()
    assert gate.current is None


def test_a_job_cancelled_while_it_is_prepared_never_reaches_the_person(
    service: HelperService, client: _Client, fs, gate: ApprovalGate, monkeypatch
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    operations = service._operations
    real = operations.plan

    def plan_then_cancel(args: dict[str, Any]) -> Any:
        result = real(args)
        service._cancel("j1")
        return result

    monkeypatch.setattr(operations, "plan", plan_then_cancel)
    client.responses = [_response(_job("propose", _propose_args()))]
    service.poll_once()
    assert gate.current is None


def test_only_recent_cancels_are_remembered(
    service: HelperService, client: _Client, fs, gate: ApprovalGate
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    old = tuple(f"old{n}" for n in range(REMEMBERED_CANCELS))
    client.responses = [
        _response(cancelled=("j1", *old)),
        _response(_job("propose", _propose_args())),
    ]
    service.poll_once()
    service.poll_once()
    assert gate.current is not None


def test_helper_stops_serving_when_its_own_files_are_reachable(
    service: HelperService, client: _Client, fs, gate: ApprovalGate
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    client.responses = [_response(_job("list", {"path": "Open"}), mounts=EXPOSED_MOUNTS)]
    assert service.poll_once() is True
    assert service.state == "exposed"
    assert client.results[0][1]["status"] == "refused"
    assert client.results[0][1]["code"] == "helper_exposed"


def test_exposed_helper_drops_what_is_waiting(
    service: HelperService, client: _Client, fs, gate: ApprovalGate
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    client.responses = [
        _response(_job("propose", _propose_args())),
        _response(mounts=EXPOSED_MOUNTS),
    ]
    service.poll_once()
    assert gate.current is not None
    service.poll_once()
    assert gate.current is None


def test_helper_serves_again_once_the_mount_is_gone(
    service: HelperService, client: _Client
) -> None:
    client.responses = [_response(mounts=EXPOSED_MOUNTS), _response()]
    service.poll_once()
    assert service.state == "exposed"
    service.poll_once()
    assert service.state == "online"
