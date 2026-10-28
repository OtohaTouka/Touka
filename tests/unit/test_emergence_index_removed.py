"""湧現指數（E-Index）已從後端刪除（ADR-0291）.

- 統計端點 ``/stats/emergence``（含 ``/v1``）不在了。
- 計算函式與沒人用的 ``utils/metrics.py`` 也刪了。
- token_usage.json：新的供應商項目不再有 ``e_index_history``；舊檔裡已有的歷史原樣保留。
- 稽核 CSV：新檔沒有 ``e_index`` 欄；舊檔沿用自己的表頭，這欄補空值，其他欄不錯位。

入帳檔走 conftest 沙盒。
"""

from __future__ import annotations

import csv
import importlib.util
import inspect
import io
import json
from pathlib import Path

import pytest
from src.models.identity import AgentIdentity
from src.services import telemetry
from src.services.resource_manager import resource_manager

_HEADERS = [
    "timestamp", "request_id", "agent_id", "provider",
    "total_ms", "local_ms", "network_ms", "pure_gen_ms",
    "input_tokens", "output_tokens", "cost_usd", "rate_source",
]
_LEGACY_HEADERS = [*_HEADERS[:8], "e_index", *_HEADERS[8:]]
_LEGACY_ROW = [
    "2026-10-01 10:00:00", "req-old", "EIdxLegacy", "OpenAI",
    "9", "1", "0", "8", "0.4321", "100", "20", "0.000140", "builtin",
]


def _identity(agent_id: str) -> AgentIdentity:
    return AgentIdentity.for_role(agent_id, "Owner", capabilities=["*"])


def _stats_dir(identity: AgentIdentity) -> Path:
    path = Path(resource_manager.get_stats_path(identity))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _audit_rows(identity: AgentIdentity) -> list[list[str]]:
    text = (_stats_dir(identity) / "usage_audit.csv").read_text(encoding="utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def _log(identity: AgentIdentity, request_id: str) -> None:
    telemetry.log_usage_audit(
        identity, request_id, "OpenAI", {"total_ms": 12, "local_ms": 2, "pure_gen_ms": 10},
        {"prompt_tokens": 30, "completion_tokens": 5}, cost_usd=0.0012,
    )


@pytest.mark.parametrize("path", ["/v1/stats/emergence", "/stats/emergence"])
def test_emergence_endpoint_is_gone(auth_client, path):
    assert auth_client.get(path).status_code == 404


def test_emergence_calculation_is_gone():
    assert not hasattr(telemetry, "calculate_emergence_index")
    assert not hasattr(telemetry, "calculate_shannon_entropy")
    assert importlib.util.find_spec("src.utils.metrics") is None
    assert "e_index" not in inspect.signature(telemetry.accumulate_token_usage).parameters


def test_token_usage_keeps_old_history_and_adds_none():
    identity = _identity("EIdxHistory")
    old_history = [{"timestamp": "2026-10-01T10:00:00+08:00", "e_index": 0.4321}]
    usage_file = _stats_dir(identity) / "token_usage.json"
    usage_file.write_text(json.dumps({"OpenAI": {
        "prompt_tokens": 100, "completion_tokens": 20, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "total_cost_usd": 0.0, "e_index_history": old_history,
    }}), encoding="utf-8")

    telemetry.accumulate_token_usage(identity, "OpenAI", {"prompt_tokens": 7})
    telemetry.accumulate_token_usage(identity, "Gemini", {"prompt_tokens": 3})

    stats = json.loads(usage_file.read_text(encoding="utf-8"))
    assert stats["OpenAI"]["prompt_tokens"] == 107
    assert stats["OpenAI"]["e_index_history"] == old_history
    assert stats["Gemini"]["prompt_tokens"] == 3
    assert "e_index_history" not in stats["Gemini"]


def test_new_audit_file_has_no_e_index_column():
    identity = _identity("EIdxNewCsv")
    _log(identity, "req-1")
    _log(identity, "req-2")

    rows = _audit_rows(identity)
    assert rows[0] == _HEADERS
    assert len(rows) == 3
    row = dict(zip(_HEADERS, rows[2], strict=True))
    assert row["request_id"] == "req-2"
    assert row["pure_gen_ms"] == "10"
    assert row["input_tokens"] == "30"
    assert row["cost_usd"] == "0.001200"


def test_old_audit_file_keeps_its_header_and_gets_a_blank_e_index():
    identity = _identity("EIdxLegacy")
    audit_file = _stats_dir(identity) / "usage_audit.csv"
    buf = io.StringIO()
    csv.writer(buf).writerows([_LEGACY_HEADERS, _LEGACY_ROW])
    audit_file.write_bytes(buf.getvalue().encode("utf-8-sig"))

    _log(identity, "req-new")

    rows = _audit_rows(identity)
    assert rows[0] == _LEGACY_HEADERS
    assert rows[1] == _LEGACY_ROW
    assert len(rows) == 3
    row = dict(zip(_LEGACY_HEADERS, rows[2], strict=True))
    assert row["e_index"] == ""
    assert row["request_id"] == "req-new"
    assert row["pure_gen_ms"] == "10"
    assert row["input_tokens"] == "30"
    assert row["output_tokens"] == "5"
    assert row["cost_usd"] == "0.001200"
