"""Heartbeat contracts and errors have one pure, deterministically projected source."""

import ast
import json
from pathlib import Path

import openapi_merge
import pytest
import sync_errors
import sync_heartbeat_models
from src.core import exceptions
from src.core.bootstrap import hydrate_config_examples
from src.models.session_persistence import TRANSPORT_MODELS

from ToukaHeartbeat.error_codes import ERRORS

ROOT = Path(__file__).resolve().parents[2]


def test_proxy_contract_is_exact_generated_projection_without_runtime_imports() -> None:
    source = ROOT / "ToukaHeartbeat" / "models.py"
    projection = ROOT / "ToukaProxy" / "src" / "models" / "heartbeat_schedule.py"
    assert projection.is_file(), "Proxy pure generated contract is missing"
    assert ast.dump(ast.parse(projection.read_text(encoding="utf-8"))) == ast.dump(
        ast.parse(source.read_text(encoding="utf-8"))
    )
    allowed = {"typing", "pydantic", "json", "hashlib"}
    for node in ast.walk(ast.parse(projection.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed
        elif isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)


def test_model_projection_check_rejects_drift_without_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "models.py"
    projection = tmp_path / "projection.py"
    source.write_bytes(sync_heartbeat_models.SOURCE.read_bytes())
    monkeypatch.setattr(sync_heartbeat_models, "SOURCE", source)
    monkeypatch.setattr(sync_heartbeat_models, "OUTPUT", projection)
    sync_heartbeat_models.sync()
    assert hasattr(sync_heartbeat_models, "check"), "projection needs a read-only drift check"
    sync_heartbeat_models.check()
    drift = projection.read_text(encoding="utf-8").replace("pulse_count: int", "pulse_count: str")
    assert drift != projection.read_text(encoding="utf-8")
    projection.write_text(drift, encoding="utf-8")
    before = projection.read_bytes()
    with pytest.raises(ValueError, match="drift"):
        sync_heartbeat_models.check()
    assert projection.read_bytes() == before


def test_heartbeat_error_projection_matches_backend_registry() -> None:
    definitions = json.loads(
        (ROOT / "ToukaProxy" / "config" / "errors.json.example").read_text(encoding="utf-8")
    )
    assert {
        key: value for key, value in definitions.items() if key.startswith("HEARTBEAT_")
    } == ERRORS


def test_clean_example_bootstrap_supplies_all_new_error_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "shipped"
    source.mkdir()
    example = ROOT / "ToukaProxy" / "config" / "errors.json.example"
    (source / example.name).write_bytes(example.read_bytes())
    target = tmp_path / "owned-config"
    hydrate_config_examples(source, target)
    monkeypatch.setattr(exceptions, "ERRORS_JSON_PATH", target / "errors.json")
    definitions = exceptions.load_error_definitions()
    projected = {key: value for key, value in definitions.items() if key.startswith("HEARTBEAT_")}
    assert projected == ERRORS
    assert {item["id"] for item in projected.values()} >= set(range(5100, 5113))
    for code in projected:
        assert exceptions.ErrorCode[code].value == code


def test_error_generator_uses_shipped_source_and_owned_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sync_errors.ERRORS_JSON == ROOT / "ToukaProxy" / "config" / "errors.json.example"
    source = tmp_path / "errors.json.example"
    source.write_bytes(sync_errors.ERRORS_JSON.read_bytes())
    private = tmp_path / "errors.json"
    private.write_text("private sentinel must not be read", encoding="utf-8")
    original_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        assert path != private, "generator attempted to read a private registry"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(sync_errors, "ERRORS_JSON", source)
    monkeypatch.setattr(sync_errors, "TS_OUTPUT", tmp_path / "errors.ts")
    monkeypatch.setattr(sync_errors, "HEARTBEAT_OUTPUT", tmp_path / "error_codes.py")
    monkeypatch.setattr(Path, "open", guarded_open)
    sync_errors.sync()
    assert (tmp_path / "errors.ts").read_bytes() == (
        ROOT / "ToukaChat/src/lib/error_codes.ts"
    ).read_bytes()
    assert (tmp_path / "error_codes.py").read_bytes() == (
        ROOT / "ToukaHeartbeat/error_codes.py"
    ).read_bytes()


def test_existing_registry_receives_missing_shipped_codes_only_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "errors.json"
    override = {"id": 1000, "status": 500, "msg": "owned explicit override"}
    original = json.dumps({"SYSTEM_ERROR": override}).encode()
    existing.write_bytes(original)
    example = tmp_path / "errors.json.example"
    example.write_bytes((ROOT / "ToukaProxy/config/errors.json.example").read_bytes())
    monkeypatch.setattr(exceptions, "ERRORS_JSON_PATH", existing)
    monkeypatch.setattr(exceptions, "ERRORS_EXAMPLE_PATH", example, raising=False)
    loaded = exceptions.load_error_definitions()
    assert loaded["SYSTEM_ERROR"] == override
    assert loaded.get("HEARTBEAT_REVISION_CONFLICT") == ERRORS["HEARTBEAT_REVISION_CONFLICT"]
    assert existing.read_bytes() == original
    assert {item["id"] for item in loaded.values()} >= set(range(5100, 5113))


def test_missing_selected_registry_never_probes_default_private_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "selected" / "errors.json"
    example = tmp_path / "errors.json.example"
    example.write_bytes((ROOT / "ToukaProxy/config/errors.json.example").read_bytes())
    default_private = tmp_path / "default" / "config" / "errors.json"
    real_exists = Path.exists

    def guarded_exists(path: Path) -> bool:
        assert path != default_private, "selected config must not fall back to private default"
        return real_exists(path)

    monkeypatch.setattr(exceptions, "ERRORS_JSON_PATH", selected)
    monkeypatch.setattr(exceptions, "ERRORS_EXAMPLE_PATH", example)
    monkeypatch.setattr(exceptions, "PROXY_ROOT", tmp_path / "default")
    monkeypatch.setattr(Path, "exists", guarded_exists)
    assert exceptions.load_error_definitions() == json.loads(example.read_bytes())


def test_openapi_merge_rejects_conflicting_component_without_silent_overwrite() -> None:
    assert hasattr(openapi_merge, "merge_schemas"), "schema merger must reject conflicting names"
    left = {"paths": {}, "components": {"schemas": {"Shared": {"type": "string"}}}}
    equal = {"paths": {}, "components": {"schemas": {"Shared": {"type": "string"}}}}
    merged = openapi_merge.merge_schemas(left, equal, transport_models=TRANSPORT_MODELS)
    assert merged["components"]["schemas"]["Shared"] == {"type": "string"}
    assert merged["paths"] == left["paths"]
    for model in TRANSPORT_MODELS:
        assert model.__name__ in merged["components"]["schemas"]
    conflicting = {"paths": {}, "components": {"schemas": {"Shared": {"type": "integer"}}}}
    with pytest.raises(ValueError, match="Shared"):
        openapi_merge.merge_schemas(left, conflicting, transport_models=TRANSPORT_MODELS)
    assert left["components"]["schemas"]["Shared"] == {"type": "string"}
