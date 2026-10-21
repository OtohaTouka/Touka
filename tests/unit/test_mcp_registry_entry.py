"""registry_item_to_hub_entry 純映射測試（ADR-0187）。

官方 MCP Registry（registry.modelcontextprotocol.io/v0）條目 → hub entry 的唯一
映射函式：npm→npx／pypi→uvx、僅收 stdio、env_requirements 取 isRequired、
isLatest/active 過濾、remote-only 與不可啟動者誠實回 None。
"""
from __future__ import annotations

from src.utils.mcp_category import MCP_HUB_CATEGORIES
from src.utils.mcp_registry_entry import registry_item_to_hub_entry

_META = {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": True}}


def _item(server: dict, meta: dict = _META) -> dict:
    return {"server": server, "_meta": meta}


def _npm_server(**over) -> dict:
    server = {
        "name": "io.github.acme/files-mcp",
        "title": "Acme Files",
        "description": "filesystem operations for cloud drive",
        "repository": {"url": "https://github.com/acme/files-mcp"},
        "packages": [{
            "registryType": "npm",
            "identifier": "@acme/files-mcp",
            "version": "2.1.0",
            "transport": {"type": "stdio"},
            "environmentVariables": [
                {"name": "ACME_TOKEN", "isRequired": True, "isSecret": True},
                {"name": "ACME_REGION", "isRequired": False},
            ],
        }],
    }
    server.update(over)
    return server


def test_npm_stdio_maps_to_npx_with_required_env_only():
    entry = registry_item_to_hub_entry(_item(_npm_server()))
    assert entry is not None
    assert entry["command"] == "npx"
    # 釘版（ADR-0191）：registry 自帶 packages[].version，映射即釘死該版本。
    assert entry["args"] == ["-y", "@acme/files-mcp@2.1.0"]
    assert entry["env_requirements"] == ["ACME_TOKEN"]
    assert entry["id"] == "registry-io.github.acme/files-mcp"
    assert entry["name"] == "Acme Files"
    assert entry["source"] == "registry"
    assert entry["is_official"] is False
    assert entry["npm_package"] == "@acme/files-mcp"
    assert entry["source_url"] == "https://github.com/acme/files-mcp"
    assert entry["category"] == "File"
    assert entry["category"] in MCP_HUB_CATEGORIES


def test_pypi_stdio_maps_to_uvx():
    server = _npm_server(packages=[{
        "registryType": "pypi",
        "identifier": "acme-mcp",
        "version": "0.4.2",
        "transport": {"type": "stdio"},
    }])
    entry = registry_item_to_hub_entry(_item(server))
    assert entry is not None
    assert entry["command"] == "uvx"
    assert entry["args"] == ["acme-mcp==0.4.2"]
    assert entry.get("npm_package") is None
    assert entry["env_requirements"] == []


def test_streamable_http_remote_maps_to_url_entry():
    """remote-only 條目自 ADR-0192 起可上架（批次 2 開通 streamable HTTP）。"""
    server = {
        "name": "io.github.acme/hosted",
        "title": "Acme Hosted",
        "description": "hosted service",
        "remotes": [{
            "type": "streamable-http",
            "url": "https://x.example/mcp",
            "headers": [
                {"name": "Authorization", "isRequired": True, "isSecret": True,
                 "description": "Bearer token (acme_sk_...)"},
                {"name": "X-Optional", "isRequired": False},
            ],
        }],
    }
    entry = registry_item_to_hub_entry(_item(server))
    assert entry is not None
    assert entry["url"] == "https://x.example/mcp"
    # header 值為 ${VAR} 模板，實值留在 env（沿用既有 env 遮罩保護）。
    assert entry["headers"] == {"Authorization": "${MCP_AUTHORIZATION}"}
    assert entry["env_requirements"] == ["MCP_AUTHORIZATION"]
    # 非必填 header 不進模板——送出空值只會換來 4xx。
    assert "X-Optional" not in entry["headers"]
    # remote 條目無本地啟動指令。
    assert entry.get("command") is None
    # 認證說明併入 description（零 schema 成本地讓使用者知道要填什麼格式）。
    assert "Bearer token" in entry["description"]


def test_stdio_package_wins_over_remote():
    """local-first：同時有本地套件與遠端端點時取本地。"""
    server = _npm_server(remotes=[{
        "type": "streamable-http", "url": "https://x.example/mcp",
    }])
    entry = registry_item_to_hub_entry(_item(server))
    assert entry is not None
    assert entry["command"] == "npx"
    assert entry.get("url") is None


def test_sse_only_remote_returns_none():
    """SSE 已被三家桌面版標為 deprecated，Touka 不收。"""
    server = {
        "name": "io.github.acme/sse-only",
        "description": "legacy sse",
        "remotes": [{"type": "sse", "url": "https://x.example/sse"}],
    }
    assert registry_item_to_hub_entry(_item(server)) is None


def test_insecure_remote_url_returns_none():
    """明文 http 外部端點不上架——header 會帶憑證。"""
    server = {
        "name": "io.github.acme/insecure",
        "description": "plaintext",
        "remotes": [{"type": "streamable-http", "url": "http://x.example/mcp"}],
    }
    assert registry_item_to_hub_entry(_item(server)) is None


def test_non_latest_or_inactive_returns_none():
    stale = {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": False}}
    deleted = {"io.modelcontextprotocol.registry/official": {"status": "deleted", "isLatest": True}}
    assert registry_item_to_hub_entry(_item(_npm_server(), meta=stale)) is None
    assert registry_item_to_hub_entry(_item(_npm_server(), meta=deleted)) is None


def test_unsupported_registry_or_transport_skipped_but_mixed_picks_supported():
    oci_only = _npm_server(packages=[{
        "registryType": "oci", "identifier": "ghcr.io/x", "version": "1.0.0",
        "transport": {"type": "stdio"},
    }])
    http_only = _npm_server(packages=[{
        "registryType": "npm", "identifier": "@x/y", "version": "1.0.0",
        "transport": {"type": "streamable-http"},
    }])
    mixed = _npm_server(packages=[
        {"registryType": "oci", "identifier": "ghcr.io/x", "version": "1.0.0",
         "transport": {"type": "stdio"}},
        {"registryType": "npm", "identifier": "@acme/files-mcp", "version": "2.1.0",
         "transport": {"type": "stdio"}},
    ])
    assert registry_item_to_hub_entry(_item(oci_only)) is None
    assert registry_item_to_hub_entry(_item(http_only)) is None
    picked = registry_item_to_hub_entry(_item(mixed))
    assert picked is not None
    assert picked["command"] == "npx"
    assert picked["args"] == ["-y", "@acme/files-mcp@2.1.0"]


def test_package_arguments_with_concrete_values_appended():
    server = _npm_server(packages=[{
        "registryType": "npm",
        "identifier": "@acme/files-mcp",
        "version": "2.1.0",
        "transport": {"type": "stdio"},
        "packageArguments": [
            {"type": "positional", "value": "--stdio"},
            {"type": "named", "name": "--port", "value": "8080"},
            {"type": "positional", "valueHint": "optional_dir"},
        ],
    }])
    entry = registry_item_to_hub_entry(_item(server))
    assert entry is not None
    assert entry["args"] == ["-y", "@acme/files-mcp@2.1.0", "--stdio", "--port", "8080"]


def test_required_package_argument_without_value_returns_none():
    server = _npm_server(packages=[{
        "registryType": "npm",
        "identifier": "@acme/files-mcp",
        "version": "2.1.0",
        "transport": {"type": "stdio"},
        "packageArguments": [
            {"type": "positional", "isRequired": True, "valueHint": "target_dir"},
        ],
    }])
    assert registry_item_to_hub_entry(_item(server)) is None


def test_package_without_version_returns_none():
    """無 ``version`` ＝無法釘版 → 誠實不上架（ADR-0191 釘版政策無例外）。

    2026-07-25 對 registry 前 5 頁（生產掃描上限）實測：405 個 stdio×npm/pypi 套件
    **全數**帶 version，故此 fail-closed 於實務上零貨架損失；真缺版本者反而是異常條目。
    """
    server = _npm_server(packages=[{
        "registryType": "npm",
        "identifier": "@acme/files-mcp",
        "transport": {"type": "stdio"},
    }])
    assert registry_item_to_hub_entry(_item(server)) is None


def test_title_fallback_to_name_tail_and_website_fallback():
    server = _npm_server(websiteUrl="https://acme.example/docs")
    server.pop("title")
    server.pop("repository")
    entry = registry_item_to_hub_entry(_item(server))
    assert entry is not None
    assert entry["name"] == "files-mcp"
    assert entry["source_url"] == "https://acme.example/docs"


def test_malformed_item_returns_none():
    assert registry_item_to_hub_entry({}) is None
    assert registry_item_to_hub_entry({"server": {}, "_meta": _META}) is None
