"""小幫手決定的簽名（ADR-0317）：固定測資和小幫手那份相同，兩邊才對得上。"""

from __future__ import annotations

import pytest
from src.tools.execution_context import APPROVED_BY_AUTO_REVIEWER
from src.utils.local_bridge_signing import (
    DECISION_KEY_BYTES,
    decision_key_from_hex,
    derive_decided_by,
    new_decision_key,
    pairing_code,
    sign_decision,
    sign_open_request,
    text_fingerprint,
)

KEY = bytes(range(32))
DECISION_SIGNATURE = "981856c8c8cc7cf5a9e1f9e8b2bb08e2bdc509345b564545be91801387a4ac9a"
OPEN_SIGNATURE = "9569b051515726882f924cef1c80c0d72907041e28bf9c42df79cc19493f732a"


def test_decision_signature_matches_the_shared_vector() -> None:
    signature = sign_decision(
        KEY,
        job_id="job-1",
        nonce="n" * 22,
        digest="a" * 64,
        shown="b" * 64,
        decided_by="yolo",
    )
    assert signature == DECISION_SIGNATURE


@pytest.mark.parametrize(
    "change",
    [
        {"job_id": "job-2"},
        {"nonce": "m" * 22},
        {"digest": "c" * 64},
        {"shown": "d" * 64},
        {"decided_by": "user_card"},
    ],
)
def test_every_field_is_covered_by_the_signature(change: dict[str, str]) -> None:
    fields = {
        "job_id": "job-1",
        "nonce": "n" * 22,
        "digest": "a" * 64,
        "shown": "b" * 64,
        "decided_by": "yolo",
    }
    assert sign_decision(KEY, **{**fields, **change}) != DECISION_SIGNATURE


def test_open_request_signature_matches_the_shared_vector() -> None:
    signature = sign_open_request(
        KEY, job_id="job-2", folder="D:/Docs", access="read", issued_at=1700000000
    )
    assert signature == OPEN_SIGNATURE


def test_text_fingerprint_is_sha256_of_utf8() -> None:
    assert (
        text_fingerprint("清單\n")
        == "d7e25ab0c84b7218ce454828510c957e080f3ff7416ff6bd61d6d5d23a27d2b3"
    )


def test_new_key_is_32_random_bytes() -> None:
    first, second = new_decision_key(), new_decision_key()
    assert len(first) == DECISION_KEY_BYTES == 32
    assert first != second


def test_pairing_code_carries_the_key_after_a_dot() -> None:
    sample = "tok_en-123"
    code = pairing_code(sample, KEY)
    token, _, key_text = code.partition(".")
    assert token == sample
    assert len(key_text) == 43
    assert "=" not in key_text


@pytest.mark.parametrize("value", [None, "", "zz" * 32, "ab" * 31, "AB" * 32])
def test_bad_stored_keys_are_not_used(value: str | None) -> None:
    assert decision_key_from_hex(value) is None


def test_stored_key_round_trips() -> None:
    assert decision_key_from_hex(KEY.hex()) == KEY


# --- 誰決定的：只看 Touka 內部的可信值 ---


def _decide(**overrides: object) -> str | None:
    values: dict[str, object] = {
        "hitl_preapproved": False,
        "preview_shown_complete": False,
        "resolver_matches": False,
        "approved_by": None,
        "auth_level": "Manual",
    }
    values.update(overrides)
    return derive_decided_by(**values)  # type: ignore[arg-type]


def test_the_paired_person_approving_a_complete_card() -> None:
    assert (
        _decide(hitl_preapproved=True, preview_shown_complete=True, resolver_matches=True)
        == "user_card"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"hitl_preapproved": True, "preview_shown_complete": False, "resolver_matches": True},
        {"hitl_preapproved": True, "preview_shown_complete": True, "resolver_matches": False},
        {"hitl_preapproved": False, "preview_shown_complete": True, "resolver_matches": True},
    ],
)
def test_a_card_counts_only_when_shown_whole_and_pressed_by_the_paired_person(
    overrides: dict[str, object],
) -> None:
    assert _decide(**overrides) is None


def test_the_auto_reviewer_allowing() -> None:
    # utils 不往上匯入服務層，這裡釘住兩邊的值相同
    assert _decide(approved_by=APPROVED_BY_AUTO_REVIEWER, auth_level="Auto") == "auto_review"


def test_yolo_without_a_card_or_reviewer() -> None:
    assert _decide(auth_level="YOLO") == "yolo"


@pytest.mark.parametrize(
    "overrides",
    [
        {"auth_level": "YOLO", "hitl_preapproved": True},
        {"auth_level": "YOLO", "approved_by": "someone"},
        {"auth_level": "Auto"},
        {"auth_level": "Manual"},
        {"auth_level": "yolo"},
        {"approved_by": "auto_reviewer_x", "auth_level": "Auto"},
    ],
)
def test_anything_else_leaves_the_decision_to_the_helper_window(
    overrides: dict[str, object],
) -> None:
    assert _decide(**overrides) is None
