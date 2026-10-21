"""P8-A 非秘密 OAuth profile 與 scope policy SSOT。"""

from __future__ import annotations

import asyncio
import importlib
import ipaddress
import threading

import pytest
from pydantic import ValidationError


def _profile_module():
    return importlib.import_module("src.protocols.mcp.oauth.profile")


def _profile_payload() -> dict[str, object]:
    return {
        "profile_id": "work",
        "generation": 1,
        "enabled": True,
        "callback_binding": "form_post",
        "trusted_public_origin": "https://chat.example.test",
        "trusted_issuers": ["https://issuer.example.test"],
        "client_id": "https://chat.example.test/oauth/client.json",
        "scope_rules": [
            {"operation": "tools/call", "selector": "search", "scopes": ["tools.read"]},
            {"operation": "resources/read", "selector": "*", "scopes": ["resources.read"]},
        ],
    }


def test_profile_is_non_secret_strict_and_https_bound():
    module = _profile_module()
    profile = module.OAuthProfile.model_validate(_profile_payload())
    dumped = profile.model_dump(mode="json")

    assert dumped["generation"] == 1
    assert "client_secret" not in repr(dumped)
    assert profile.trusted_public_origin == "https://chat.example.test"

    bad = _profile_payload()
    bad["trusted_public_origin"] = "http://chat.example.test"
    with pytest.raises(ValidationError):
        module.OAuthProfile.model_validate(bad)


def test_private_network_exception_is_exact_installation_origin_and_cidr() -> None:
    module = _profile_module()
    payload = _profile_payload()
    payload["private_network_exceptions"] = [
        {
            "installation_id": "remote-a",
            "origin": "https://issuer.internal.example",
            "cidrs": ["10.20.0.0/24"],
        }
    ]
    profile = module.OAuthProfile.model_validate(payload)

    assert profile.allowed_private_cidrs(
        "remote-a",
        "https://issuer.internal.example/token",
    ) == (ipaddress.ip_network("10.20.0.0/24"),)
    assert (
        profile.allowed_private_cidrs(
            "remote-b",
            "https://issuer.internal.example/token",
        )
        == ()
    )
    assert (
        profile.allowed_private_cidrs(
            "remote-a",
            "https://sibling.internal.example/token",
        )
        == ()
    )


@pytest.mark.parametrize(
    "exception",
    [
        {
            "installation_id": "remote-a",
            "origin": "http://issuer.internal.example",
            "cidrs": ["10.20.0.0/24"],
        },
        {
            "installation_id": "remote-a",
            "origin": "https://issuer.internal.example/path",
            "cidrs": ["10.20.0.0/24"],
        },
        {
            "installation_id": "remote-a",
            "origin": "https://issuer.internal.example",
            "cidrs": ["0.0.0.0/0"],
        },
    ],
)
def test_private_network_exception_rejects_unsafe_shape(exception: object) -> None:
    module = _profile_module()
    payload = _profile_payload()
    payload["private_network_exceptions"] = [exception]
    with pytest.raises(ValidationError):
        module.OAuthProfile.model_validate(payload)


def test_scope_policy_exact_selector_precedes_operation_wildcard():
    module = _profile_module()
    profile = module.OAuthProfile.model_validate(_profile_payload())

    assert profile.required_scopes("tools/call", "search") == frozenset({"tools.read"})
    assert profile.required_scopes("tools/call", "Search") is None
    assert profile.required_scopes("resources/read", "mcp://a/../b") == frozenset(
        {"resources.read"}
    )
    assert profile.required_scopes("prompts/get", "prompt-a") is None


@pytest.mark.parametrize(
    "rule",
    [
        {"operation": "unknown", "selector": "*", "scopes": ["a"]},
        {"operation": "tools/call", "selector": "prefix*", "scopes": ["a"]},
        {"operation": "tools/call", "selector": "", "scopes": ["a"]},
        {"operation": "tools/call", "selector": "x", "scopes": []},
    ],
)
def test_scope_policy_rejects_unknown_operation_glob_empty_selector_or_scope(rule):
    module = _profile_module()
    payload = _profile_payload()
    payload["scope_rules"] = [rule]
    with pytest.raises(ValidationError):
        module.OAuthProfile.model_validate(payload)


def test_scope_policy_rejects_duplicate_exact_or_wildcard_rules():
    module = _profile_module()
    payload = _profile_payload()
    payload["scope_rules"] = [
        {"operation": "tools/call", "selector": "same", "scopes": ["a"]},
        {"operation": "tools/call", "selector": "same", "scopes": ["b"]},
    ]
    with pytest.raises(ValidationError):
        module.OAuthProfile.model_validate(payload)


def test_returned_scope_must_be_subset_and_refresh_omission_preserves_existing():
    module = _profile_module()
    requested = frozenset({"tools.read", "profile.read"})

    assert module.validate_returned_scopes(None, requested, refresh=False) == requested
    assert module.validate_returned_scopes(None, requested, refresh=True) == requested
    assert module.validate_returned_scopes("tools.read", requested, refresh=False) == frozenset(
        {"tools.read"}
    )
    with pytest.raises(module.OAuthScopeError):
        module.validate_returned_scopes("tools.read admin", requested, refresh=False)


@pytest.mark.asyncio
async def test_profile_repository_roundtrip_and_generation_cas(tmp_path):
    module = _profile_module()
    repository = module.OAuthProfileRepository(tmp_path / "profiles.json")
    profile = module.OAuthProfile.model_validate(_profile_payload())

    assert await repository.list_profiles() == []
    assert await repository.put(profile) == profile
    assert await repository.get("work") == profile

    stale = profile.model_copy(update={"enabled": False})
    with pytest.raises(module.OAuthProfileConflictError):
        await repository.put(stale)
    updated = profile.model_copy(update={"generation": 2, "enabled": False})
    assert await repository.put(updated) == updated
    assert (await repository.get("work")).generation == 2


@pytest.mark.asyncio
async def test_profile_repository_rejects_corrupt_document(tmp_path):
    module = _profile_module()
    path = tmp_path / "profiles.json"
    path.write_text("{broken", encoding="utf-8")
    repository = module.OAuthProfileRepository(path)
    with pytest.raises(module.OAuthProfileDocumentError):
        await repository.list_profiles()


@pytest.mark.asyncio
async def test_profile_repository_serializes_concurrent_initial_create(tmp_path):
    module = _profile_module()
    path = tmp_path / "profiles.json"
    first = module.OAuthProfileRepository(path)
    second = module.OAuthProfileRepository(path)
    profile = module.OAuthProfile.model_validate(_profile_payload())

    results = await asyncio.gather(
        first.put(profile),
        second.put(profile),
        return_exceptions=True,
    )
    assert sum(isinstance(result, module.OAuthProfileConflictError) for result in results) == 1
    assert sum(isinstance(result, module.OAuthProfile) for result in results) == 1


def test_profile_fingerprint_is_canonical_and_secret_free():
    module = _profile_module()
    profile = module.OAuthProfile.model_validate(_profile_payload())
    reordered = module.OAuthProfile.model_validate(dict(reversed(list(_profile_payload().items()))))
    assert module.oauth_profile_fingerprint(profile) == module.oauth_profile_fingerprint(reordered)
    assert len(module.oauth_profile_fingerprint(profile)) == 64


def test_dcr_profile_requires_named_trusted_issuer_and_allows_no_static_client():
    module = _profile_module()
    payload = _profile_payload()
    payload["client_id"] = None
    payload["dcr_enabled"] = True
    payload["dcr_trusted_issuers"] = ["https://issuer.example.test"]
    profile = module.OAuthProfile.model_validate(payload)
    assert profile.client_id is None

    payload["dcr_trusted_issuers"] = []
    with pytest.raises(ValidationError):
        module.OAuthProfile.model_validate(payload)


def test_profile_without_static_cimd_or_dcr_client_is_invalid():
    module = _profile_module()
    payload = _profile_payload()
    payload["client_id"] = None
    with pytest.raises(ValidationError):
        module.OAuthProfile.model_validate(payload)


@pytest.mark.asyncio
async def test_slow_profile_io_never_blocks_event_loop_or_cancellation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _profile_module()
    entered = threading.Event()
    release = threading.Event()

    def slow_exists(_path: object) -> bool:
        entered.set()
        assert release.wait(timeout=5)
        return False

    monkeypatch.setattr(module.resource_manager, "exists", slow_exists)
    repository = module.OAuthProfileRepository(tmp_path / "profiles.json")
    task = asyncio.create_task(repository.get("work"))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=5)
        for _turn in range(20):
            await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    finally:
        release.set()
