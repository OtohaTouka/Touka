"""TDD — E#17：CORS 須放行 localhost / 127.0.0.1 / 私網 IP / vscode-webview。

原本精確字串比對只列 localhost:3000/3001，導致以 127.0.0.1 或區網 IP（手機/第二台裝置連
自架）開 UI 時所有 API 被瀏覽器 CORS 擋死。改用 allow_origin_regex 涵蓋本機與私網。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from src.main import app


def _allow_origin(origin: str) -> str | None:
    with TestClient(app) as client:
        resp = client.get("/health", headers={"Origin": origin})
    return resp.headers.get("access-control-allow-origin")


def test_cors_allows_localhost():
    assert _allow_origin("http://localhost:3001") == "http://localhost:3001"


def test_cors_allows_127_0_0_1():
    assert _allow_origin("http://127.0.0.1:3001") == "http://127.0.0.1:3001"


def test_cors_allows_private_lan_ip():
    assert _allow_origin("http://192.168.1.50:3001") == "http://192.168.1.50:3001"


def test_cors_allows_vscode_webview():
    assert _allow_origin("vscode-webview://abc123") == "vscode-webview://abc123"


def test_cors_blocks_foreign_origin():
    assert _allow_origin("http://evil.example.com") is None
