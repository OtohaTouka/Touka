"""區例外紀錄的自我測試（ADR-0293 R13）：宣告的範圍、類別比對、沒宣告的留到測試結束。

conftest 的 autouse fixture 在測試結束時把這個測試留下的紀錄當失敗；這裡每個測試都把自己
建立的紀錄宣告掉，所以本身是綠的。
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from memory_server.utils.no_follow import LinkRefusedError, ZoneLinkRefusedError
from memory_server.utils.zone_paths import (
    Anchor,
    ZonePath,
    ZonePathRequiredError,
    ZoneSegmentError,
)

from tests.support import zone_records
from tests.support.zone_records import expect_zone_refusal

_ZP = ZonePath(Anchor.PERSONAL_DRIVE, ("Alice", "notes.txt"))


def test_a_declared_refusal_is_consumed() -> None:
    with expect_zone_refusal(ZoneSegmentError), pytest.raises(ZoneSegmentError):
        _ZP.join_rel("../../Bob")


def test_a_declaration_with_no_refusal_fails() -> None:
    with (
        pytest.raises(pytest.fail.Exception, match="got 0"),
        expect_zone_refusal(ZonePathRequiredError),
    ):
        pass


def test_the_count_must_be_in_range() -> None:
    with (
        pytest.raises(pytest.fail.Exception, match="got 2"),
        expect_zone_refusal(ZoneSegmentError, at_most=1),
    ):
        for _ in range(2):
            with pytest.raises(ZoneSegmentError):
                ZonePath(Anchor.PERSONAL_DRIVE, ("..",))


def test_a_parent_class_covers_its_subclasses() -> None:
    """``LinkRefusedError`` 的宣告也收 ``ZoneLinkRefusedError``；不同類別的照樣留著."""
    # 外層收下內層留下的那一筆
    with expect_zone_refusal(ZonePathRequiredError), expect_zone_refusal(LinkRefusedError):
        ZoneLinkRefusedError(_ZP.display(), None)
        with pytest.raises(ZonePathRequiredError):
            str(_ZP)


def test_a_class_name_also_works() -> None:
    with expect_zone_refusal("ZoneLinkRefusedError", display_prefix="zone://personal_drive/"):
        ZoneLinkRefusedError(_ZP.display(), None)


def test_the_display_prefix_narrows_the_match() -> None:
    with (
        expect_zone_refusal(ZoneLinkRefusedError, display_prefix="zone://personal_drive/Alice"),
        pytest.raises(pytest.fail.Exception, match="got 0"),
        expect_zone_refusal(ZoneLinkRefusedError, display_prefix="zone://custom/"),
    ):
        ZoneLinkRefusedError(_ZP.display(), None)


def test_a_lazily_formatted_log_record_is_caught() -> None:
    """``logger.debug("%s", zp)``：conftest 把 logger 設成 DEBUG、格式化時記下誤用.

    pytest 的 log 擷取遇到格式化錯誤會往上丟，所以測試本身也會紅。
    """
    with expect_zone_refusal(ZonePathRequiredError), pytest.raises(ZonePathRequiredError):
        logging.getLogger("src.services.example").debug("drive %s", _ZP)


async def test_a_refusal_in_a_thread_keeps_the_test_id() -> None:
    def refuse() -> None:
        ZoneLinkRefusedError(_ZP.display(), None)

    with expect_zone_refusal(ZoneLinkRefusedError):
        await asyncio.to_thread(refuse)


def test_records_of_another_test_are_not_taken() -> None:
    with expect_zone_refusal(ZoneSegmentError, at_most=1):
        ZoneSegmentError(None)
        token = zone_records.begin("other::test")
        ZoneSegmentError(None)  # 另一個測試的：宣告結束時還在
    left = zone_records.end("other::test", token)
    assert [r.test_id for r in left] == ["other::test"]
