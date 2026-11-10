"""核准關卡與畫面文字（ADR-0311）：一次一個、冷卻、自動拒絕、核准鍵延遲、顯示消毒。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from touka_local.approval import (
    APPROVE_DELAY_SECONDS,
    MAX_DECISION_SECONDS,
    REJECT_COOLDOWN_SECONDS,
    ApprovalGate,
)
from touka_local.errors import Refused
from touka_local.view import (
    APPROVAL_COLUMNS,
    APPROVAL_WINDOW_WIDTH,
    all_paths_text,
    approval_counts,
    approval_rows,
    approval_title,
    clean_reason,
    connection_text,
    display_safe,
    full_paths_text,
    human_size,
    refusal_text,
    report_text,
    requester_text,
    row_values,
)


@dataclass(frozen=True)
class _Plan:
    digest: str = "d" * 64


@pytest.fixture
def gate(clock) -> ApprovalGate:
    return ApprovalGate(clock=clock)


def _code(callable_, *args, **kwargs) -> str:
    with pytest.raises(Refused) as caught:
        callable_(*args, **kwargs)
    return caught.value.code


def test_offer_then_approve(gate: ApprovalGate, clock) -> None:
    plan = _Plan()
    gate.offer("j1", plan, 540, requester="soul")
    current = gate.current
    assert current is not None
    assert (current.job_id, current.plan, current.requester) == ("j1", plan, "soul")
    clock.now += APPROVE_DELAY_SECONDS
    assert gate.approve("j1", plan.digest) is plan
    assert gate.current is None


def test_second_window_is_refused(gate: ApprovalGate) -> None:
    gate.offer("j1", _Plan(), 540)
    assert _code(gate.offer, "j2", _Plan(), 540) == "approval_busy"
    assert gate.current.job_id == "j1"  # type: ignore[union-attr]


def test_approve_is_delayed(gate: ApprovalGate, clock) -> None:
    plan = _Plan()
    gate.offer("j1", plan, 540)
    clock.now += APPROVE_DELAY_SECONDS - 0.01
    assert _code(gate.approve, "j1", plan.digest) == "too_soon"
    assert gate.current is not None


def test_approve_binds_to_digest(gate: ApprovalGate, clock) -> None:
    gate.offer("j1", _Plan(), 540)
    clock.now += APPROVE_DELAY_SECONDS
    assert _code(gate.approve, "j1", "e" * 64) == "changed"
    assert gate.current is not None


def test_approve_binds_to_job(gate: ApprovalGate, clock) -> None:
    plan = _Plan()
    gate.offer("j1", plan, 540)
    clock.now += APPROVE_DELAY_SECONDS
    assert _code(gate.approve, "j2", plan.digest) == "no_pending"


def test_approve_twice(gate: ApprovalGate, clock) -> None:
    plan = _Plan()
    gate.offer("j1", plan, 540)
    clock.now += APPROVE_DELAY_SECONDS
    gate.approve("j1", plan.digest)
    assert _code(gate.approve, "j1", plan.digest) == "no_pending"


def test_cannot_approve_after_deadline_even_before_tick(gate: ApprovalGate, clock) -> None:
    plan = _Plan()
    gate.offer("j1", plan, 540)
    clock.now += 540
    assert _code(gate.approve, "j1", plan.digest) == "expired"


def test_reject_starts_cooldown(gate: ApprovalGate, clock) -> None:
    gate.offer("j1", _Plan(), 540)
    assert gate.reject("j1") is True
    assert gate.current is None
    clock.now += REJECT_COOLDOWN_SECONDS - 0.01
    assert _code(gate.offer, "j2", _Plan(), 540) == "approval_cooldown"
    clock.now += 0.01
    gate.offer("j2", _Plan(), 540)


def test_reject_unknown_job(gate: ApprovalGate) -> None:
    assert gate.reject("nope") is False


def test_auto_reject_at_limit(gate: ApprovalGate, clock) -> None:
    gate.offer("j1", _Plan(), 9999)
    clock.now += MAX_DECISION_SECONDS - 0.01
    assert gate.expire_due() is None
    clock.now += 0.01
    expired = gate.expire_due()
    assert expired is not None and expired.job_id == "j1"
    assert gate.current is None
    assert _code(gate.offer, "j2", _Plan(), 540) == "approval_cooldown"


def test_shorter_limit_from_touka_is_respected(gate: ApprovalGate, clock) -> None:
    gate.offer("j1", _Plan(), 10)
    clock.now += 10
    assert gate.expire_due() is not None


def test_limit_is_540_seconds() -> None:
    assert MAX_DECISION_SECONDS == 540


def test_cancel_also_starts_the_cooldown(gate: ApprovalGate, clock) -> None:
    gate.offer("j1", _Plan(), 540)
    assert gate.cancel("j1") is True
    assert gate.cancel("j1") is False
    assert _code(gate.offer, "j2", _Plan(), 540) == "approval_cooldown"
    clock.now += REJECT_COOLDOWN_SECONDS
    gate.offer("j2", _Plan(), 540)


# --- 顯示 ---


@pytest.mark.parametrize(
    "text",
    ["a‮b", "a⁦b", "a‏b", "a\x01b", "a\nb", "a\tb", "a\x7fb", "a b", "a﻿b"],
)
def test_display_safe_makes_hidden_characters_visible(text: str) -> None:
    shown = display_safe(text)
    assert len(shown) == len(text)
    assert shown[1] != text[1]
    assert shown[0] == "a" and shown[2] == "b"


def test_display_safe_keeps_ordinary_text() -> None:
    assert display_safe("報告 (final)_v2.txt") == "報告 (final)_v2.txt"


def test_clean_reason() -> None:
    assert clean_reason("整理\n  下載\t資料夾") == "整理 下載 資料夾"
    assert clean_reason("  ") == ""
    assert len(clean_reason("字" * 400)) == 300
    assert "‮" not in clean_reason("a‮b")


@dataclass(frozen=True)
class _Item:
    op: str
    source_text: str
    destination_text: str | None = None
    size: int = 0
    is_folder: bool = False
    contained: int = 0
    unusual: tuple[str, ...] = ()
    source_full: str = "D:\\Open\\x"
    destination_full: str | None = None


@dataclass(frozen=True)
class _ViewPlan:
    items: tuple[_Item, ...]
    reason: str = ""


def _view_plan() -> _ViewPlan:
    return _ViewPlan(
        items=(
            _Item(
                "move",
                "Open/a‮.txt",
                "Open/b.txt",
                size=2048,
                source_full="D:\\Open\\a‮.txt",
                destination_full="D:\\Open\\b.txt",
            ),
            _Item("delete", "Open/run.exe", size=1, unusual=("executable",)),
            _Item(
                "delete",
                "Open/big",
                size=5,
                is_folder=True,
                contained=150,
                unusual=("many_items", "hidden"),
            ),
        )
    )


def test_rows() -> None:
    rows = approval_rows(_view_plan())
    assert [r.action for r in rows] == ["搬移", "丟到回收桶", "丟到回收桶"]
    assert "‮" not in rows[0].source
    assert rows[0].destination == "b.txt  |  Open"
    assert rows[1].destination == ""
    assert rows[0].detail == "2.0 KB"
    assert rows[2].detail == "資料夾，150 項，5 B"
    assert rows[0].marks == ""
    assert rows[1].marks == "可執行檔"
    assert rows[2].marks == "項目很多、隱藏檔"


def test_counts_and_title() -> None:
    counts = approval_counts(_view_plan())
    assert (counts.moves, counts.deletes, counts.unusual) == (1, 2, 2)
    assert approval_title(counts) == "搬移 1 項、丟到回收桶 2 項"


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536 * 1024, "1.5 MB"),
        (3 * 1024**3, "3.0 GB"),
    ],
)
def test_human_size(size: int, text: str) -> None:
    assert human_size(size) == text


@pytest.mark.parametrize(
    "code",
    [
        "bad_path",
        "protected",
        "link",
        "git_project",
        "drive_type",
        "overlap",
        "blocked",
        "system_folder",
        "not_found",
        "not_a_folder",
        "bad_port",
        "bad_token",
        "approval_busy",
        "too_soon",
        "expired",
        "changed",
        "no_pending",
        "busy",
    ],
)
def test_refusal_text_is_human(code: str) -> None:
    assert refusal_text(code) != refusal_text("something_unknown")


def test_report_text() -> None:
    report = {
        "items": [
            {"path": "a", "status": "done"},
            {"path": "b", "status": "done"},
            {"path": "c", "status": "changed"},
            {"path": "d", "status": "failed"},
            {"path": "e", "status": "aborted"},
            {"path": "f", "status": "not_done"},
        ]
    }
    assert (
        report_text(report) == "完成 2 項，檔案有變動跳過 1 項，失敗 1 項，被中止 1 項，沒做 1 項"
    )
    assert report_text({"items": [{"path": "a", "status": "done"}]}) == "完成 1 項"


@pytest.mark.parametrize(
    ("state", "text"),
    [
        ("online", "已連線"),
        ("offline", "連不上 Touka"),
        ("unpaired", "尚未配對"),
        ("upgrade_needed", "版本不符，請到 GitHub 的 Touka 官方頁面取得對應版本，重新安裝小幫手"),
        (
            "exposed",
            "小幫手、它用的 Python 或桌面上的 ToukaLocal.cmd 在 Touka 碰得到的資料夾裡，"
            "為了安全已停止服務。請把 .env 的掛載路徑改到不包含這些位置",
        ),
    ],
)
def test_connection_text(state: str, text: str) -> None:
    assert connection_text(state) == text


def test_file_name_comes_first_so_long_paths_cannot_hide_it() -> None:
    folder = "/".join(["Open", *(["很長的資料夾名稱"] * 30)])
    full = "D:\\" + folder.replace("/", "\\") + "\\重要.txt"
    row = approval_rows(
        _ViewPlan(items=(_Item("delete", f"{folder}/重要.txt", source_full=full),))
    )[0]
    assert row.source.startswith("重要.txt")
    assert row.source.endswith(folder)
    assert row.full_source == full


def test_selected_row_shows_the_complete_paths() -> None:
    rows = approval_rows(_view_plan())
    assert full_paths_text(rows[0]) == "位置：D:\\Open\\a⍰.txt\n新位置：D:\\Open\\b.txt"
    assert full_paths_text(rows[1]) == "位置：D:\\Open\\x"


def test_requester_is_marked_as_unreliable() -> None:
    text = requester_text("soul\n  a\u202eb")
    assert text.startswith("提出者（可能不準）：soul a")
    assert "\u202e" not in text
    assert "\n" not in text


def test_report_warns_when_the_record_is_incomplete() -> None:
    report = {"items": [{"path": "a", "status": "done"}], "journal_incomplete": True}
    text = report_text(report)
    assert text.startswith("完成 1 項")
    assert "紀錄沒有寫完整" in text


def test_size_and_warnings_come_before_the_paths() -> None:
    keys = [key for key, _heading, _width in APPROVAL_COLUMNS]
    assert keys.index("marks") < keys.index("source")
    assert keys.index("detail") < keys.index("source")


def test_every_column_fits_in_the_window() -> None:
    total = sum(width for _key, _heading, width in APPROVAL_COLUMNS)
    assert total <= APPROVAL_WINDOW_WIDTH - 40


def test_row_values_follow_the_columns() -> None:
    row = approval_rows(_view_plan())[2]
    keys = [key for key, _heading, _width in APPROVAL_COLUMNS]
    assert dict(zip(keys, row_values(row), strict=True)) == {
        "action": row.action,
        "marks": row.marks,
        "detail": row.detail,
        "source": row.source,
        "destination": row.destination,
    }


@pytest.mark.parametrize("character", ["\uff0f", "\uff3c", "\u2215", "\uff5c", "\u00a0", "\u3000"])
def test_lookalike_characters_are_shown_as_marks_in_paths(character: str) -> None:
    shown = display_safe(f"a{character}b")
    assert shown == "a⍰b"


def test_a_reason_keeps_its_punctuation() -> None:
    assert clean_reason("整理／封存") == "整理／封存"


def test_a_name_cannot_fake_the_folder_part() -> None:
    fake = "a  \uff5c  C:\uff3cWindows.txt"
    row = approval_rows(_ViewPlan(items=(_Item("delete", f"Open/{fake}"),)))[0]
    assert "\uff5c" not in row.source
    assert "\uff3c" not in row.source


def test_every_item_is_listed_with_its_full_path() -> None:
    rows = approval_rows(_view_plan())
    text = all_paths_text(rows)
    for number, row in enumerate(rows, start=1):
        assert f"{number}. {row.action}" in text
        assert row.full_source in text
    assert rows[0].full_destination in text
    assert text.index("1. ") < text.index("2. ") < text.index("3. ")


def test_secret_mark_has_words() -> None:
    plan = _ViewPlan(items=(_Item("delete", "Open/a.pem", unusual=("secret",)),))
    assert approval_rows(plan)[0].marks == "金鑰或密碼檔"
