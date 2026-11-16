"""小幫手決定用的金鑰與配對碼（ADR-0317）。"""

from __future__ import annotations

import pytest
from src.utils.local_bridge_signing import (
    DECISION_KEY_BYTES,
    decision_key_from_hex,
    new_decision_key,
    pairing_code,
)

KEY = bytes(range(32))


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
