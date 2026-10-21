"""Unit tests for ToukaProxy IDE discovery (TDD — P0 fix).

Tests verify that discover_ide_servers() correctly identifies ToukaUI
discovery files and ignores malformed or wrong-name files.
"""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003

import pytest
from src.protocols.ide.discovery import discover_ide_servers


@pytest.fixture
def ide_temp_dir(tmp_path: Path) -> Path:
    """在 tmp_path 下建立 gemini/ide 目錄結構，隔離實際 %TEMP%。"""
    ide_dir = tmp_path / "gemini" / "ide"
    ide_dir.mkdir(parents=True)
    return ide_dir


def _write_discovery_file(ide_dir: Path, filename: str, data: dict) -> Path:
    f = ide_dir / filename
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# 正常發現
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_q36_discovers_modern_touka_ui_without_legacy_routes(ide_temp_dir: Path):
    """標準 ToukaUI 發現檔應被正確解析並回傳連線資訊。"""
    _write_discovery_file(
        ide_temp_dir,
        "gemini-ide-server-touka.json",
        {
            "ideInfo": {"name": "ToukaUI"},
            "port": 3005,
            "authToken": "test-token-abc",
            "transport": {
                "type": "streamable-http",
                "endpoint": "/mcp",
                "protocolMode": "2026-07-28",
            },
        },
    )

    result = await discover_ide_servers(ide_base_dir=ide_temp_dir)

    assert "ToukaUI" in result
    assert result["ToukaUI"]["url"] == "http://localhost:3005/mcp"
    assert result["ToukaUI"]["token"] == "test-token-abc"  # noqa: S105
    assert result["ToukaUI"]["protocol_mode"] == "2026-07-28"
    assert set(result["ToukaUI"]) == {"url", "token", "protocol_mode"}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_q36_discovery_rejects_retired_protocol_modes(ide_temp_dir: Path, mode):
    _write_discovery_file(
        ide_temp_dir,
        "gemini-ide-server-touka.json",
        {
            "ideInfo": {"name": "ToukaUI"},
            "port": 3005,
            "authToken": "test-token",
            "transport": {"type": "streamable-http", "endpoint": "/mcp", "protocolMode": mode},
        },
    )
    assert await discover_ide_servers(ide_base_dir=ide_temp_dir) == {}


@pytest.mark.asyncio
async def test_empty_dir_returns_empty_dict(ide_temp_dir: Path):
    """空目錄應回傳空 dict。"""
    result = await discover_ide_servers(ide_base_dir=ide_temp_dir)
    assert result == {}


@pytest.mark.asyncio
async def test_nonexistent_dir_returns_empty_dict(tmp_path: Path):
    """目錄不存在時應回傳空 dict，不應拋例外。"""
    missing = tmp_path / "no_such_dir"
    result = await discover_ide_servers(ide_base_dir=missing)
    assert result == {}


# ---------------------------------------------------------------------------
# 過濾邏輯
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ignores_wrong_ide_name(ide_temp_dir: Path):
    """ideInfo.name 不是 ToukaUI 的檔案應被跳過。"""
    _write_discovery_file(
        ide_temp_dir,
        "gemini-ide-server-other.json",
        {"ideInfo": {"name": "SomeOtherIDE"}, "port": 9000, "authToken": "other-token"},
    )

    result = await discover_ide_servers(ide_base_dir=ide_temp_dir)
    assert result == {}


@pytest.mark.asyncio
async def test_ignores_file_without_port(ide_temp_dir: Path):
    """缺少 port 的發現檔應被忽略。"""
    _write_discovery_file(
        ide_temp_dir,
        "gemini-ide-server-touka.json",
        {"ideInfo": {"name": "ToukaUI"}, "authToken": "no-port"},
    )

    result = await discover_ide_servers(ide_base_dir=ide_temp_dir)
    assert result == {}


@pytest.mark.asyncio
async def test_ignores_file_without_bearer_token(ide_temp_dir: Path):
    """缺 token 不得把 IDE MCP server 降成 unauthenticated peer。"""
    _write_discovery_file(
        ide_temp_dir,
        "gemini-ide-server-touka.json",
        {
            "ideInfo": {"name": "ToukaUI"},
            "port": 3005,
        },
    )

    assert await discover_ide_servers(ide_base_dir=ide_temp_dir) == {}


@pytest.mark.asyncio
async def test_rejects_non_path_transport_endpoint(ide_temp_dir: Path):
    """發現檔不得把 localhost contract 改寫成任意遠端 URL。"""
    _write_discovery_file(
        ide_temp_dir,
        "gemini-ide-server-touka.json",
        {
            "ideInfo": {"name": "ToukaUI"},
            "port": 3005,
            "authToken": "test-token",
            "transport": {
                "type": "streamable-http",
                "endpoint": "https://evil.example/mcp",
                "protocolMode": "2026-07-28",
            },
        },
    )

    assert await discover_ide_servers(ide_base_dir=ide_temp_dir) == {}


@pytest.mark.asyncio
async def test_ignores_malformed_json(ide_temp_dir: Path):
    """格式錯誤的 JSON 應被靜默跳過，不應崩潰。"""
    bad_file = ide_temp_dir / "gemini-ide-server-bad.json"
    bad_file.write_text("{this is not valid json", encoding="utf-8")

    result = await discover_ide_servers(ide_base_dir=ide_temp_dir)
    assert result == {}


@pytest.mark.asyncio
async def test_multiple_files_only_returns_touka_ui(ide_temp_dir: Path):
    """同目錄有多個發現檔時，只回傳 ToukaUI。"""
    _write_discovery_file(
        ide_temp_dir,
        "gemini-ide-server-touka.json",
        {"ideInfo": {"name": "ToukaUI"}, "port": 3005, "authToken": "my-token"},
    )
    _write_discovery_file(
        ide_temp_dir,
        "gemini-ide-server-vscode.json",
        {"ideInfo": {"name": "VSCode"}, "port": 4000, "authToken": "other"},
    )

    result = await discover_ide_servers(ide_base_dir=ide_temp_dir)
    assert list(result.keys()) == ["ToukaUI"]
    assert result["ToukaUI"]["url"] == "http://localhost:3005/mcp"
