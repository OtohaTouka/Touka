"""開放名單回報給 Touka（ADR-0330）：送什麼、什麼時候送、壞資料不讓整份被拒。

都是純判斷與假連線，CI 的 Linux 也能測。
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from touka_local import grants as grants_module
from touka_local import service as service_module
from touka_local.approval import ApprovalGate
from touka_local.connection import AuthLost, ConnectionLost, ProtocolMismatch, ToukaClient
from touka_local.folder_report import FOLDER_REPORT_SECONDS, FolderReporter
from touka_local.grants import Environment, GrantBook
from touka_local.operations import Operations
from touka_local.protocol import (
    FOLDER_ENTRY_FIELDS,
    FOLDER_REPORT_FIELDS,
    MAX_REPORTED_FOLDERS,
    MAX_REPORTED_PATH_CHARS,
    PROTOCOL_VERSION,
    PollResponse,
    folder_report,
)
from touka_local.service import STATE_EXPOSED, STATE_ONLINE, HelperService

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "t" * 43
INSTANCE = "i" * 22


# --- 回報的內容 ---


def test_a_report_has_exactly_the_protocol_fields() -> None:
    payload = folder_report(
        instance=INSTANCE,
        revision=3,
        exposed=False,
        folders=[("Open", "D:\\Open", True, False)],
    )
    assert set(payload) == FOLDER_REPORT_FIELDS
    assert payload["protocol"] == PROTOCOL_VERSION
    assert payload["instance"] == INSTANCE
    assert payload["revision"] == 3
    assert payload["exposed"] is False
    assert payload["folders"] == [
        {"name": "Open", "path": "D:\\Open", "writable": True, "usable": False}
    ]
    assert set(payload["folders"][0]) == FOLDER_ENTRY_FIELDS
    assert payload["omitted"] == 0


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("", "D:\\Bad"),
        ("a\x07b", "D:\\Bad"),
        ("Bad", "D:\\a\u202eb"),
        ("Bad", "D:\\a\u200fb"),
        ("Bad", "D:\\a\u061cb"),
        ("Bad", "D:\\a\x85b"),
        ("Bad", "D:\\a\ud800b"),
        ("Bad", "D:\\" + "a" * MAX_REPORTED_PATH_CHARS),
        ("B" * 301, "D:\\Bad"),
    ],
)
def test_an_entry_touka_would_refuse_is_left_out(name: str, path: str) -> None:
    # 手改過的名單可能有一筆不合規則：只少那一筆，整份照樣送得出去
    payload = folder_report(
        instance=INSTANCE,
        revision=0,
        exposed=False,
        folders=[(name, path, False, True), ("Good", "D:\\Good", False, True)],
    )
    assert [entry["name"] for entry in payload["folders"]] == ["Good"]
    assert payload["omitted"] == 1
    # 單獨的代理字元會讓整份 UTF-8 編不出來，所以一定要先擋掉
    json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_more_than_the_limit_are_counted_not_sent() -> None:
    folders = [(f"F{i}", f"D:\\F{i}", False, True) for i in range(MAX_REPORTED_FOLDERS + 6)]
    payload = folder_report(instance=INSTANCE, revision=0, exposed=False, folders=folders)
    assert [entry["name"] for entry in payload["folders"]] == [
        f"F{i}" for i in range(MAX_REPORTED_FOLDERS)
    ]
    assert payload["omitted"] == 6


# --- 什麼時候送 ---


def test_the_first_report_is_due_and_then_it_waits(clock) -> None:
    reporter = FolderReporter(clock=clock)
    assert reporter.due(0, exposed=False)
    reporter.record(0, exposed=False, ok=True)
    assert not reporter.due(0, exposed=False)
    clock.now += FOLDER_REPORT_SECONDS - 1
    assert not reporter.due(0, exposed=False)
    clock.now += 1
    assert reporter.due(0, exposed=False)


def test_a_change_is_due_at_once(clock) -> None:
    reporter = FolderReporter(clock=clock)
    reporter.record(0, exposed=False, ok=True)
    assert reporter.due(1, exposed=False)
    assert reporter.due(0, exposed=True)


def test_after_a_failure_it_waits_before_trying_again(clock) -> None:
    reporter = FolderReporter(clock=clock)
    reporter.record(0, exposed=False, ok=False)
    # 名單變了也一樣：送不出去時不是每一輪都重送
    assert not reporter.due(1, exposed=False)
    clock.now += FOLDER_REPORT_SECONDS
    assert reporter.due(1, exposed=False)


def test_a_new_connection_reports_again_at_once(clock) -> None:
    reporter = FolderReporter(clock=clock)
    reporter.record(0, exposed=False, ok=True)
    reporter.connection_lost()
    assert reporter.due(0, exposed=False)
    reporter.record(0, exposed=False, ok=False)
    reporter.connection_lost()
    assert reporter.due(0, exposed=False)


# --- 名單的修改次數 ---


def _env() -> Environment:
    return Environment(
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


@pytest.fixture
def book(fs, tmp_path: Path) -> GrantBook:
    fs.add_dir("D:\\Open")
    fs.add_dir("D:\\Other")
    book = GrantBook(tmp_path / "g.json", fs=fs, env=_env(), drive_type=lambda _a: "fixed")
    book.add("D:\\Open", writable=True)
    return book


def test_the_revision_counts_saved_changes(book: GrantBook) -> None:
    start = book.revision
    book.add("D:\\Other", writable=False)
    book.set_writable("Other", writable=True)
    book.remove("Other")
    assert book.revision == start + 3


def test_a_change_that_could_not_be_saved_is_not_counted(
    book: GrantBook, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = book.revision

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(grants_module, "durable_write_text", fail)
    with pytest.raises(OSError):
        book.remove("Open")
    assert book.revision == start


def test_a_snapshot_says_which_folders_are_paused(book: GrantBook) -> None:
    revision, entries = book.snapshot()
    assert revision == book.revision
    assert [(grant.name, usable) for grant, usable in entries] == [("Open", True)]
    # 掛進容器的位置不能用：名單上還在，但暫停
    book.set_mount_roots(["D:\\Open"])
    _revision, entries = book.snapshot()
    assert [(grant.name, usable) for grant, usable in entries] == [("Open", False)]


# --- 輪詢之後送 ---


@dataclass
class _Client:
    responses: list[object] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)
    report_answer: bool | Exception = True

    def poll(self) -> PollResponse:
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]

    def confirm(self, _job_id: str) -> bool:
        return True

    def result(self, _job_id: str, _payload: dict[str, Any]) -> bool:
        return True

    def report_folders(self, payload: dict[str, Any]) -> bool:
        self.reports.append(payload)
        if isinstance(self.report_answer, Exception):
            raise self.report_answer
        return self.report_answer


def _response(mounts: tuple[str, ...] = ()) -> PollResponse:
    return PollResponse(job=None, cancelled=(), host_mount_roots=mounts)


@pytest.fixture
def client() -> _Client:
    return _Client()


def _service(client: _Client, fs, book: GrantBook, trash, clock, tmp_path: Path) -> HelperService:
    operations = Operations(
        fs=fs, grants=book, trash=trash, clock=clock, journal_dir=tmp_path / "j"
    )
    return HelperService(
        client=client,
        operations=operations,
        gate=ApprovalGate(clock=clock),
        grants=book,
        clock=clock,
        spawn=lambda work: work(),
    )


@pytest.fixture
def service(client: _Client, fs, book: GrantBook, trash, clock, tmp_path: Path) -> HelperService:
    return _service(client, fs, book, trash, clock, tmp_path)


def _poll(service: HelperService, client: _Client, response: object | None = None) -> bool:
    client.responses.append(_response() if response is None else response)
    return service.poll_once()


def test_a_successful_poll_sends_the_list(
    service: HelperService, client: _Client, book: GrantBook
) -> None:
    assert _poll(service, client)
    [report] = client.reports
    assert report["revision"] == book.revision
    assert report["exposed"] is False
    assert report["folders"] == [
        {"name": "Open", "path": "D:\\Open", "writable": True, "usable": True}
    ]
    assert len(report["instance"]) == 22


def test_it_sends_again_only_when_something_changes(
    service: HelperService, client: _Client, book: GrantBook, clock
) -> None:
    _poll(service, client)
    _poll(service, client)
    assert len(client.reports) == 1
    book.set_writable("Open", writable=False)
    _poll(service, client)
    assert len(client.reports) == 2
    assert client.reports[-1]["folders"][0]["writable"] is False
    clock.now += FOLDER_REPORT_SECONDS
    _poll(service, client)
    assert len(client.reports) == 3


@pytest.mark.parametrize(
    "failure",
    [ConnectionLost(), AuthLost(), ProtocolMismatch(), RuntimeError("boom")],
    ids=["connection", "auth", "protocol", "other"],
)
def test_after_a_failed_poll_it_sends_again(
    service: HelperService, client: _Client, failure: Exception
) -> None:
    _poll(service, client)
    assert not _poll(service, client, failure)
    assert len(client.reports) == 1
    _poll(service, client)
    assert len(client.reports) == 2


def test_after_a_new_pairing_it_sends_again(service: HelperService, client: _Client) -> None:
    _poll(service, client)
    service.replace_client(client)
    _poll(service, client)
    assert len(client.reports) == 2


def test_one_run_keeps_one_instance(
    service: HelperService, client: _Client, fs, book: GrantBook, trash, clock, tmp_path: Path
) -> None:
    _poll(service, client)
    clock.now += FOLDER_REPORT_SECONDS
    _poll(service, client)
    first, second = client.reports
    assert first["instance"] == second["instance"]
    # 重開小幫手是新的一次執行，Touka 靠這個知道修改次數從頭算
    other = _Client()
    _poll(_service(other, fs, book, trash, clock, tmp_path), other)
    assert other.reports[0]["instance"] != first["instance"]


def test_an_exposed_helper_still_reports(service: HelperService, client: _Client) -> None:
    _poll(service, client, _response(mounts=("C:\\Python",)))
    assert service.state == STATE_EXPOSED
    assert client.reports[-1]["exposed"] is True


@pytest.mark.parametrize("answer", [False, RuntimeError("boom")])
def test_a_failed_report_does_not_stop_polling(
    service: HelperService, client: _Client, answer: object, caplog: pytest.LogCaptureFixture
) -> None:
    client.report_answer = answer  # type: ignore[assignment]
    with caplog.at_level(logging.ERROR):
        assert _poll(service, client)
    assert service.state == STATE_ONLINE
    # 送不出去時隔一陣子才重試，不是每一輪
    _poll(service, client)
    assert len(client.reports) == 1


@pytest.mark.parametrize("broken", ["snapshot", "folder_report"])
def test_a_broken_list_does_not_stop_polling(
    service: HelperService,
    client: _Client,
    book: GrantBook,
    broken: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    if broken == "snapshot":
        monkeypatch.setattr(book, "snapshot", boom)
    else:
        monkeypatch.setattr(service_module, "folder_report", boom)
    with caplog.at_level(logging.ERROR):
        assert _poll(service, client)
    assert service.state == STATE_ONLINE
    assert client.reports == []
    assert "reporting the open folders failed" in caplog.text
    # 算一次送失敗：隔一陣子才再試
    monkeypatch.undo()
    _poll(service, client)
    assert client.reports == []


# --- 連線 ---


@dataclass
class _Response:
    status: int = 200

    def read(self, _limit: int = -1) -> bytes:
        return b"{}"

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@dataclass
class _Opener:
    outcome: object = field(default_factory=_Response)
    calls: list[urllib.request.Request] = field(default_factory=list)

    def open(self, request: urllib.request.Request, timeout: float) -> object:
        del timeout
        self.calls.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "x", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]


def test_the_report_goes_to_its_own_endpoint() -> None:
    opener = _Opener()
    client = ToukaClient(port=8000, token=TOKEN, helper_version="0.5.0", opener=opener)
    payload = folder_report(instance=INSTANCE, revision=0, exposed=False, folders=[])
    assert client.report_folders(payload) is True
    [request] = opener.calls
    assert request.full_url == "http://127.0.0.1:8000/v1/local-bridge/folder-report"
    assert json.loads(request.data) == payload


@pytest.mark.parametrize(
    "outcome",
    [_Response(status=204), _http_error(409), _http_error(500), urllib.error.URLError("x")],
)
def test_the_report_counts_only_a_200(outcome: object) -> None:
    client = ToukaClient(port=8000, token=TOKEN, helper_version="0.5.0", opener=_Opener(outcome))
    assert (
        client.report_folders(
            folder_report(instance=INSTANCE, revision=0, exposed=False, folders=[])
        )
        is False
    )
