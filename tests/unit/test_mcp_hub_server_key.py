"""GET /v1/mcp/hub 徽章比對鍵 ``server_key`` 投影測試（ADR-0190）。

F6：Hub 安裝落盤鍵＝``derive_server_key(name)``（ADR-0102，如 ``fetch``），
而 hub 條目 ``id`` 帶 ``curated-``/``official-`` 前綴（如 ``official-fetch``）——
前端拿 ``id`` 查 ``server_config`` 恆 miss，「已安裝／可更新」徽章恆滅。
修法＝後端於 /hub 回應逐條投影 ``server_key``（衍生規則單一 SSOT 在後端，
前端只消費、不得自行重刻）。
"""

import pytest

from src.models.identity import AgentIdentity
from src.routers import mcp as mcp_router
from src.services.mcp_hub_service import derive_server_key

_HUB = [
    {
        "id": "curated-mcp-docgen",
        "name": "MCP Docgen",
        "description": "d",
        "command": "npx",
        "args": ["-y", "@nicozanf/mcp-docgen"],
    },
    {
        "id": "official-fetch",
        "name": "Fetch",
        "description": "d",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
    },
]


def _patch_hub(monkeypatch, hub):
    monkeypatch.setattr(
        mcp_router.resource_manager, "read_json", lambda _p: {"hub": hub}
    )
    monkeypatch.setattr(mcp_router, "NPM_CACHE", {})
    monkeypatch.setattr(mcp_router, "NPM_CACHE_TIME", 0)


@pytest.mark.asyncio
async def test_hub_endpoint_projects_server_key(monkeypatch):
    """每條 hub 條目附 server_key＝install 實際落盤鍵（derive_server_key(name)）。"""
    _patch_hub(monkeypatch, [dict(e) for e in _HUB])
    result = await mcp_router.get_mcp_hub(
        AgentIdentity.for_role(agent_id="admin1", role="admin")
    )
    by_id = {item["id"]: item for item in result["hub"]}
    assert by_id["curated-mcp-docgen"]["server_key"] == "mcp-docgen", (
        "id 前綴（curated-）不得污染比對鍵"
    )
    assert by_id["official-fetch"]["server_key"] == "fetch"
    for item in result["hub"]:
        assert item["server_key"] == derive_server_key(item["name"]), (
            "投影值必須等於安裝落盤鍵（derive_server_key SSOT）"
        )


@pytest.mark.asyncio
async def test_hub_response_model_declares_server_key(monkeypatch):
    """MCPHubServerItem 必須宣告 server_key——response_model 剝除未宣告欄位（ADR-0187 教訓）。"""
    _patch_hub(monkeypatch, [dict(e) for e in _HUB])
    result = await mcp_router.get_mcp_hub(
        AgentIdentity.for_role(agent_id="admin1", role="admin")
    )
    validated = mcp_router.MCPHubResponse.model_validate(result)
    assert {i.id: i.server_key for i in validated.hub} == {
        "curated-mcp-docgen": "mcp-docgen",
        "official-fetch": "fetch",
    }, "宣告缺失＝欄位在線上被靜默剝除"
