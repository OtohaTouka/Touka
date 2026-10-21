"""MCP 初次連線逾時（ADR-0191）——「掛住不回應」的伺服器不得拖垮啟動。

缺陷（本批 Tier A 現勘 Stripe 條目時揭露的 LIVE 生產缺口，非 Stripe 專屬）：
SDK v1 的手動初始握手過去無任何逾時，而
``ProtocolManager.initialize_all`` 是**序列** ``await connect_server(...)``、且位於
``main.py`` 啟動 lifespan 內。伺服器若「啟動成功但不回應 initialize」（金鑰無效的
stdio↔HTTP 橋接器就是這個形態——錯誤只 ``console.error`` 被吞、stdio 不關），
``await`` 永遠不返回；掛住不是 exception，``try/except`` 完全接不住 →
**ToukaProxy 啟動流程永不完成，且排在其後的每一台 MCP server 都連不上**。
一個打錯字但格式合法的金鑰＝全域開機拒絕服務、且無任何錯誤訊息。

SDK v2 延續以 ``Client(read_timeout_seconds=...)`` 設界——**刻意不用**
``asyncio.wait_for`` 包 ``connect_mcp_server``：後者的逾時取消會跨 anyio 取消域，
``except`` 分支的 ``stack.aclose()`` 隨即拋
``RuntimeError: Attempted to exit cancel scope in a different task``＝teardown 破損
（已實測）。``read_timeout_seconds`` 在同一 task 內逾時，乾淨拋 ``MCPError``。

本檔用**真子進程**（讀 stdin 但永不回應的 python）驗行為，不 mock 傳輸層。
"""

from __future__ import annotations

import asyncio
import sys
import time
from contextlib import AsyncExitStack

import pytest
from src.protocols import mcp as mcp_pkg
from src.protocols.manager import ProtocolManager
from src.protocols.mcp.client import connect_mcp_server
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig

# 「啟動成功但永不回應 JSON-RPC」的伺服器替身：持續讀 stdin、不寫 stdout。
_HANGING_SERVER = (
    "import sys\n"
    "for _ in sys.stdin:\n"
    "    pass\n"
)


def _hanging_config() -> ProtocolServerConfig:
    return ProtocolServerConfig(command=sys.executable, args=["-c", _HANGING_SERVER])


@pytest.mark.asyncio
async def test_connect_times_out_instead_of_hanging_forever(monkeypatch):
    """不回應的伺服器 → 於逾時上限內拋錯，而非永久 await。"""
    monkeypatch.setattr(mcp_pkg.client, "MCP_INIT_TIMEOUT_SECONDS", 2.0)

    started = time.monotonic()
    with pytest.raises(Exception) as exc_info:
        # 護欄（E74 教訓「驗逾時的測試必須自有界」）：受測機制若失效，本測試自身也不能
        # 永久掛住拖垮整套。判準靠下方的**時長**而非「有沒有拋例外」——護欄觸發時
        # elapsed 會逼近 30s 而遠超門檻，測試照樣紅。
        await asyncio.wait_for(connect_mcp_server(_hanging_config()), timeout=30.0)
    elapsed = time.monotonic() - started

    # 有界：確實在受測逾時（2s）附近返回，而非拖到護欄才結束。
    assert elapsed < 25.0, f"連線耗時 {elapsed:.1f}s，逾時未生效（護欄兜底）"
    # 且不是 teardown 破損的 RuntimeError（asyncio.wait_for 路線的失敗形狀）。
    assert "cancel scope" not in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_hanging_server_does_not_block_initialize_all(monkeypatch):
    """單一連線失敗會被隔離——重試用盡後，後續伺服器仍被嘗試。"""
    attempted: list[str] = []
    healthy_client = object()

    registry = ProtocolRegistry.__new__(ProtocolRegistry)
    registry.servers = {
        "hanging": ProtocolServerConfig(command="hang"),
        "healthy": ProtocolServerConfig(command="healthy"),
    }

    manager = ProtocolManager(registry)

    async def _connect(config, **_kwargs):
        attempted.append(config.command)
        if config.command == "hang":
            raise TimeoutError("startup deadline exceeded")
        return healthy_client, AsyncExitStack()

    async def _no_ide():
        return {}

    monkeypatch.setattr("src.protocols.manager.connect_mcp_server", _connect)
    # discover_ide_servers 會外呼探測，測試中短路（本檔關注點為失敗隔離）。
    monkeypatch.setattr("src.protocols.manager.discover_ide_servers", _no_ide)

    await asyncio.wait_for(manager.initialize_all(), timeout=10.0)

    assert attempted == ["hang", "hang", "hang", "healthy"]
    assert manager._active_sessions == {"healthy": healthy_client}
    await manager.cleanup()
