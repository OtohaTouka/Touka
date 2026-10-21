"""P8-A OAuth authoritative .env ring persistence."""

from __future__ import annotations

from src import config


def test_oauth_ring_values_are_persisted_by_one_atomic_file_replacement(
    tmp_path,
    monkeypatch,
):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "UNCHANGED=value\nTOUKA_MCP_OAUTH_KEY=old\nTOUKA_MCP_OAUTH_RETIRED_KEYS=[]\n",
        encoding="utf-8",
    )
    applied: list[dict[str, str]] = []
    replacements: list[tuple[object, object]] = []
    original_replace = config.os.replace
    monkeypatch.setattr(config, "resolve_env_file_path", lambda: env_path)
    monkeypatch.setattr(config, "is_bootstrap_enabled", lambda: True)
    monkeypatch.setattr(config, "apply_runtime_env", lambda values: applied.append(values))

    def replace(source, destination):
        replacements.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr(config.os, "replace", replace)

    result = config.persist_generated_secrets(
        {
            "TOUKA_MCP_OAUTH_KEY": "new",
            "TOUKA_MCP_OAUTH_RETIRED_KEYS": '["old"]',
            "TOUKA_MCP_OAUTH_ROTATION_PHASE": "dual-read-new-write",
            "TOUKA_MCP_OAUTH_KEY_EPOCH": "2",
        }
    )

    content = env_path.read_text(encoding="utf-8")
    assert result is True
    assert len(replacements) == 1
    assert content.count("TOUKA_MCP_OAUTH_KEY=") == 1
    assert "UNCHANGED=value" in content
    assert 'TOUKA_MCP_OAUTH_RETIRED_KEYS=["old"]' in content
    assert applied == [
        {
            "TOUKA_MCP_OAUTH_KEY": "new",
            "TOUKA_MCP_OAUTH_RETIRED_KEYS": '["old"]',
            "TOUKA_MCP_OAUTH_ROTATION_PHASE": "dual-read-new-write",
            "TOUKA_MCP_OAUTH_KEY_EPOCH": "2",
        }
    ]
