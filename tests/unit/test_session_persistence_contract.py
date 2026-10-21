"""可靠性 R2：保存／終態公開模型由單一純 Pydantic 定義。"""

from importlib import import_module
from importlib.util import find_spec

import pytest
from openapi_merge import OpenAPIMergeConflict, merge_schemas
from pydantic import TypeAdapter, ValidationError
from src.main import app
from src.models.session_persistence import TRANSPORT_MODELS

CONTRACT = (
    import_module("src.models.session_persistence")
    if find_spec("src.models.session_persistence") is not None
    else None
)
RESULT = TypeAdapter(CONTRACT.SaveResult) if CONTRACT is not None else None


def test_snapshot_boolean_union_does_not_emit_string_discriminator_mapping():
    schema = TypeAdapter(CONTRACT.SessionSnapshotResponse).json_schema()
    assert "discriminator" not in schema
    assert schema["$defs"]["PresentSessionSnapshot"]["properties"]["exists"]["const"] is True
    assert schema["$defs"]["AbsentSessionSnapshot"]["properties"]["exists"]["const"] is False
    for name in ("PresentSessionSnapshot", "AbsentSessionSnapshot"):
        assert {"exists", "revision", "session"}.issubset(schema["$defs"][name]["required"])
    assert (
        TypeAdapter(CONTRACT.SessionSnapshotResponse)
        .validate_python({"exists": False, "revision": "absence", "session": None})
        .exists
        is False
    )
    for invalid in (
        {"revision": "r", "session": None},
        {"exists": "False", "revision": "r", "session": None},
    ):
        with pytest.raises(ValidationError):
            TypeAdapter(CONTRACT.SessionSnapshotResponse).validate_python(invalid)


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "saved", "representation": "text", "revision": "opaque-revision"},
        {"status": "saved", "representation": "full", "revision": "opaque-revision"},
        {"status": "skipped", "reason": "incognito"},
        {"status": "skipped", "reason": "no_text"},
        {"status": "failed", "error_code": 1102},
        {"status": "unknown", "error_code": 1302},
    ],
)
def test_save_result_round_trip_preserves_distinct_outcomes(payload):
    assert RESULT is not None, "typed persistence result is missing"
    assert RESULT.validate_python(payload).model_dump(mode="json") == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "saved", "representation": "full"},
        {"status": "saved", "representation": "full", "revision": ""},
        {"status": "saved", "representation": "partial", "revision": "revision"},
        {"status": "skipped", "reason": "missing_session"},
        {"status": "skipped", "reason": "incognito", "revision": "old"},
        {"status": "unknown", "error_code": 1302, "revision": "old"},
        {"status": "failed", "error_code": 1102, "detail": "synthetic-sensitive-input"},
        {"status": "unknown", "error_code": "1302"},
        {"status": "unknown", "error_code": True},
        {"status": "unknown", "error_code": -1},
        {"status": "done"},
    ],
)
def test_invalid_result_never_becomes_saved(payload):
    assert RESULT is not None, "typed persistence result is missing"
    with pytest.raises(ValidationError):
        RESULT.validate_python(payload)


@pytest.mark.parametrize("generation", ["completed", "error", "cancelled"])
def test_terminal_keeps_generation_independent_from_persistence(generation):
    assert CONTRACT is not None, "typed terminal event is missing"
    coordinates = {
        "session_id": "synthetic-session",
        "stream_id": "synthetic-stream",
        "user_parent_id": "synthetic-user",
        "assistant_node_id": "synthetic-assistant",
    }
    started = CONTRACT.StreamStartedEvent(**coordinates)
    terminal = CONTRACT.StreamTerminalEvent(
        **coordinates,
        generation=generation,
        persistence={"status": "saved", "representation": "text", "revision": "revision"},
    )
    assert started.type == "touka_stream_started"
    assert terminal.type == "touka_stream_terminal"
    assert terminal.generation == generation
    assert terminal.model_dump()["persistence"]["representation"] == "text"


def test_models_export_named_discriminated_result_without_external_references():
    assert CONTRACT is not None, "typed persistence schema is missing"
    schema = CONTRACT.SessionSaveResponse.model_json_schema()
    result = schema["$defs"]["SaveResult"]
    assert result["discriminator"]["propertyName"] == "status"
    assert set(result["discriminator"]["mapping"]) == {"saved", "skipped", "failed", "unknown"}
    assert "revision" in schema["$defs"]["SavedResult"]["required"]


def test_actual_schema_merge_contains_transport_contracts_without_fake_paths():
    original = {"paths": {}, "components": {"schemas": {}}}
    schema = merge_schemas(original, original, transport_models=TRANSPORT_MODELS)
    assert "SaveResult" in schema["components"]["schemas"]
    assert "StreamStartedEvent" in schema["components"]["schemas"]
    assert "StreamTerminalEvent" in schema["components"]["schemas"]
    assert schema["paths"] == {}
    assert original["components"]["schemas"] == {}


def test_actual_schema_merge_rejects_transport_name_collision():
    wrong = {"paths": {}, "components": {"schemas": {"SaveResult": {"type": "integer"}}}}
    with pytest.raises(OpenAPIMergeConflict):
        merge_schemas(wrong, {"paths": {}}, transport_models=TRANSPORT_MODELS)


def test_transport_projection_matches_actual_fastapi_nullable_defaults():
    current = app.openapi()
    merged = merge_schemas(current, {"paths": {}}, transport_models=TRANSPORT_MODELS)
    assert (
        merged["components"]["schemas"]["AbsentSessionSnapshot"]
        == current["components"]["schemas"]["AbsentSessionSnapshot"]
    )
    session = merged["components"]["schemas"]["AbsentSessionSnapshot"]["properties"]["session"]
    assert session["type"] == "null"


def test_nullable_transport_projection_still_rejects_a_different_type():
    definition = CONTRACT.AbsentSessionSnapshot.model_json_schema()
    definition["properties"]["session"] = {"type": "string"}
    with pytest.raises(OpenAPIMergeConflict):
        merge_schemas(
            {"paths": {}, "components": {"schemas": {"AbsentSessionSnapshot": definition}}},
            {"paths": {}},
            transport_models=TRANSPORT_MODELS,
        )
