"""移除 ``POST /mcp/scan``（npm ``mcp-scan``）的回歸測試。

舊端點每次以 ``npx -y mcp-scan@latest`` 執行未釘版的第三方套件，子程序帶完整
Proxy 環境與使用者剛輸入的 MCP env 值；該套件不讀傳入的設定檔，``safe`` 結果
沒有意義。端點與前端兩處掃描按鈕一併移除，安裝前的人工審核改看視窗中的完整指令。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from src.main import app

ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOTS = (ROOT / "ToukaProxy" / "src", ROOT / "ToukaChat" / "src")
_SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".json"}
_FORBIDDEN = ("mcp-scan", "/mcp/scan")


def test_scan_route_is_not_registered():
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/mcp/scan" not in paths
    assert "/v1/mcp/scan" not in paths


def test_openapi_contract_has_no_scan_path():
    spec = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    assert not [path for path in spec["paths"] if path.endswith("/mcp/scan")]


@pytest.mark.parametrize("needle", _FORBIDDEN)
def test_sources_do_not_reference_mcp_scan(needle: str):
    hits = [
        path.relative_to(ROOT).as_posix()
        for source_root in _SOURCE_ROOTS
        for path in source_root.rglob("*")
        if path.suffix in _SOURCE_SUFFIXES
        and "node_modules" not in path.parts
        and needle in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert hits == []
