"""小幫手核對 Touka 的章（ADR-0317）：固定測資和 Touka 那份相同。"""

from __future__ import annotations

import pytest
from touka_local.protocol import Decision
from touka_local.signing import (
    decision_signature_ok,
    open_request_signature_ok,
    sign_decision,
    sign_open_request,
    split_pairing_code,
    text_fingerprint,
)

KEY = bytes(range(32))
DECISION_SIGNATURE = "981856c8c8cc7cf5a9e1f9e8b2bb08e2bdc509345b564545be91801387a4ac9a"
# 開放請求的章（ADR-0320），和 Touka 那份相同；路徑不以磁碟代號開頭，Touka 端的測試才能共用
OPEN_PATH = "相片/家 2026"
OPEN_NONCE = "A" * 22
OPEN_SIGNATURE = "7873824c0b7a87298dfa71848a8895bceca3d793394974aebf8e6e0b38bffb9f"


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


# --- 開放請求的章（ADR-0320） ---


def _open_decision(**overrides: str) -> Decision:
    fingerprint = text_fingerprint(OPEN_PATH)
    values = {
        "nonce": OPEN_NONCE,
        "digest": fingerprint,
        "shown": fingerprint,
        "decided_by": "owner_request",
        "signature": OPEN_SIGNATURE,
    }
    values.update(overrides)
    return Decision(**values)


def test_open_signature_matches_the_shared_vector() -> None:
    assert sign_open_request(KEY, job_id="job-1", nonce=OPEN_NONCE, path=OPEN_PATH) == (
        OPEN_SIGNATURE
    )


def test_a_good_open_request_is_accepted() -> None:
    assert open_request_signature_ok(KEY, "job-1", OPEN_PATH, _open_decision())


@pytest.mark.parametrize(
    ("job_id", "path", "key", "decision"),
    [
        ("job-2", OPEN_PATH, KEY, _open_decision()),
        ("job-1", "相片/家 2027", KEY, _open_decision()),
        ("job-1", OPEN_PATH, None, _open_decision()),
        ("job-1", OPEN_PATH, bytes(32), _open_decision()),
        ("job-1", OPEN_PATH, KEY, _open_decision(decided_by="yolo")),
        ("job-1", OPEN_PATH, KEY, _open_decision(nonce="B" * 22)),
        ("job-1", OPEN_PATH, KEY, _open_decision(shown="0" * 64)),
        ("job-1", OPEN_PATH, KEY, _open_decision(digest="0" * 64)),
    ],
)
def test_anything_wrong_means_no_open_request(
    job_id: str, path: str, key: bytes | None, decision: Decision
) -> None:
    assert not open_request_signature_ok(key, job_id, path, decision)


def test_the_two_kinds_of_stamp_differ() -> None:
    fingerprint = text_fingerprint(OPEN_PATH)
    as_list = sign_decision(
        KEY,
        job_id="job-1",
        nonce=OPEN_NONCE,
        digest=fingerprint,
        shown=fingerprint,
        decided_by="owner_request",
    )
    assert as_list != OPEN_SIGNATURE
    assert not decision_signature_ok(KEY, "job-1", _open_decision())
