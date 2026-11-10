"""本機小幫手配對（ADR-0311）：權杖只存雜湊、換新與解除、讀不到就算沒配對。"""

from __future__ import annotations

from pathlib import Path

import pytest
from src.services.local_bridge_pairing import LocalPairingStore
from src.services.resource_manager import resource_manager
from src.utils.local_bridge_token import hash_token, new_token, token_matches


def _store(tmp_path: Path) -> LocalPairingStore:
    return LocalPairingStore(path=tmp_path / "local_bridge" / "pairing.json")


def test_token_helpers() -> None:
    token = new_token()
    assert len(token) >= 40
    assert new_token() != token
    assert token_matches(token, hash_token(token)) is True
    assert token_matches(token + "x", hash_token(token)) is False
    assert token_matches("", hash_token(token)) is False
    assert token_matches("非 ascii", hash_token(token)) is False
    assert token_matches(token, "") is False
    assert token_matches("", hash_token("")) is False


@pytest.mark.asyncio
async def test_create_stores_only_a_hash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    token = await store.create("Owner")
    text = (tmp_path / "local_bridge" / "pairing.json").read_text(encoding="utf-8")
    assert token not in text
    assert hash_token(token) in text
    current = store.current()
    assert current is not None
    assert current.human_id == "Owner"
    assert current.generation == 1
    assert store.authenticate(token) == current


@pytest.mark.asyncio
async def test_rotation_voids_the_old_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = await store.create("Owner")
    new = await store.create("Owner")
    assert store.authenticate(old) is None
    current = store.authenticate(new)
    assert current is not None and current.generation == 2


@pytest.mark.asyncio
async def test_revoke_keeps_generation_moving(tmp_path: Path) -> None:
    store = _store(tmp_path)
    token = await store.create("Owner")
    await store.revoke()
    assert store.generation() == 2
    assert store.current() is None
    assert store.authenticate(token) is None
    await store.create("Owner")
    current = store.current()
    assert current is not None and current.generation == 3


@pytest.mark.asyncio
async def test_revoke_without_pairing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.revoke()
    assert store.generation() == 1
    assert store.current() is None


@pytest.mark.asyncio
async def test_survives_restart(tmp_path: Path) -> None:
    token = await _store(tmp_path).create("Owner")
    reopened = _store(tmp_path)
    current = reopened.authenticate(token)
    assert current is not None and current.human_id == "Owner"


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"generation": 1}',
        '{"generation": "x", "token_hash": "a", "human_id": "b"}',
        '{"generation": 1, "token_hash": "short", "human_id": "b"}',
        '{"generation": 1, "token_hash": null, "human_id": "b"}',
    ],
)
def test_unusable_file_means_not_paired(tmp_path: Path, content: str) -> None:
    path = tmp_path / "local_bridge" / "pairing.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    store = LocalPairingStore(path=path)
    assert store.current() is None
    assert store.authenticate("anything") is None


def test_missing_file_means_not_paired(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.current() is None
    assert store.generation() == 0


@pytest.mark.asyncio
async def test_write_failure_keeps_previous_pairing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    token = await store.create("Owner")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(resource_manager, "write_json", boom)
    with pytest.raises(OSError):  # noqa: PT011 - 這裡只確定寫入失敗往外丟
        await store.create("Owner")
    assert store.authenticate(token) is not None
