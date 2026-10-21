"""TDD — 批次0 費率 SSOT：telemetry 整合（增量累計、稽核列永不靜默漏記、model 穿線）。

- ssot-1：accumulate_token_usage 改逐回合增量累計（非從累計桶重算）。
- ssot-4：log_usage_audit 殺掉 :178 重複硬編碼後，即使未傳 model 仍須寫出稽核列
  （model 預設 None → 不拋例外 → 不靜默漏記），且新增 rate_source 欄。
"""
from __future__ import annotations

from pathlib import Path

from src.models.identity import AgentIdentity
from src.services.providers import pricing
from src.services.resource_manager import resource_manager
from src.services.telemetry import accumulate_token_usage, log_usage_audit

_FLAT = {"_default": {"input": 3.0, "output": 15.0}, "pricing": {}}


def _force_flat_rate(monkeypatch) -> None:
    monkeypatch.setattr(pricing.config_utils, "read_config_json", lambda _fn: _FLAT)


def test_accumulate_is_incremental_not_recomputed(monkeypatch):
    """兩回合各 1M input（$3）→ 增量累計 $6（非從 2M 累計桶重算）。"""
    _force_flat_rate(monkeypatch)
    ident = AgentIdentity.for_role(agent_id="TelemIncremental", role="Owner")
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    accumulate_token_usage(ident, "ProvX", dict(usage), model="model-a")
    accumulate_token_usage(ident, "ProvX", dict(usage), model="model-a")

    data = resource_manager.read_json(
        Path(resource_manager.get_stats_path(ident)) / "token_usage.json"
    )
    assert data["ProvX"]["prompt_tokens"] == 2_000_000
    assert data["ProvX"]["total_cost_usd"] == 6.0


def test_audit_row_written_even_without_model_and_has_rate_source():
    """ssot-4：未傳 model 也須寫出稽核列（不因 model 未定義而靜默漏記）＋ rate_source 欄。"""
    ident = AgentIdentity.for_role(agent_id="AuditNoModel", role="Owner")
    log_usage_audit(
        ident,
        "req-audit-1",
        "ProvX",
        {"total_ms": 1},
        {"input_tokens": 10, "output_tokens": 5},
    )
    csv_path = Path(resource_manager.get_stats_path(ident)) / "usage_audit.csv"
    content = csv_path.read_text(encoding="utf-8-sig")
    assert "req-audit-1" in content          # 列有寫入
    assert "rate_source" in content          # 新欄存在
    assert "fallback" in content             # model=None → fallback 來源標記
