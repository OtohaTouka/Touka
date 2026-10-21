"""批⑤-C 啟動接線（E52／E55）：main.py lifespan 的自我健檢與 skill↔MCP 接線真的接上。

兩條共通型態＝「純函式有測、lifespan 呼叫端無測」（家族③）——所有自動化測試以
TOUKA_BOOTSTRAP_ENABLED=false 跳過 bootstrap 段（E52），或只直呼純函式（E55）：

- E52（ADR-0096）：flag=true 時 lifespan 須呼叫 ``validate_live_configs()``——壞 live 設定
  fail-fast 中止啟動。刪該行或 try/except 吞錯＝ADR 要修的「靜默退回預設」舊行為復辟
  （成本 cap/quota 失效根因）→ 本測紅。
- E55（ADR-0102）：lifespan 兩段——``migrate_stale_server_keys()`` 有改名→
  ``initialize_all`` 前重載 registry；``validate_skill_mcp_wiring`` 缺口→逐筆
  '⚠️ [Skill-MCP]' 告警。刪整段＝回退「舊 key 靜默連不上 MCP」→ 本測紅。

骨架前例：``test_dispatch_supervisor_lifespan``（``with TestClient(app)`` 觸發完整 lifespan）。
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from src.main import app, protocol_manager
from src.utils.bootstrap import ConfigValidationError

# --------------------------------------------------------------------------- #
# E52（ADR-0096）：bootstrap 段 validate_live_configs 啟動接線
# --------------------------------------------------------------------------- #


def test_bootstrap_enabled_fails_fast_on_bad_live_config(tmp_path, monkeypatch):
    """flag=true＋壞 live 設定檔 → 啟動 fail-fast 拋 ConfigValidationError。

    變異刪 main.py:186 或包 try/except pass → 啟動照常完成、壞設定被吞 → 紅。
    """
    monkeypatch.setenv("TOUKA_BOOTSTRAP_ENABLED", "true")
    # 隔離設定目錄（雙落點）：validate 走 settings.CONFIG_DIR（動態讀 TOUKA_CONFIG_DIR env）、
    # ensure_configs 走 bootstrap 模組凍結常數 CONFIG_DIR——兩者都導向 tmp，零共用沙盒污染。
    monkeypatch.setenv("TOUKA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("src.utils.bootstrap.CONFIG_DIR", str(tmp_path))
    (tmp_path / "autonomy.json").write_text("{broken json", encoding="utf-8")

    with pytest.raises(ConfigValidationError), TestClient(app):
        pass  # pragma: no cover — 啟動必須在 yield 前中止


# --------------------------------------------------------------------------- #
# E55（ADR-0102）：skill↔MCP 啟動接線（migrate→reload ＋ validate→告警）
# --------------------------------------------------------------------------- #


def test_lifespan_migrate_true_reloads_registry_before_init(monkeypatch):
    """migrate 回報有改名 → initialize_all 之前重載 registry（技能宣告的 server 名對得上）。

    變異刪 main.py:214-219 整段 → 無 migrate、init 前無 reload → 紅。
    """
    order: list[str] = []

    monkeypatch.setattr(
        "src.utils.skill_mcp_wiring.migrate_stale_server_keys",
        lambda **_kw: (order.append("migrate"), [("curated-mcp-docgen", "mcp-docgen")])[1],
    )
    monkeypatch.setattr(
        protocol_manager.registry, "load_config", lambda: order.append("load_config")
    )

    async def _fake_init() -> None:
        order.append("initialize_all")

    monkeypatch.setattr(protocol_manager, "initialize_all", _fake_init)

    with TestClient(app):
        pass

    assert "migrate" in order and "initialize_all" in order
    i_mig, i_init = order.index("migrate"), order.index("initialize_all")
    # migrate 為真 → 必須在 initialize_all 之前重載過 registry
    assert "load_config" in order[i_mig:i_init]


def test_lifespan_warns_on_skill_mcp_wiring_gaps(monkeypatch, caplog):
    """健檢回報缺口 → 啟動日誌逐筆 '⚠️ [Skill-MCP]' 告警（非 fail-fast、大聲不靜默）。

    變異刪 main.py:223-231 整段 → 缺口零告警（無聲失敗復辟）→ 紅。
    """
    captured: dict[str, object] = {}

    def _fake_validate(registered, **_kw):
        captured["registered"] = registered
        return ["技能 document-generator 需要 MCP server mcp-docgen（未註冊）"]

    monkeypatch.setattr(
        "src.utils.skill_mcp_wiring.validate_skill_mcp_wiring", _fake_validate
    )

    with caplog.at_level(logging.WARNING, logger="src.main"), TestClient(app):
        pass

    msgs = [r.getMessage() for r in caplog.records]
    assert any("⚠️ [Skill-MCP]" in m and "mcp-docgen" in m for m in msgs)
    # 以「已註冊 server 集合」呼叫（真實 registry 狀態，非空殼參數）
    assert isinstance(captured["registered"], set)
