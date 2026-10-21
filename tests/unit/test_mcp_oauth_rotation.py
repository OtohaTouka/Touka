"""P8-A crash-resumable OAuth key rotation state machine."""

from __future__ import annotations

import base64
import importlib

import pytest


def _key(module, kid: str, fill: int):
    encoded = base64.urlsafe_b64encode(bytes([fill]) * 32).decode().rstrip("=")
    return module.OAuthKey.parse(f"v1:{kid}:{encoded}")


class _Provider:
    source = "managed"

    def __init__(self, keyring_module):
        self.old = _key(keyring_module, "old", 7)
        self.new = _key(keyring_module, "new", 9)
        self.ring = keyring_module.OAuthKeyRing(current=self.old)
        self.events: list[tuple[str, int]] = []
        self._keyring_module = keyring_module

    def create_rotation_candidate(self):
        return self.new

    async def prepare_rotation(self, *, epoch: int, candidate):
        assert candidate == self.new
        self.events.append(("prepare", epoch))
        self.ring = self._keyring_module.OAuthKeyRing(
            current=self.new,
            retiring=(self.old,),
        )
        return self.ring

    async def reload(self):
        return self.ring

    async def retire(self, *, epoch: int):
        self.events.append(("retire", epoch))
        self.ring = self._keyring_module.OAuthKeyRing(current=self.new)
        return self.ring


class _PublishThenRaiseProvider(_Provider):
    async def prepare_rotation(self, *, epoch: int, candidate):
        await super().prepare_rotation(epoch=epoch, candidate=candidate)
        message = "simulated lost acknowledgement after atomic publish"
        raise RuntimeError(message)


@pytest.mark.asyncio
async def test_rotation_waits_for_state_ttl_then_retires_without_old_references(tmp_path):
    keyring_module = importlib.import_module("src.protocols.mcp.oauth.keyring")
    models = importlib.import_module("src.protocols.mcp.oauth.models")
    store_module = importlib.import_module("src.protocols.mcp.oauth.store")
    rotation_module = importlib.import_module("src.protocols.mcp.oauth.rotation")
    provider = _Provider(keyring_module)
    store = store_module.OAuthSqliteStore(tmp_path / "oauth.sqlite3", provider.ring)
    await store.prepare_schema()
    slot = models.OAuthAuthorizationSlotKey(
        human_principal_id="human-a",
        execution_agent_id="agent-a",
        installation_id="server-a",
        installation_generation=1,
        auth_profile_id="profile-a",
    )
    payload = models.OAuthTransactionPayload(
        pkce_verifier="verifier-canary",
        requested_scopes=frozenset({"tools.read"}),
        issuer="https://issuer.example.test",
        resource="https://mcp.example.test/mcp",
        redirect_uri="https://proxy.example.test/mcp/oauth/callback",
        redirect_route="/mcp/oauth/callback",
        response_mode="form_post",
        profile_generation=1,
        profile_fingerprint="profile-fingerprint",
        auth_profile_id="profile-a",
        human_principal_id="human-a",
        execution_agent_id="agent-a",
        installation_id="server-a",
        installation_generation=1,
        authorization_endpoint="https://issuer.example.test/authorize",
        token_endpoint="https://issuer.example.test/token",
        code_challenge_methods_supported=("S256",),
        response_modes_supported=("form_post",),
        authorization_response_iss_parameter_supported=True,
        metadata_security_fingerprint="metadata-fingerprint",
    )
    await store.create_transaction(
        slot=slot,
        operation_id="operation-a",
        raw_state="state-canary",
        payload=payload,
        now=1000.0,
        expires_at=1600.0,
    )
    service = rotation_module.OAuthKeyRotationService(
        store=store,
        provider=provider,
        worker_id="worker-a",
        state_ttl_seconds=600.0,
        worker_liveness_seconds=900.0,
        batch_size=100,
    )

    started = await service.start(now=1000.0)
    draining = await service.maintain(now=1001.0)
    before_deadline = await service.maintain(now=1599.999)
    completed = await service.maintain(now=1600.0)

    assert started.phase == models.OAuthKeyRotationPhase.DUAL_READ_NEW_WRITE
    assert draining.phase == models.OAuthKeyRotationPhase.STATE_DRAIN
    assert before_deadline.phase == models.OAuthKeyRotationPhase.STATE_DRAIN
    assert completed.phase == models.OAuthKeyRotationPhase.STABLE
    assert provider.events == [("prepare", 2), ("retire", 2)]
    assert sum((await store.count_key_references("old")).values()) == 0


@pytest.mark.asyncio
async def test_rotation_blocks_retirement_until_every_snapshot_worker_converges(tmp_path):
    keyring_module = importlib.import_module("src.protocols.mcp.oauth.keyring")
    models = importlib.import_module("src.protocols.mcp.oauth.models")
    store_module = importlib.import_module("src.protocols.mcp.oauth.store")
    rotation_module = importlib.import_module("src.protocols.mcp.oauth.rotation")
    provider = _Provider(keyring_module)
    store = store_module.OAuthSqliteStore(tmp_path / "oauth.sqlite3", provider.ring)
    await store.prepare_schema()
    await store.report_worker_epoch("worker-b", epoch=1, now=999.0)
    service = rotation_module.OAuthKeyRotationService(
        store=store,
        provider=provider,
        worker_id="worker-a",
        state_ttl_seconds=0.0,
        worker_liveness_seconds=900.0,
        batch_size=100,
    )

    await service.start(now=1000.0)
    blocked = await service.maintain(now=1000.0)
    assert blocked.phase == models.OAuthKeyRotationPhase.WORKER_CONVERGE
    await store.report_worker_epoch("worker-b", epoch=2, now=1001.0)
    completed = await service.maintain(now=1001.0)

    assert completed.phase == models.OAuthKeyRotationPhase.STABLE
    assert provider.events[-1] == ("retire", 2)


@pytest.mark.asyncio
async def test_prepare_crash_before_key_publish_rolls_back_to_stable(tmp_path):
    keyring_module = importlib.import_module("src.protocols.mcp.oauth.keyring")
    models = importlib.import_module("src.protocols.mcp.oauth.models")
    store_module = importlib.import_module("src.protocols.mcp.oauth.store")
    rotation_module = importlib.import_module("src.protocols.mcp.oauth.rotation")
    provider = _Provider(keyring_module)
    store = store_module.OAuthSqliteStore(tmp_path / "oauth.sqlite3", provider.ring)
    await store.prepare_schema()
    prepared = await store.begin_key_rotation(
        new_kid=provider.new.kid,
        worker_id="worker-a",
        now=1000.0,
        state_ttl_seconds=600.0,
        worker_liveness_seconds=900.0,
    )
    assert prepared.phase == models.OAuthKeyRotationPhase.PREPARE
    service = rotation_module.OAuthKeyRotationService(
        store=store,
        provider=provider,
        worker_id="worker-a",
        state_ttl_seconds=600.0,
        worker_liveness_seconds=900.0,
        batch_size=100,
    )

    recovered = await service.maintain(now=1001.0)

    assert recovered.phase == models.OAuthKeyRotationPhase.STABLE
    assert recovered.current_kid == "old"
    assert recovered.epoch == 1


@pytest.mark.asyncio
async def test_start_reconciles_authoritative_dual_ring_after_lost_publish_ack(tmp_path):
    keyring_module = importlib.import_module("src.protocols.mcp.oauth.keyring")
    models = importlib.import_module("src.protocols.mcp.oauth.models")
    store_module = importlib.import_module("src.protocols.mcp.oauth.store")
    rotation_module = importlib.import_module("src.protocols.mcp.oauth.rotation")
    provider = _PublishThenRaiseProvider(keyring_module)
    store = store_module.OAuthSqliteStore(tmp_path / "oauth.sqlite3", provider.ring)
    await store.prepare_schema()
    service = rotation_module.OAuthKeyRotationService(
        store=store,
        provider=provider,
        worker_id="worker-a",
        state_ttl_seconds=600.0,
        worker_liveness_seconds=900.0,
        batch_size=100,
    )

    recovered = await service.start(now=1000.0)

    assert recovered.phase == models.OAuthKeyRotationPhase.DUAL_READ_NEW_WRITE
    assert recovered.current_kid == "new"
    assert recovered.retiring_kid == "old"


@pytest.mark.asyncio
async def test_rotation_excludes_expired_worker_lease_from_convergence_snapshot(tmp_path):
    keyring_module = importlib.import_module("src.protocols.mcp.oauth.keyring")
    models = importlib.import_module("src.protocols.mcp.oauth.models")
    store_module = importlib.import_module("src.protocols.mcp.oauth.store")
    rotation_module = importlib.import_module("src.protocols.mcp.oauth.rotation")
    provider = _Provider(keyring_module)
    store = store_module.OAuthSqliteStore(tmp_path / "oauth.sqlite3", provider.ring)
    await store.prepare_schema()
    await store.report_worker_epoch("dead-worker", epoch=1, now=900.0)
    service = rotation_module.OAuthKeyRotationService(
        store=store,
        provider=provider,
        worker_id="worker-a",
        state_ttl_seconds=0.0,
        worker_liveness_seconds=30.0,
        batch_size=100,
    )

    await service.start(now=1000.0)
    completed = await service.maintain(now=1000.0)

    assert completed.phase == models.OAuthKeyRotationPhase.STABLE
    assert provider.events[-1] == ("retire", 2)
