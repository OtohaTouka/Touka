"""照 Touka 的模式核准（ADR-0317）：只有章、號碼、指紋都對，又在底線以內，才不跳視窗。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from touka_local.approval import APPROVE_DELAY_SECONDS, ApprovalGate
from touka_local.grants import Environment, GrantBook
from touka_local.operations import Operations
from touka_local.protocol import Decision, Job, PollResponse
from touka_local.service import HelperService
from touka_local.signing import sign_decision, text_fingerprint

if TYPE_CHECKING:
    from pathlib import Path

KEY = bytes(range(32))


@dataclass
class _Client:
    responses: list[object] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    results: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def poll(self) -> PollResponse:
        return self.responses.pop(0)  # type: ignore[return-value]

    def confirm(self, job_id: str) -> bool:
        self.confirmed.append(job_id)
        return True

    def result(self, job_id: str, payload: dict[str, Any]) -> bool:
        self.results.append((job_id, payload))
        return True

    def report_folders(self, _payload: dict[str, Any]) -> bool:
        return True


@dataclass
class _Switches:
    key: bytes | None = KEY
    following: bool = True


@pytest.fixture
def client() -> _Client:
    return _Client()


@pytest.fixture
def switches() -> _Switches:
    return _Switches()


@pytest.fixture
def gate(clock) -> ApprovalGate:
    return ApprovalGate(clock=clock)


@pytest.fixture
def service(
    client: _Client, fs, trash, clock, gate: ApprovalGate, tmp_path: Path, switches: _Switches
) -> HelperService:
    env = Environment(
        user_profile="C:\\Users\\Me",
        windows_dir="C:\\Windows",
        program_files=("C:\\Program Files",),
        program_data="C:\\ProgramData",
        app_data="C:\\Users\\Me\\AppData\\Roaming",
        local_app_data="C:\\Users\\Me\\AppData\\Local",
        helper_dir="C:\\Users\\Me\\AppData\\Local\\ToukaLocal",
        code_paths=("C:\\Python",),
        touka_folder="D:\\Touka",
    )
    fs.add_dir("D:\\Open")
    fs.add_dir("D:\\Other")
    book = GrantBook(tmp_path / "g.json", fs=fs, env=env, drive_type=lambda _a: "fixed")
    book.add("D:\\Open", writable=True)
    book.add("D:\\Other", writable=True)
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
        decision_key=lambda: switches.key,
        following=lambda: switches.following,
    )


ITEMS = [{"op": "delete", "path": "Open/a.txt"}]
ARGS = {"items": ITEMS, "reason": "清"}


def _run(service: HelperService, client: _Client, job: Job) -> dict[str, Any]:
    client.responses.append(PollResponse(job=job, cancelled=(), host_mount_roots=()))
    service.poll_once()
    return client.results[-1][1] if client.results else {}


def _preview(
    service: HelperService, client: _Client, args: dict[str, Any] = ARGS
) -> dict[str, Any]:
    job = Job(id="p1", action="preview", args=args, requester="s", decide_within=120.0)
    payload = _run(service, client, job)
    assert payload["status"] == "ok"
    return payload["data"]


def _signed(
    preview: dict[str, Any],
    *,
    job_id: str = "d1",
    decided_by: str = "auto_review",
    key: bytes = KEY,
    **changes: str,
) -> Decision:
    fields = {
        "nonce": preview["nonce"],
        "digest": preview["digest"],
        "shown": _shown(preview["text"]),
        "decided_by": decided_by,
    }
    fields.update(changes)
    signature = sign_decision(key, job_id=job_id, **fields)
    return Decision(signature=signature, **fields)


def _shown(text: str) -> str:
    return text_fingerprint(text)


def _decide(
    service: HelperService, client: _Client, decision: Decision | None, job_id: str = "d1"
) -> dict[str, Any]:
    client.results.clear()
    return _run(
        service,
        client,
        Job(
            id=job_id,
            action="propose",
            args=ARGS,
            requester="s",
            decide_within=540.0,
            decision=decision,
        ),
    )


def test_preview_shows_what_the_window_would_show(service, client, fs) -> None:
    fs.add_file("D:\\Open\\a.txt")
    preview = _preview(service, client)
    assert len(preview["nonce"]) == 22
    assert preview["items"] == 1 and preview["entries"] == 1
    assert "D:\\Open\\a.txt" in preview["text"]
    assert preview["window_reason"] is None
    assert fs.exists("D:\\Open\\a.txt")


def test_a_good_decision_runs_without_a_window(service, client, fs, gate, trash) -> None:
    fs.add_file("D:\\Open\\a.txt")
    preview = _preview(service, client)
    payload = _decide(service, client, _signed(preview))
    assert gate.current is None
    assert client.confirmed == ["d1"]
    assert payload["status"] == "ok"
    assert payload["data"]["decided_by"] == "auto_review"
    assert trash.sent == ["D:\\Open\\a.txt"]
    assert service._operations.history(service._operations.wall_time()).mode_batches


@pytest.mark.parametrize(
    "tamper",
    [
        {"key": bytes(32)},
        {"job_id": "other"},
        {"decided_by": "helper_window"},
        {"shown": "0" * 64},
        {"digest": "0" * 64},
        {"nonce": "x" * 22},
    ],
)
def test_anything_wrong_opens_the_window_instead(
    service, client, fs, gate, trash, tamper: dict[str, Any]
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    preview = _preview(service, client)
    _decide(service, client, _signed(preview, **tamper))
    assert gate.current is not None
    assert gate.current.reason == "no_decision"
    assert client.confirmed == []
    assert trash.sent == []


def test_a_one_time_number_works_once(service, client, fs, gate, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    preview = _preview(service, client)
    decision = _signed(preview, decided_by="yolo")
    fs.add_file("D:\\Open\\b.txt")
    service._operations.plan(ARGS)  # 不影響號碼
    first = _decide(service, client, decision)
    assert first["data"]["decided_by"] == "yolo"
    fs.add_file("D:\\Open\\a.txt")
    _decide(service, client, decision, job_id="d1")
    assert gate.current is not None and gate.current.reason == "no_decision"


def test_an_expired_number_opens_the_window(service, client, fs, gate, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    preview = _preview(service, client)
    clock.now += 541
    _decide(service, client, _signed(preview))
    assert gate.current is not None and gate.current.reason == "no_decision"


def test_files_that_changed_after_the_preview_stop_the_list(
    service, client, fs, gate, trash
) -> None:
    fs.add_file("D:\\Open\\a.txt", b"old")
    preview = _preview(service, client)
    fs.add_file("D:\\Open\\a.txt", b"new and longer")
    payload = _decide(service, client, _signed(preview))
    assert payload["status"] == "refused" and payload["code"] == "changed"
    assert gate.current is None and trash.sent == []


def test_without_a_key_or_switched_off_the_window_opens(
    service, client, fs, gate, switches: _Switches
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    switches.following = False
    preview = _preview(service, client)
    assert preview["window_reason"] == "not_following"
    _decide(service, client, _signed(preview))
    assert gate.current is not None and gate.current.reason == "not_following"


def test_a_floor_opens_the_window_and_a_person_can_still_approve(
    service, client, fs, gate, clock, trash
) -> None:
    """碰到底線改跳視窗時，執行的位置要先放掉，人按核准才不會被當成忙碌（N1）。"""
    fs.add_file("D:\\Open\\a.txt")
    fs.add_file("D:\\Open\\key.pem")
    args = {"items": [{"op": "delete", "path": "Open/key.pem"}], "reason": "清"}
    preview = _preview(service, client, args)
    assert preview["window_reason"] == "marked"
    client.results.clear()
    _run(
        service,
        client,
        Job(
            id="d1",
            action="propose",
            args=args,
            requester="s",
            decide_within=540.0,
            decision=_signed(preview),
        ),
    )
    pending = gate.current
    assert pending is not None and pending.reason == "marked"
    assert not service.running
    clock.now += APPROVE_DELAY_SECONDS
    service.approve("d1", pending.plan.digest)
    assert trash.sent == ["D:\\Open\\key.pem"]
    assert client.results[-1][1]["data"]["decided_by"] == "helper_window"


def test_a_window_promised_by_the_preview_still_opens(
    service, client, fs, gate, trash, switches: _Switches
) -> None:
    """預覽時碰到底線，卡片就會說「小幫手還會跳視窗」；等卡片時原因消失了也要跳。"""
    fs.add_file("D:\\Open\\a.txt")
    switches.following = False
    preview = _preview(service, client)
    assert preview["window_reason"] == "not_following"
    switches.following = True
    _decide(service, client, _signed(preview))
    assert gate.current is not None and gate.current.reason == "not_following"
    assert trash.sent == []


def test_one_mode_decision_at_a_time(service, client, fs) -> None:
    fs.add_file("D:\\Open\\a.txt")
    preview = _preview(service, client)
    service._running = object()  # type: ignore[assignment]
    payload = _decide(service, client, _signed(preview))
    assert payload["status"] == "refused" and payload["code"] == "approval_busy"


def test_a_mode_decision_waits_for_an_open_window(service, client, fs, gate) -> None:
    fs.add_file("D:\\Open\\a.txt")
    preview = _preview(service, client)
    _decide(service, client, None, job_id="w1")
    assert gate.current is not None
    payload = _decide(service, client, _signed(preview))
    assert payload["status"] == "refused" and payload["code"] == "approval_busy"
    assert gate.current.job_id == "w1"


def test_a_failed_scan_releases_the_slot(service, client) -> None:
    payload = _decide(
        service,
        client,
        Decision("n" * 22, "a" * 64, "b" * 64, "yolo", "c" * 64),
    )
    assert payload["status"] == "refused"
    assert not service.running


def test_a_decision_on_another_kind_of_job_is_refused(service, client, fs) -> None:
    fs.add_file("D:\\Open\\a.txt")
    payload = _run(
        service,
        client,
        Job(
            id="r1",
            action="read",
            args={"path": "Open/a.txt"},
            requester="s",
            decide_within=30.0,
            decision=Decision("n" * 22, "a" * 64, "b" * 64, "yolo", "c" * 64),
        ),
    )
    assert payload["code"] == "bad_request"


def test_an_open_request_without_a_stamp_is_refused(service, client, gate) -> None:
    # 開放請求另外在 test_local_helper_open_request.py 測（ADR-0320）；沒有章就不跳視窗
    payload = _run(
        service,
        client,
        Job(id="o1", action="request_open", args={}, requester="s", decide_within=30.0),
    )
    assert payload["code"] == "bad_signature"
    assert gate.current is None


def test_a_new_pairing_forgets_old_previews(service, client, fs, gate) -> None:
    fs.add_file("D:\\Open\\a.txt")
    preview = _preview(service, client)
    service.replace_client(client)
    _decide(service, client, _signed(preview))
    assert gate.current is not None and gate.current.reason == "no_decision"


def test_without_a_decision_the_window_says_who_decided(service, client, fs, gate, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    _decide(service, client, None)
    pending = gate.current
    assert pending is not None
    clock.now += APPROVE_DELAY_SECONDS
    service.approve("d1", pending.plan.digest)
    assert client.results[-1][1]["data"]["decided_by"] == "helper_window"
