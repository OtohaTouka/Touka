"""MCPHubService.fetch_* 整合測試（mock httpx）。

分類頁籤修復：確認 official/community 來源的 hub server 都被分到**功能性分類**
（前端 taxonomy），不再是舊的 Official/Community；且官方/社群身分仍由獨立的
``source``/``is_official`` 欄位保留（不流失訊號）。
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.config import get_app_data_dir
from src.routers.mcp import MCPHubServerItem
from src.services import mcp_hub_service as hub_mod
from src.services.mcp_hub_service import MCPHubService
from src.services.resource_manager import resource_manager
from src.utils.mcp_category import MCP_HUB_CATEGORIES
from src.utils.mcp_sentinels import APPDATA_SENTINEL


def _mock_client(payload):
    """建一個 async httpx client mock，client.get(...) 回傳 .json()==payload。"""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    return client


@pytest.fixture
def svc(tmp_path):
    return MCPHubService(
        hub_path=str(tmp_path / "mcp_hub.json"),
        config_path=str(tmp_path / "server_config.json"),
    )


@pytest.mark.asyncio
async def test_official_servers_use_functional_category(svc):
    """官方 server 分到功能性分類，非 Official；is_official/source 保留。"""
    payload = [
        {"name": "filesystem", "type": "dir"},
        {"name": "git", "type": "dir"},
        {"name": "memory", "type": "dir"},
    ]
    client = _mock_client(payload)
    with patch("src.services.mcp_hub_service.httpx.AsyncClient", return_value=client):
        servers = await svc.fetch_official_servers()

    by_name = {s["name"]: s for s in servers}
    assert by_name["Filesystem"]["category"] == "File"
    assert by_name["Git"]["category"] == "Dev"
    assert by_name["Memory"]["category"] == "Utils"
    for s in servers:
        assert s["category"] in MCP_HUB_CATEGORIES
        assert s["category"] not in {"Official", "Community"}
        assert s["is_official"] is True
        assert s["source"] == "official"


_REGISTRY_META = {
    "io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": True}
}


def _registry_item(name: str, description: str, packages: list | None, **server_extra) -> dict:
    server: dict = {"name": name, "description": description, **server_extra}
    if packages is not None:
        server["packages"] = packages
    return {"server": server, "_meta": _REGISTRY_META}


@pytest.mark.asyncio
async def test_registry_servers_use_functional_category(svc):
    """官方 Registry 社群來源（ADR-0187）：可裝條目映射、分類功能性。

    **契約變更（ADR-0192）**：remote-only 條目原本被略過（stdio-only 年代），批次 2
    開通 streamable HTTP 後改為一併上架、帶 ``url`` 欄位。
    """
    page = {
        "servers": [
            _registry_item(
                "io.github.acme/pg-mcp", "postgres database tools",
                [{
                    "registryType": "npm", "identifier": "@acme/pg-mcp",
                    "version": "3.0.1",
                    "transport": {"type": "stdio"},
                    "environmentVariables": [{"name": "PG_URL", "isRequired": True}],
                }],
                title="Acme Postgres",
            ),
            _registry_item(
                "io.github.acme/hosted-only", "hosted web service", None,
                remotes=[{"type": "streamable-http", "url": "https://x.example"}],
            ),
        ],
        "metadata": {"count": 2},
    }
    client = _mock_client(page)
    with patch("src.services.mcp_hub_service.httpx.AsyncClient", return_value=client):
        servers = await svc.fetch_registry_servers()

    assert len(servers) == 2
    by_id = {s["id"]: s for s in servers}
    entry = by_id["registry-io.github.acme/pg-mcp"]
    assert entry["name"] == "Acme Postgres"
    assert entry["category"] == "Database"
    assert entry["category"] in MCP_HUB_CATEGORIES
    assert entry["source"] == "registry"
    assert entry["is_official"] is False
    assert entry["command"] == "npx"
    assert entry["env_requirements"] == ["PG_URL"]

    # ADR-0192：remote-only 條目改為上架（帶 url、無本地啟動指令）。
    remote = by_id["registry-io.github.acme/hosted-only"]
    assert remote["url"] == "https://x.example"
    assert remote.get("command") is None
    assert remote["category"] in MCP_HUB_CATEGORIES


@pytest.mark.asyncio
async def test_registry_pagination_follows_cursor_then_stops(svc):
    """分頁跟隨 metadata.nextCursor（第二請求帶 cursor 參數）；無 cursor 即停。"""
    def _page(pkg_name: str, cursor: str | None) -> dict:
        meta: dict = {"count": 1}
        if cursor:
            meta["nextCursor"] = cursor
        return {
            "servers": [_registry_item(
                f"io.github.a/{pkg_name}", "postgres tools",
                [{"registryType": "npm", "identifier": pkg_name, "version": "1.0.0",
                  "transport": {"type": "stdio"}}],
            )],
            "metadata": meta,
        }

    resp1, resp2 = MagicMock(), MagicMock()
    resp1.raise_for_status = MagicMock()
    resp1.json = MagicMock(return_value=_page("one", "CUR"))
    resp2.raise_for_status = MagicMock()
    resp2.json = MagicMock(return_value=_page("two", None))
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=[resp1, resp2])
    with patch("src.services.mcp_hub_service.httpx.AsyncClient", return_value=client):
        servers = await svc.fetch_registry_servers()

    # 釘版後套件名位帶 @version（ADR-0191）；分頁順序仍逐頁串接。
    assert [s["args"][-1] for s in servers] == ["one@1.0.0", "two@1.0.0"]
    assert client.get.await_count == 2
    assert client.get.await_args_list[1].kwargs["params"]["cursor"] == "CUR"


@pytest.mark.asyncio
async def test_registry_pagination_cap_is_loud(svc, caplog):
    """恆有 nextCursor → 停在 _REGISTRY_MAX_PAGES 且記 WARNING（no silent caps）。"""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"servers": [], "metadata": {"nextCursor": "MORE"}})
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    with (
        patch("src.services.mcp_hub_service.httpx.AsyncClient", return_value=client),
        caplog.at_level(logging.WARNING),
    ):
        await svc.fetch_registry_servers()

    assert client.get.await_count == hub_mod._REGISTRY_MAX_PAGES
    assert "截斷" in caplog.text


def test_hub_response_model_carries_icon_and_source_url():
    """/mcp/hub 的 response_model 不得剝除 icon/source_url（前端實際消費形狀）。"""
    item = MCPHubServerItem(
        id="registry-x", name="X", description="d", command="npx", args=["-y", "x"],
        icon="Box", source_url="https://github.com/x/x",
    )
    dumped = item.model_dump()
    assert dumped.get("icon") == "Box"
    assert dumped.get("source_url") == "https://github.com/x/x"


@pytest.mark.asyncio
async def test_sync_preserves_curated_entries(svc):
    """sync 後本地 curated(first-party)條目仍在，且與官方/社群合併。"""
    resource_manager.write_json(
        svc.hub_path,
        {"hub": [{"id": "curated-mcp-docgen", "name": "mcp-docgen",
                  "source": "curated", "category": "File"}]},
    )
    with (
        patch.object(svc, "fetch_official_servers",
                     AsyncMock(return_value=[{"name": "Git", "source": "official"}])),
        patch.object(svc, "fetch_registry_servers", AsyncMock(return_value=[])),
    ):
        result = await svc.sync()
    assert result["status"] == "success"
    names = {s["name"] for s in resource_manager.read_json(svc.hub_path)["hub"]}
    assert "mcp-docgen" in names
    assert "Git" in names


@pytest.mark.asyncio
async def test_sync_includes_registry_leg(svc):
    """sync 合併必含官方 Registry 腿（僅官方腿空、registry 有貨也要成功入檔）。"""
    with (
        patch.object(svc, "fetch_official_servers", AsyncMock(return_value=[])),
        patch.object(svc, "fetch_registry_servers",
                     AsyncMock(return_value=[{"name": "RegOne", "source": "registry"}])),
    ):
        result = await svc.sync()
    assert result["status"] == "success"
    names = {s["name"] for s in resource_manager.read_json(svc.hub_path)["hub"]}
    assert "RegOne" in names


@pytest.mark.asyncio
async def test_sync_refreshes_seeded_officials_without_duplication(svc):
    """種子官方條目在 sync 後被抓取結果**取代**（非保留、不重複）——curated 才 survive。"""
    resource_manager.write_json(
        svc.hub_path,
        {"hub": [
            {"id": "curated-mcp-docgen", "name": "mcp-docgen", "source": "curated"},
            {"id": "official-git", "name": "Git", "source": "official",
             "description": "seeded-old"},
        ]},
    )
    with (
        patch.object(svc, "fetch_official_servers",
                     AsyncMock(return_value=[{"id": "official-git", "name": "Git",
                                              "source": "official",
                                              "description": "fetched-new"}])),
        patch.object(svc, "fetch_registry_servers", AsyncMock(return_value=[])),
    ):
        result = await svc.sync()
    assert result["status"] == "success"
    hub = resource_manager.read_json(svc.hub_path)["hub"]
    gits = [s for s in hub if s["name"] == "Git"]
    assert len(gits) == 1
    assert gits[0]["description"] == "fetched-new"
    assert "mcp-docgen" in {s["name"] for s in hub}


@pytest.mark.asyncio
async def test_sync_network_failure_keeps_existing(svc):
    """外部來源全空 → no_update，現有檔（含 curated）原封不動。"""
    resource_manager.write_json(
        svc.hub_path,
        {"hub": [
            {"id": "curated-mcp-docgen", "name": "mcp-docgen", "source": "curated"},
            {"id": "official-old", "name": "Old", "source": "official"},
        ]},
    )
    with (
        patch.object(svc, "fetch_official_servers", AsyncMock(return_value=[])),
        patch.object(svc, "fetch_registry_servers", AsyncMock(return_value=[])),
    ):
        result = await svc.sync()
    assert result["status"] == "no_update"
    names = {s["name"] for s in resource_manager.read_json(svc.hub_path)["hub"]}
    assert names == {"mcp-docgen", "Old"}


@pytest.mark.asyncio
async def test_install_curated_resolves_output_sentinel(svc):
    """安裝 curated mcp-docgen：寫入 server_config 並把 @TOUKA_OUTPUT@ 解析為實路徑。"""
    resource_manager.write_json(
        svc.hub_path,
        {"hub": [{
            "id": "curated-mcp-docgen", "name": "mcp-docgen", "command": "uvx",
            "args": ["mcp-docgen"], "env_requirements": [],
            "env": {"MCP_DOCGEN_OUTPUT_DIR": "@TOUKA_OUTPUT@"}, "source": "curated",
        }]},
    )
    result = await svc.install("curated-mcp-docgen", {})
    assert result["status"] == "success"
    server = resource_manager.read_json(svc.config_path)["mcpServers"]["mcp-docgen"]
    assert server["command"] == "uvx"
    assert server["args"] == ["mcp-docgen"]
    out = server["env"]["MCP_DOCGEN_OUTPUT_DIR"]
    assert out != "@TOUKA_OUTPUT@"
    assert "docgen_output" in out


# ---------------------------------------------------------------------------
# ADR-0186：官方條目啟動規格（F1 uvx/npm 正名、F2 filesystem 允許目錄哨兵）
# ---------------------------------------------------------------------------

_OFFICIAL_7_PAYLOAD = [
    {"name": "everything", "type": "dir"},
    {"name": "fetch", "type": "dir"},
    {"name": "filesystem", "type": "dir"},
    {"name": "git", "type": "dir"},
    {"name": "memory", "type": "dir"},
    {"name": "sequentialthinking", "type": "dir"},
    {"name": "time", "type": "dir"},
]


async def _fetch_official(svc, payload):
    client = _mock_client(payload)
    with patch("src.services.mcp_hub_service.httpx.AsyncClient", return_value=client):
        return await svc.fetch_official_servers()


@pytest.mark.asyncio
async def test_official_python_servers_use_uvx(svc):
    """fetch/git/time 是 PyPI 套件：uvx mcp-server-*（npm 同名 404，2026-07-24 實證）。

    版本段以前綴斷言容納釘版演進（ADR-0191）——本測試守的是「PyPI 套件正名」，
    「有沒有釘版」另由 seed-integrity 的釘版棘輪守，兩個關注點不互相綁死。
    """
    servers = await _fetch_official(svc, _OFFICIAL_7_PAYLOAD)
    by_id = {s["id"]: s for s in servers}
    for dir_name in ("fetch", "git", "time"):
        entry = by_id[f"official-{dir_name}"]
        assert entry["command"] == "uvx"
        assert len(entry["args"]) == 1
        assert entry["args"][0].startswith(f"mcp-server-{dir_name}==")


@pytest.mark.asyncio
async def test_official_sequentialthinking_npm_name_hyphenated(svc):
    """sequentialthinking 的 npm 正名帶連字號（server-sequential-thinking）。"""
    servers = await _fetch_official(svc, _OFFICIAL_7_PAYLOAD)
    entry = next(s for s in servers if s["id"] == "official-sequentialthinking")
    assert entry["command"] == "npx"
    assert len(entry["args"]) == 2
    assert entry["args"][0] == "-y"
    assert entry["args"][1].startswith("@modelcontextprotocol/server-sequential-thinking@")


@pytest.mark.asyncio
async def test_official_filesystem_args_carry_appdata_sentinel(svc):
    """filesystem 無允許目錄且 client 未宣告 roots 會初始化拋錯 → args 帶 @TOUKA_APPDATA@。"""
    servers = await _fetch_official(svc, _OFFICIAL_7_PAYLOAD)
    entry = next(s for s in servers if s["id"] == "official-filesystem")
    assert entry["command"] == "npx"
    assert len(entry["args"]) == 3
    assert entry["args"][0] == "-y"
    assert entry["args"][1].startswith("@modelcontextprotocol/server-filesystem@")
    # 哨兵須留在套件名之後（釘版把版本綴在套件名上，不得擠掉尾位哨兵）。
    assert entry["args"][2] == APPDATA_SENTINEL


@pytest.mark.asyncio
async def test_official_unknown_dir_falls_back_npx_with_warning(svc, caplog):
    """未知官方目錄 → npx 慣例 fallback（保持可見）＋WARNING（無聲變有聲）。"""
    with caplog.at_level(logging.WARNING):
        servers = await _fetch_official(svc, [{"name": "somenew", "type": "dir"}])
    entry = servers[0]
    assert entry["command"] == "npx"
    assert entry["args"] == ["-y", "@modelcontextprotocol/server-somenew"]
    assert "somenew" in caplog.text


@pytest.mark.asyncio
async def test_install_resolves_args_sentinel(svc):
    """install 端 args 哨兵與 env 同語意：落盤解析為絕對路徑（connect 端亦會再解析）。"""
    resource_manager.write_json(
        svc.hub_path,
        {"hub": [{
            "id": "official-filesystem", "name": "Filesystem", "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", APPDATA_SENTINEL],
            "env_requirements": [], "source": "official",
        }]},
    )
    result = await svc.install("official-filesystem", {})
    assert result["status"] == "success"
    server = resource_manager.read_json(svc.config_path)["mcpServers"]["filesystem"]
    assert server["args"][:2] == ["-y", "@modelcontextprotocol/server-filesystem"]
    assert server["args"][2] == str(get_app_data_dir())
    assert APPDATA_SENTINEL not in server["args"]
