"""Touka 設定頁縮小開放名單（ADR-0330 2b）：拿掉或改成只能讀。

章對才做、一次性號碼只能用一次；名稱和路徑都要對上同一筆；只能縮小，沒有任何輸入能讓資料夾
變成可以改或讓名單變多；不跳視窗、不算停收；被看見時照其他工作回 ``helper_exposed``。
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from touka_local.approval import ApprovalGate
from touka_local.errors import Refused
from touka_local.grants import Environment, GrantBook
from touka_local.operations import Operations
from touka_local.protocol import DECIDED_BY_OPEN_REQUEST, Decision, Job, PollResponse
from touka_local.revoke import CHANGE_READ_ONLY, CHANGE_REMOVE, verify_revoke
from touka_local.service import HelperService
from touka_local.signing import sign_revoke, text_fingerprint
from touka_local.view import open_outcome_text, undo_result_text

if TYPE_CHECKING:
    from pathlib import Path

KEY = bytes(range(32))
HELPER_DIR = "C:\\Users\\Me\\AppData\\Local\\ToukaLocal"
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
_NONCES = iter(f"{n:022d}" for n in range(10**6))


@dataclass
class _Client:
    responses: list[PollResponse] = field(default_factory=list)
    results: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def poll(self) -> PollResponse:
        return self.responses.pop(0)

    def confirm(self, _job_id: str) -> bool:
        return True

    def result(self, job_id: str, payload: dict[str, Any]) -> bool:
        self.results.append((job_id, payload))
        return True

    def report_folders(self, _payload: dict[str, Any]) -> bool:
        return True


@dataclass
class _Key:
    value: bytes | None = KEY


@pytest.fixture
def key() -> _Key:
    return _Key()


@pytest.fixture
def book(fs, tmp_path: Path) -> GrantBook:
    fs.add_dir("D:\\Open")
    fs.add_dir("D:\\View")
    grants = GrantBook(tmp_path / "g.json", fs=fs, env=ENV, drive_type=lambda _a: "fixed")
    grants.add("D:\\Open", writable=True)
    grants.add("D:\\View", writable=False)
    return grants


@pytest.fixture
def gate(clock) -> ApprovalGate:
    return ApprovalGate(clock=clock)


@pytest.fixture
def client() -> _Client:
    return _Client()


@pytest.fixture
def service(fs, book, trash, clock, gate, tmp_path: Path, client: _Client, key: _Key):
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
        decision_key=lambda: key.value,
    )


def _decision(
    name: str,
    path: str,
    change: str,
    *,
    job_id: str,
    nonce: str | None = None,
    signing_key: bytes = KEY,
) -> Decision:
    nonce = nonce if nonce is not None else next(_NONCES)
    return Decision(
        nonce=nonce,
        digest=text_fingerprint(path),
        shown=text_fingerprint(name),
        decided_by=DECIDED_BY_OPEN_REQUEST,
        signature=sign_revoke(
            signing_key, job_id=job_id, nonce=nonce, name=name, path=path, change=change
        ),
    )


def _job(
    name: str = "Open",
    path: str = "D:\\Open",
    change: str = CHANGE_READ_ONLY,
    *,
    job_id: str = "r1",
    args: dict[str, Any] | None = None,
    decision: Decision | None | str = "sign",
) -> Job:
    if decision == "sign":
        decision = _decision(name, path, change, job_id=job_id)
    return Job(
        id=job_id,
        action="revoke",
        args={"name": name, "path": path, "change": change} if args is None else args,
        requester="owner",
        decide_within=30.0,
        decision=decision,  # type: ignore[arg-type]
    )


def _poll(service: HelperService, client: _Client, job: Job, *, mounts: tuple[str, ...] = ()):
    client.responses.append(PollResponse(job=job, cancelled=(), host_mount_roots=mounts))
    service.poll_once()
    return client.results[-1][1]


def _saved(tmp_path: Path) -> list[dict[str, Any]]:
    return json.loads((tmp_path / "g.json").read_text(encoding="utf-8"))["grants"]


# --- 驗章 ---


def test_a_good_revoke_is_verified() -> None:
    assert verify_revoke(_job(), KEY) == ("Open", "D:\\Open", CHANGE_READ_ONLY)


@pytest.mark.parametrize(
    "job",
    [
        _job(args={"name": "Open", "path": "D:\\Open"}),
        _job(args={"name": "Open", "path": "D:\\Open", "change": "read_only", "extra": 1}),
        _job(args={"name": 1, "path": "D:\\Open", "change": "read_only"}),
        _job(args={"name": "Open", "path": ["D:\\Open"], "change": "read_only"}),
        _job(name="Open", path="D:\\Open", change="writable"),
        _job(name="", path="D:\\Open"),
        _job(name="Open", path="D:\\" + "a" * 1100),
        # 單獨的代理字元算不出指紋：先擋下，不會在算指紋時出錯
        _job(
            name="Open\ud800",
            path="D:\\Open",
            decision=Decision("0" * 22, "a" * 64, "b" * 64, DECIDED_BY_OPEN_REQUEST, "c" * 64),
        ),
        _job(decision=None),
        _job(decision=_decision("Open", "D:\\Open", "remove", job_id="r1")),
        _job(decision=_decision("Open", "D:\\Open", "read_only", job_id="r2")),
        _job(
            decision=_decision("Open", "D:\\Open", "read_only", job_id="r1", signing_key=bytes(32))
        ),
    ],
)
def test_anything_wrong_is_a_bad_signature(job: Job) -> None:
    with pytest.raises(Refused) as refused:
        verify_revoke(job, KEY)
    assert refused.value.code == "bad_signature"


def test_without_a_key_the_helper_asks_for_a_new_pairing() -> None:
    with pytest.raises(Refused) as refused:
        verify_revoke(_job(), None)
    assert refused.value.code == "needs_new_pairing"


# --- 縮小名單 ---


def test_read_only_changes_the_folder_without_a_window(service, client, gate, book, tmp_path):
    before = book.revision
    payload = _poll(service, client, _job())
    assert payload["status"] == "ok"
    data = payload["data"]
    assert data["name"] == "Open"
    assert data["writable"] is False
    assert "removed" not in data
    assert data["instance"] == service._instance
    assert data["revision"] == before + 1 == book.revision
    assert gate.current is None
    assert {g["name"]: g["writable"] for g in _saved(tmp_path)} == {"Open": False, "View": False}
    assert service.open_outcome == ("touka_read_only", "Open")
    assert open_outcome_text(*service.open_outcome) == "Touka 設定頁把「Open」改成只能看。"


def test_remove_takes_the_folder_off_the_list(service, client, book, tmp_path):
    payload = _poll(service, client, _job(change=CHANGE_REMOVE))
    assert payload["status"] == "ok"
    assert payload["data"]["removed"] is True
    assert "writable" not in payload["data"]
    assert [g["name"] for g in _saved(tmp_path)] == ["View"]
    assert service.open_outcome == ("touka_removed", "Open")
    assert open_outcome_text(*service.open_outcome) == "Touka 設定頁拿掉了「Open」。"


def test_read_only_on_a_read_only_folder_succeeds_without_saving(service, client, book):
    before = book.revision
    payload = _poll(service, client, _job("View", "D:\\View"))
    assert payload["status"] == "ok"
    assert payload["data"]["writable"] is False
    assert payload["data"]["revision"] == before == book.revision


def test_name_and_path_must_match_the_same_entry(service, client, tmp_path):
    payload = _poll(service, client, _job("Open", "D:\\View"))
    assert (payload["status"], payload["code"]) == ("refused", "not_found")
    assert {g["name"]: g["writable"] for g in _saved(tmp_path)} == {"Open": True, "View": False}


def test_name_and_path_match_regardless_of_case(service, client, tmp_path):
    payload = _poll(service, client, _job("open", "d:\\OPEN"))
    assert payload["status"] == "ok"
    assert payload["data"]["name"] == "Open"
    assert {g["name"]: g["writable"] for g in _saved(tmp_path)}["Open"] is False


def test_a_nonce_works_only_once(service, client):
    decision = _decision("Open", "D:\\Open", CHANGE_REMOVE, job_id="r1")
    first = _poll(service, client, _job(change=CHANGE_REMOVE, decision=decision))
    assert first["status"] == "ok"
    again = _poll(service, client, _job(change=CHANGE_REMOVE, decision=decision))
    assert (again["status"], again["code"]) == ("refused", "bad_signature")


def test_a_bad_stamp_changes_nothing(service, client, tmp_path):
    payload = _poll(
        service,
        client,
        _job(
            decision=_decision(
                "Open", "D:\\Open", CHANGE_READ_ONLY, job_id="r1", signing_key=bytes(32)
            )
        ),
    )
    assert (payload["status"], payload["code"]) == ("refused", "bad_signature")
    assert {g["name"]: g["writable"] for g in _saved(tmp_path)}["Open"] is True


def test_an_exposed_helper_refuses_like_any_other_job(service, client, tmp_path):
    payload = _poll(service, client, _job(), mounts=(HELPER_DIR,))
    assert (payload["status"], payload["code"]) == ("refused", "helper_exposed")
    assert {g["name"]: g["writable"] for g in _saved(tmp_path)}["Open"] is True


def test_revokes_do_not_count_toward_pausing_open_requests(service, client):
    for number in range(5):
        _poll(service, client, _job("Missing", "D:\\Missing", job_id=f"r{number}"))
    assert service.open_paused_until is None


def test_a_save_failure_is_reported_and_changes_nothing(service, client, book, monkeypatch):
    def _fail(*_args: Any, **_kwargs: Any) -> None:
        raise OSError

    monkeypatch.setattr("touka_local.grants.durable_write_text", _fail)
    payload = _poll(service, client, _job())
    assert (payload["status"], payload["code"]) == ("error", "io_error")
    assert {g.name: g.writable for g in book.grants()}["Open"] is True


@pytest.mark.parametrize("change", [CHANGE_READ_ONLY, CHANGE_REMOVE])
def test_the_list_only_ever_shrinks(book, change: str) -> None:
    before = {g.name: g.writable for g in book.grants()}
    for name, path in (("Open", "D:\\Open"), ("View", "D:\\View"), ("Nope", "D:\\Nope")):
        with contextlib.suppress(Refused):
            book.shrink(name, path, change)
    after = {g.name: g.writable for g in book.grants()}
    assert set(after) <= set(before)
    assert not any(after.values())


def test_shrink_refuses_anything_but_the_two_changes(book) -> None:
    with pytest.raises(Refused):
        book.shrink("View", "D:\\View", "writable")
    assert {g.name: g.writable for g in book.grants()}["View"] is False


def test_undo_explains_moves_whose_folder_was_taken_away() -> None:
    counts = {"restored": 1, "skipped": 0, "not_undoable": 0}
    text = undo_result_text({**counts, "grant_gone": 2})
    assert "2 項的資料夾已經拿掉、只能讀或暫停中" in text
    assert "加回" in text
    assert "資料夾已經拿掉" not in undo_result_text(counts)
    assert "同一批請不要再按復原" in undo_result_text({**counts, "journal_incomplete": 1})
