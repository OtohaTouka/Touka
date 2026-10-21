"""P8-A OAuth key ring、state HMAC 與 AES-GCM 邊界。"""

from __future__ import annotations

import asyncio
import base64
import importlib

import pytest


def _oauth_keyring():
    return importlib.import_module("src.protocols.mcp.oauth.keyring")


def _encoded_key(kid: str, fill: int) -> str:
    raw = bytes([fill]) * 32
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"v1:{kid}:{encoded}"


def test_key_parser_requires_version_kid_and_exactly_32_bytes():
    module = _oauth_keyring()
    key = module.OAuthKey.parse(_encoded_key("key-a", 7))
    assert key.kid == "key-a"
    assert len(key.material) == 32

    with pytest.raises(module.OAuthKeyFormatError):
        module.OAuthKey.parse("v1:key-a:AA")
    with pytest.raises(module.OAuthKeyFormatError):
        module.OAuthKey.parse(_encoded_key("bad kid", 7))
    with pytest.raises(module.OAuthKeyFormatError):
        module.OAuthKey.parse(_encoded_key("key-a", 7).replace("v1:", "v2:"))


def test_aes_gcm_uses_kid_and_aad_and_never_serializes_plaintext():
    module = _oauth_keyring()
    ring = module.OAuthKeyRing(
        current=module.OAuthKey.parse(_encoded_key("current", 11)),
    )
    sealed = ring.encrypt(b"refresh-token-canary", aad=b"grant:one")

    assert sealed.kid == "current"
    assert "refresh-token-canary" not in sealed.model_dump_json()
    assert ring.decrypt(sealed, aad=b"grant:one") == b"refresh-token-canary"
    with pytest.raises(module.OAuthDecryptionError):
        ring.decrypt(sealed, aad=b"grant:two")


def test_retiring_key_remains_available_for_state_lookup_and_old_ciphertext():
    module = _oauth_keyring()
    old = module.OAuthKey.parse(_encoded_key("old", 13))
    new = module.OAuthKey.parse(_encoded_key("new", 17))
    old_ring = module.OAuthKeyRing(current=old)
    sealed = old_ring.encrypt(b"pending-verifier", aad=b"transaction:t1")
    old_digest = old_ring.state_digest("state-canary")

    rotated = module.OAuthKeyRing(current=new, retiring=(old,))
    candidates = rotated.state_digest_candidates("state-canary")

    assert old_digest in candidates
    assert candidates[0][0] == "new"
    assert rotated.decrypt(sealed, aad=b"transaction:t1") == b"pending-verifier"


def test_duplicate_kid_is_rejected():
    module = _oauth_keyring()
    current = module.OAuthKey.parse(_encoded_key("same", 19))
    retiring = module.OAuthKey.parse(_encoded_key("same", 23))
    with pytest.raises(module.OAuthKeyFormatError):
        module.OAuthKeyRing(current=current, retiring=(retiring,))


@pytest.mark.asyncio
async def test_provider_external_mode_never_generates_ephemeral_key(tmp_path):
    module = _oauth_keyring()
    provider = module.OAuthKeyRingProvider(
        source="external",
        current_value="",
        retiring_json="[]",
        lock_path=tmp_path / "oauth-key.lock",
        bootstrap_enabled=True,
        persist=lambda _key, _value: True,
    )
    with pytest.raises(module.OAuthKeyUnavailableError):
        await provider.load()


@pytest.mark.asyncio
async def test_provider_managed_mode_generates_once_under_cross_process_lock(
    tmp_path,
):
    module = _oauth_keyring()
    persisted: list[str] = []

    def persist(_key: str, value: str) -> bool:
        persisted.append(value)
        return True

    kwargs = {
        "source": "managed",
        "current_value": "",
        "retiring_json": "[]",
        "lock_path": tmp_path / "oauth-key.lock",
        "bootstrap_enabled": True,
        "persist": persist,
        "reload_current": lambda: persisted[-1] if persisted else "",
    }
    first, second = await asyncio.gather(
        module.OAuthKeyRingProvider(**kwargs).load(),
        module.OAuthKeyRingProvider(**kwargs).load(),
    )
    assert len(persisted) == 1
    assert first.current.material == second.current.material


@pytest.mark.asyncio
async def test_provider_managed_mode_fails_closed_when_persistence_is_disabled(tmp_path):
    module = _oauth_keyring()
    provider = module.OAuthKeyRingProvider(
        source="managed",
        current_value="",
        retiring_json="[]",
        lock_path=tmp_path / "oauth-key.lock",
        bootstrap_enabled=False,
        persist=lambda _key, _value: True,
    )
    with pytest.raises(module.OAuthKeyUnavailableError):
        await provider.load()


@pytest.mark.asyncio
async def test_provider_rejects_unbounded_or_malformed_retiring_ring(tmp_path):
    module = _oauth_keyring()
    with pytest.raises(module.OAuthKeyFormatError):
        await module.OAuthKeyRingProvider(
            source="managed",
            current_value=_encoded_key("current", 31),
            retiring_json="not-json",
            lock_path=tmp_path / "oauth-key.lock",
            bootstrap_enabled=False,
            persist=lambda _key, _value: False,
        ).load()
    with pytest.raises(module.OAuthKeyFormatError):
        await module.OAuthKeyRingProvider(
            source="managed",
            current_value=_encoded_key("current", 31),
            retiring_json=str([_encoded_key("old-a", 33), _encoded_key("old-b", 35)]).replace(
                "'", '"'
            ),
            lock_path=tmp_path / "oauth-key.lock",
            bootstrap_enabled=False,
            persist=lambda _key, _value: False,
        ).load()


@pytest.mark.asyncio
async def test_managed_provider_persists_rotation_ring_as_one_atomic_update(tmp_path):
    module = _oauth_keyring()
    values = {
        "TOUKA_MCP_OAUTH_KEY": _encoded_key("old", 41),
        "TOUKA_MCP_OAUTH_RETIRED_KEYS": "[]",
    }
    writes: list[dict[str, str]] = []

    def persist_many(updates: dict[str, str]) -> bool:
        writes.append(dict(updates))
        values.update(updates)
        return True

    provider = module.OAuthKeyRingProvider(
        source="managed",
        current_value=values["TOUKA_MCP_OAUTH_KEY"],
        retiring_json=values["TOUKA_MCP_OAUTH_RETIRED_KEYS"],
        lock_path=tmp_path / "oauth-key.lock",
        bootstrap_enabled=False,
        persist=lambda _key, _value: False,
        persist_many=persist_many,
        reload_values=lambda: dict(values),
    )
    candidate = module.OAuthKey.parse(_encoded_key("new", 43))

    dual = await provider.prepare_rotation(epoch=2, candidate=candidate)

    assert dual.current == candidate
    assert tuple(key.kid for key in dual.retiring) == ("old",)
    assert len(writes) == 1
    assert set(writes[0]) == {
        "TOUKA_MCP_OAUTH_KEY",
        "TOUKA_MCP_OAUTH_RETIRED_KEYS",
        "TOUKA_MCP_OAUTH_ROTATION_PHASE",
        "TOUKA_MCP_OAUTH_KEY_EPOCH",
    }
    reloaded = await provider.reload()
    assert reloaded.current.kid == "new"
    retired = await provider.retire(epoch=2)
    assert retired.current.kid == "new"
    assert retired.retiring == ()
    assert len(writes) == 2
