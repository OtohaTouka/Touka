"""照 Touka 的模式核准要用的三個小檔（ADR-0317）：讀不懂一律往安全的那邊算。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from touka_local.settings import (
    load_decision_key,
    load_mode_following,
    load_mode_reset,
    save_decision_key,
    save_mode_following,
    save_mode_reset,
)

if TYPE_CHECKING:
    from pathlib import Path

KEY = bytes(range(32))


def test_decision_key_round_trip_and_removal(tmp_path: Path) -> None:
    file = tmp_path / "decision_key"
    assert load_decision_key(file) is None
    save_decision_key(file, KEY)
    assert load_decision_key(file) == KEY
    save_decision_key(file, None)
    assert load_decision_key(file) is None
    assert not file.exists()


@pytest.mark.parametrize("content", ["", "zz" * 32, "AB" * 32, "ab" * 31, "ab" * 33, "{}"])
def test_a_bad_key_file_means_no_key(tmp_path: Path, content: str) -> None:
    file = tmp_path / "decision_key"
    file.write_text(content, encoding="utf-8")
    assert load_decision_key(file) is None


def test_following_round_trip(tmp_path: Path) -> None:
    file = tmp_path / "mode_following.json"
    save_mode_following(file, enabled=True)
    assert load_mode_following(file) is True
    save_mode_following(file, enabled=False)
    assert load_mode_following(file) is False


@pytest.mark.parametrize(
    "content",
    [
        None,  # 沒有檔案
        "not json",
        '{"enabled": "true"}',
        '{"enabled": 1}',
        '{"enabled": true, "extra": 1}',
        "[true]",
    ],
)
def test_anything_unclear_means_switched_off(tmp_path: Path, content: str | None) -> None:
    file = tmp_path / "mode_following.json"
    if content is not None:
        file.write_text(content, encoding="utf-8")
    assert load_mode_following(file) is False


def test_reset_time_round_trip(tmp_path: Path) -> None:
    file = tmp_path / "mode_reset.json"
    assert load_mode_reset(file) is None
    save_mode_reset(file, 1234.5)
    assert load_mode_reset(file) == 1234.5


@pytest.mark.parametrize(
    "content",
    ["nope", '{"reset_at": "1"}', '{"reset_at": true}', '{"reset_at": NaN}', '{"x": 1}'],
)
def test_an_unreadable_reset_means_no_reset(tmp_path: Path, content: str) -> None:
    file = tmp_path / "mode_reset.json"
    file.write_text(content, encoding="utf-8")
    assert load_mode_reset(file) is None
