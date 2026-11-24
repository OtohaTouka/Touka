"""小幫手退到背景：什麼時候顯示、叫出或浮出主視窗（ADR-0312 修訂，2026-11）。

這些是純函式，CI 的 Linux 也能測；真的 Tk 視窗在 test_local_helper_real_windows.py。
"""

from __future__ import annotations

import pytest
from touka_local import presence
from touka_local.presence import (
    ALREADY_OPEN,
    ATTENTION_COOLDOWN_SECONDS,
    BACKGROUND_FLAG,
    HIDE,
    MINIMIZE,
    NONE,
    QUIET,
    REASON_OPEN_PAUSED,
    REASON_RECORDS_UNREADABLE,
    SHOW,
    SHOWN,
    SURFACE,
    AttentionTracker,
    attention_reasons,
    close_button_action,
    second_instance_action,
    visible_at_start,
    wants_background,
    window_action,
)
from touka_local.service import (
    STATE_EXPOSED,
    STATE_OFFLINE,
    STATE_ONLINE,
    STATE_STARTING,
    STATE_UNPAIRED,
    STATE_UPGRADE_NEEDED,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- 啟動 ---


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], False),
        ([BACKGROUND_FLAG], True),
        (["--other", BACKGROUND_FLAG], True),
        (["--background=1"], False),
        (["--BACKGROUND"], False),
    ],
)
def test_only_the_exact_flag_means_background(argv: list[str], expected: bool) -> None:
    assert wants_background(argv) is expected


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("show_ready", [False, True])
def test_the_main_window_hides_only_when_it_can_be_shown_again(
    background: bool, paired: bool, show_ready: bool
) -> None:
    # 開機啟動、已配對、而且之後叫得出來，才藏起來；其他情況照舊顯示
    hidden = background and paired and show_ready
    assert visible_at_start(background=background, paired=paired, show_ready=show_ready) is (
        not hidden
    )


@pytest.mark.parametrize(
    ("background", "signalled", "expected"),
    [
        (True, False, QUIET),
        (True, True, QUIET),
        (False, True, SHOWN),
        (False, False, ALREADY_OPEN),
    ],
)
def test_a_second_copy_shows_the_first_or_says_it_is_open(
    background: bool, signalled: bool, expected: str
) -> None:
    assert second_instance_action(background=background, signalled=signalled) == expected


# --- 需要人處理的狀況 ---


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (STATE_UNPAIRED, STATE_UNPAIRED),
        (STATE_UPGRADE_NEEDED, STATE_UPGRADE_NEEDED),
        (STATE_EXPOSED, STATE_EXPOSED),
    ],
)
def test_service_states_that_need_a_person(state: str, reason: str) -> None:
    assert attention_reasons(state=state, open_paused=False, records_readable=True) == {reason}


@pytest.mark.parametrize("state", [STATE_ONLINE, STATE_OFFLINE, STATE_STARTING])
def test_normal_states_need_nobody(state: str) -> None:
    # Touka 沒開（連不上）是平常的事，不叫人
    assert attention_reasons(state=state, open_paused=False, records_readable=True) == set()


def test_paused_open_requests_and_unreadable_records_need_a_person() -> None:
    reasons = attention_reasons(state=STATE_ONLINE, open_paused=True, records_readable=False)
    assert reasons == {REASON_OPEN_PAUSED, REASON_RECORDS_UNREADABLE}


def test_a_new_condition_is_raised_once() -> None:
    tracker = AttentionTracker(_Clock())
    assert tracker.check(frozenset({STATE_UNPAIRED}))
    assert not tracker.check(frozenset({STATE_UNPAIRED}))


def test_a_condition_that_stays_is_never_raised_again() -> None:
    clock = _Clock()
    tracker = AttentionTracker(clock)
    assert tracker.check(frozenset({STATE_UNPAIRED}))
    for _ in range(5):
        clock.now += ATTENTION_COOLDOWN_SECONDS
        assert not tracker.check(frozenset({STATE_UNPAIRED}))


def test_a_condition_coming_back_soon_is_not_raised_again() -> None:
    # Touka 能讓「版本不符」一下有一下沒有；冷卻內回來不再浮出
    clock = _Clock()
    tracker = AttentionTracker(clock)
    assert tracker.check(frozenset({STATE_UPGRADE_NEEDED}))
    clock.now += 1
    assert not tracker.check(frozenset())
    clock.now += ATTENTION_COOLDOWN_SECONDS - 2
    assert not tracker.check(frozenset({STATE_UPGRADE_NEEDED}))
    # 之後一直在也不再提醒（刻意的取捨，主視窗打開仍看得到）
    clock.now += ATTENTION_COOLDOWN_SECONDS
    assert not tracker.check(frozenset({STATE_UPGRADE_NEEDED}))


def test_a_condition_coming_back_after_the_cooldown_is_raised_again() -> None:
    clock = _Clock()
    tracker = AttentionTracker(clock)
    assert tracker.check(frozenset({STATE_EXPOSED}))
    clock.now += 1
    assert not tracker.check(frozenset())
    clock.now += ATTENTION_COOLDOWN_SECONDS
    assert tracker.check(frozenset({STATE_EXPOSED}))


def test_several_new_conditions_are_raised_together() -> None:
    tracker = AttentionTracker(_Clock())
    assert tracker.check(frozenset({STATE_EXPOSED, REASON_OPEN_PAUSED}))
    assert not tracker.check(frozenset({STATE_EXPOSED, REASON_OPEN_PAUSED}))


def test_a_different_new_condition_is_raised_even_inside_another_cooldown() -> None:
    clock = _Clock()
    tracker = AttentionTracker(clock)
    assert tracker.check(frozenset({STATE_EXPOSED}))
    clock.now += 1
    assert tracker.check(frozenset({STATE_EXPOSED, REASON_RECORDS_UNREADABLE}))


# --- 每一輪要對主視窗做什麼 ---


@pytest.mark.parametrize("attention", [False, True])
@pytest.mark.parametrize("withdrawn", [False, True])
def test_a_request_to_show_wins(attention: bool, withdrawn: bool) -> None:
    assert window_action(show_requested=True, attention=attention, withdrawn=withdrawn) == SHOW


def test_a_condition_only_surfaces_a_hidden_window() -> None:
    assert window_action(show_requested=False, attention=True, withdrawn=True) == SURFACE
    # 主視窗開著時不動；tracker 已記下提醒過，之後按 × 也不會馬上又跑回來
    assert window_action(show_requested=False, attention=True, withdrawn=False) == NONE
    assert window_action(show_requested=False, attention=False, withdrawn=True) == NONE


def test_a_condition_raised_while_the_window_was_open_does_not_surface_later() -> None:
    clock = _Clock()
    tracker = AttentionTracker(clock)
    reasons = frozenset({STATE_UNPAIRED})
    first = window_action(show_requested=False, attention=tracker.check(reasons), withdrawn=False)
    assert first == NONE
    clock.now += 1
    after_hiding = window_action(
        show_requested=False, attention=tracker.check(reasons), withdrawn=True
    )
    assert after_hiding == NONE


@pytest.mark.parametrize(("show_ready", "expected"), [(True, HIDE), (False, MINIMIZE)])
def test_the_close_button_never_strands_the_helper(show_ready: bool, expected: str) -> None:
    # 叫不出主視窗時，× 只最小化，留在工作列
    assert close_button_action(show_ready=show_ready) == expected


def test_the_cooldown_is_ten_minutes() -> None:
    assert presence.ATTENTION_COOLDOWN_SECONDS == 600
