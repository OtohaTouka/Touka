"""Unit tests for H5/M6 security hardening (TDD).

H5 — Heartbeat Token Hardcoded: config.py HEARTBEAT_TOKEN default must NOT be
     a publicly known value; empty string forces environment variable usage.
M6 — Token Plaintext Storage: StorageService must generate a cryptographically
     random token on first boot instead of the public known default, and must
     persist it so it survives restarts.
"""

from __future__ import annotations

import json

from src.config import settings

from ToukaHeartbeat.storage_service import StorageService

KNOWN_INSECURE_TOKEN = "touka_heartbeat_secret_2026"  # noqa: S105


# ---------------------------------------------------------------------------
# H5 — config.py HEARTBEAT_TOKEN default
# ---------------------------------------------------------------------------

def test_config_heartbeat_token_default_is_not_insecure():
    """HEARTBEAT_TOKEN default in config.py must NOT be the public known value."""
    # Access model_fields from the class (not instance) per Pydantic v2.11+ guidance
    default_value = settings.__class__.model_fields["HEARTBEAT_TOKEN"].default
    assert default_value != KNOWN_INSECURE_TOKEN, (
        f"HEARTBEAT_TOKEN default must not be the known-insecure value "
        f"'{KNOWN_INSECURE_TOKEN}'"
    )


# ---------------------------------------------------------------------------
# M6 — StorageService: random token on first boot
# ---------------------------------------------------------------------------

def test_storage_service_generates_random_token_when_no_config(tmp_path):
    """When config.json does not exist, load_config() must generate a random token."""
    config_file = tmp_path / "config.json"
    svc = StorageService(config_file)

    config = svc.load_config()

    token = config.get("heartbeat_token", "")
    assert token != KNOWN_INSECURE_TOKEN, (
        "StorageService must not use the known-insecure default token"
    )
    assert len(token) >= 16, "Generated token must be at least 16 characters"


def test_storage_service_persists_generated_token(tmp_path):
    """The randomly generated token must be saved to disk on first boot."""
    config_file = tmp_path / "config.json"
    svc = StorageService(config_file)

    config = svc.load_config()
    first_token = config["heartbeat_token"]

    assert config_file.exists(), "Config file must be created on first boot"

    # Read the file directly and verify the token is persisted
    on_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert on_disk.get("heartbeat_token") == first_token, (
        "The generated token must be written to disk"
    )


def test_storage_service_reuses_token_across_restarts(tmp_path):
    """Subsequent calls to load_config() must return the SAME token (not regenerate)."""
    config_file = tmp_path / "config.json"

    svc1 = StorageService(config_file)
    first_token = svc1.load_config()["heartbeat_token"]

    # Simulate restart with a fresh StorageService instance
    svc2 = StorageService(config_file)
    second_token = svc2.load_config()["heartbeat_token"]

    assert first_token == second_token, (
        "Token must not be regenerated on restart; it should be loaded from disk"
    )


def test_storage_service_preserves_existing_token(tmp_path):
    """If config.json already exists with a custom token, it must not be overwritten."""
    config_file = tmp_path / "config.json"
    custom_token = "my-custom-secure-token-xyz"  # noqa: S105

    # Pre-seed the config with a custom token
    config_file.write_text(
        json.dumps({"heartbeat_token": custom_token, "schedules": []}),
        encoding="utf-8",
    )

    svc = StorageService(config_file)
    loaded = svc.load_config()

    assert loaded["heartbeat_token"] == custom_token, (
        "Existing custom token must not be replaced by a new random one"
    )


def test_storage_service_includes_required_fields(tmp_path):
    """Generated initial config must contain api_base_url and schedules fields."""
    config_file = tmp_path / "config.json"
    svc = StorageService(config_file)
    config = svc.load_config()

    assert "api_base_url" in config
    assert "schedules" in config
    assert isinstance(config["schedules"], list)
