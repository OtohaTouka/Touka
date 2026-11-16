"""小幫手自己的底線（ADR-0317）：每條剛好在線上能過、剛好超過就跳視窗。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from touka_local import floors
from touka_local.floors import History, ModeBatch, is_vacated, vacated_key, window_reason

NOW = 1_000_000.0
ROOT = "D:\\Open"


def _item(
    op: str = "move",
    source: str = "D:\\Open\\a.txt",
    destination: str | None = "D:\\Open\\b.txt",
    *,
    contained: int = 0,
    unusual: tuple[str, ...] = (),
    destination_root: str | None = ROOT,
) -> Any:
    return SimpleNamespace(
        op=op,
        source_full=source,
        destination_full=destination,
        source_root=ROOT,
        destination_root=destination_root if destination is not None else None,
        contained=contained,
        unusual=unusual,
    )


def _plan(*items: Any) -> Any:
    return SimpleNamespace(items=items or (_item(),))


def _reason(plan: Any = None, history: History | None = None, **flags: bool) -> str | None:
    return window_reason(
        plan if plan is not None else _plan(),
        history if history is not None else History(),
        following=flags.get("following", True),
        decision_ok=flags.get("decision_ok", True),
        now=NOW,
    )


def test_a_small_plain_list_goes_ahead() -> None:
    assert _reason() is None


def test_no_decision_or_switched_off_or_unreadable_records() -> None:
    assert _reason(decision_ok=False) == floors.REASON_NO_DECISION
    assert _reason(following=False) == floors.REASON_NOT_FOLLOWING
    assert _reason(history=History(readable=False)) == floors.REASON_JOURNAL_UNREADABLE


def test_item_count_floor() -> None:
    at_limit = tuple(
        _item(source=f"D:\\Open\\{n}", destination=None, op="delete") for n in range(20)
    )
    assert _reason(_plan(*at_limit)) is None
    assert _reason(_plan(*at_limit, _item(destination=None, op="delete"))) == (
        floors.REASON_TOO_MANY_ITEMS
    )


def test_entry_count_floor_counts_what_is_inside_folders() -> None:
    assert _reason(_plan(_item(op="delete", destination=None, contained=199))) is None
    assert _reason(_plan(_item(op="delete", destination=None, contained=200))) == (
        floors.REASON_TOO_MANY_ENTRIES
    )


@pytest.mark.parametrize("mark", ["secret", "executable", "many_items"])
def test_marked_items_go_to_a_person(mark: str) -> None:
    assert _reason(_plan(_item(unusual=(mark,)))) == floors.REASON_MARKED


def test_hidden_alone_is_not_enough() -> None:
    assert _reason(_plan(_item(unusual=("hidden",)))) is None


def _history(batches: list[ModeBatch], **kwargs: Any) -> History:
    return History(mode_batches=tuple(batches), **kwargs)


def test_hourly_batch_count() -> None:
    nine = [ModeBatch(at=NOW - 60, items=1, entries=1)] * 9
    assert _reason(history=_history(nine)) is None
    assert _reason(history=_history([*nine, nine[0]])) == floors.REASON_HOURLY_LIMIT


def test_hourly_items_and_entries() -> None:
    items = [ModeBatch(at=NOW - 60, items=99, entries=99)]
    assert _reason(history=_history(items)) is None
    assert _reason(history=_history([ModeBatch(at=NOW - 60, items=100, entries=100)])) == (
        floors.REASON_HOURLY_LIMIT
    )
    entries = [ModeBatch(at=NOW - 60, items=1, entries=299)]
    assert _reason(history=_history(entries)) is None
    assert _reason(history=_history([ModeBatch(at=NOW - 60, items=1, entries=300)])) == (
        floors.REASON_HOURLY_LIMIT
    )


def test_old_batches_roll_off_and_approving_in_the_window_does_not_reset() -> None:
    full = [ModeBatch(at=NOW - 3601, items=10, entries=10)] * 10
    assert _reason(history=_history(full)) is None


def test_future_batches_still_count() -> None:
    """時鐘被往回調，不能因此把總量歸零。"""
    future = [ModeBatch(at=NOW + 7200, items=10, entries=10)] * 10
    assert _reason(history=_history(future)) == floors.REASON_HOURLY_LIMIT


def test_the_main_window_reset_only_clears_what_came_before() -> None:
    before = [ModeBatch(at=NOW - 100, items=10, entries=10)] * 10
    assert _reason(history=_history(before, reset_at=NOW - 50)) is None
    after = [ModeBatch(at=NOW - 10, items=10, entries=10)] * 10
    assert _reason(history=_history(after, reset_at=NOW - 50)) == floors.REASON_HOURLY_LIMIT


def test_a_reset_time_in_the_future_is_ignored() -> None:
    full = [ModeBatch(at=NOW - 10, items=10, entries=10)] * 10
    assert _reason(history=_history(full, reset_at=NOW + 600)) == floors.REASON_HOURLY_LIMIT


def test_moving_between_open_folders() -> None:
    other = _item(destination="D:\\Other\\b.txt", destination_root="D:\\Other")
    assert _reason(_plan(other)) == floors.REASON_CROSS_FOLDER
    same_but_cased = _item(destination="D:\\open\\b.txt", destination_root="d:\\open")
    assert _reason(_plan(same_but_cased)) is None


@pytest.mark.parametrize(
    "destination",
    ["D:\\Open\\Gone", "D:\\open\\GONE", "D:\\Open\\Gone\\inner.txt"],
)
def test_moving_into_a_recently_vacated_place(destination: str) -> None:
    history = History(vacated=(vacated_key("D:\\Open\\Gone"),))
    assert _reason(_plan(_item(destination=destination)), history) == (
        floors.REASON_RECENTLY_VACATED
    )


@pytest.mark.parametrize("source", ["D:\\Open\\Proj", "D:\\open\\PROJ", "D:\\Open\\Proj\\notes.md"])
def test_moving_a_folder_that_holds_a_vacated_place(source: str) -> None:
    """刪了 Proj\\notes.md 再把 Proj 搬走或改名，7 天的空位不能跟著消失（對抗式複審 HIGH）。"""
    history = History(vacated=(vacated_key("D:\\Open\\Proj\\notes.md"),))
    plan = _plan(_item(source=source, destination="D:\\Open\\Archive\\Proj"))
    assert _reason(plan, history) == floors.REASON_RECENTLY_VACATED


def test_moving_a_sibling_of_a_vacated_place_goes_ahead() -> None:
    history = History(vacated=(vacated_key("D:\\Open\\Proj\\notes.md"),))
    plan = _plan(_item(source="D:\\Open\\Proj2", destination="D:\\Open\\Archive\\Proj2"))
    assert _reason(plan, history) is None


def test_a_sibling_with_a_longer_name_is_not_vacated() -> None:
    assert not is_vacated("D:\\Open\\Gone2", (vacated_key("D:\\Open\\Gone"),))


def test_dotless_i_folds_the_safe_way() -> None:
    """ı（U+0131）只用 casefold 時和 I 不同；先轉大寫就會一樣，寧可多擋。"""
    assert is_vacated("D:\\Open\\ı.txt", (vacated_key("D:\\Open\\I.txt"),))
