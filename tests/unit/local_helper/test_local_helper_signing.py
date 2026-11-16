"""小幫手核對 Touka 的章（ADR-0317）：固定測資和 Touka 那份相同。"""

from __future__ import annotations

import pytest
from touka_local.protocol import Decision
from touka_local.signing import (
    decision_signature_ok,
    sign_decision,
    split_pairing_code,
    text_fingerprint,
)

KEY = bytes(range(32))
DECISION_SIGNATURE = "981856c8c8cc7cf5a9e1f9e8b2bb08e2bdc509345b564545be91801387a4ac9a"


def _decision(**overrides: str) -> Decision:
    values = {
        "nonce": "n" * 22,
        "digest": "a" * 64,
        "shown": "b" * 64,
        "decided_by": "yolo",
        "signature": DECISION_SIGNATURE,
    }
    values.update(overrides)
    return Decision(**values)


def test_signatures_match_the_shared_vectors() -> None:
    assert (
        sign_decision(
            KEY, job_id="job-1", nonce="n" * 22, digest="a" * 64, shown="b" * 64, decided_by="yolo"
        )
        == DECISION_SIGNATURE
    )
    assert (
        text_fingerprint("清單\n")
        == "d7e25ab0c84b7218ce454828510c957e080f3ff7416ff6bd61d6d5d23a27d2b3"
    )


def test_a_good_decision_is_accepted() -> None:
    assert decision_signature_ok(KEY, "job-1", _decision())


@pytest.mark.parametrize(
    ("job_id", "key", "decision"),
    [
        ("job-2", KEY, _decision()),  # 別件工作的章
        ("job-1", bytes(32), _decision()),  # 別把鑰匙蓋的
        ("job-1", None, _decision()),  # 舊配對沒有鑰匙
        ("job-1", KEY, _decision(signature="0" * 64)),
        ("job-1", KEY, _decision(signature=DECISION_SIGNATURE.upper())),
        ("job-1", KEY, _decision(decided_by="helper_window")),
        ("job-1", KEY, _decision(decided_by="")),
        ("job-1", KEY, _decision(nonce="n" * 21)),
        ("job-1", KEY, _decision(digest="A" * 64)),
        ("job-1", KEY, _decision(shown="b" * 63)),
    ],
)
def test_anything_wrong_means_no_decision(
    job_id: str, key: bytes | None, decision: Decision
) -> None:
    assert not decision_signature_ok(key, job_id, decision)


def test_a_new_pairing_code_carries_a_key() -> None:
    token = "t" * 43
    code = f"{token}.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    assert split_pairing_code(code) == (token, KEY)


@pytest.mark.parametrize(
    "code",
    [
        "t" * 43,  # 舊配對碼，沒有鑰匙
        "t" * 43 + ".",  # 點後面空的
        "t" * 43 + ".AAEC",  # 太短
        "t" * 43 + ".AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",  # 帶補齊
        "t" * 43 + ".AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh+",  # 不是 urlsafe
    ],
)
def test_codes_without_a_usable_key(code: str) -> None:
    token, key = split_pairing_code(code)
    assert token == code.split(".", 1)[0]
    assert key is None
