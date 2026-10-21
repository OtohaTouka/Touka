"""remote MCP 連線設定的解析與驗證（ADR-0192，批次 2 / L1）。

三家桌面版（Claude Code／Codex／Antigravity）的 remote MCP 都收斂到 streamable HTTP
＋靜態 header 帶憑證。Touka 的對應設計把**憑證留在 ``env``**、header 只寫 ``${VAR}``
引用——如此可直接繼承既有的 env 遮罩與「前端不得清除磁碟既值」保護
（``routers/mcp.py`` 的 ``_preserve_masked_env``），不必為 header 另造一套密鑰保護。

本檔守三件事：remote/stdio 形狀判別、URL 安全閘（帶密鑰的請求不得走明文 http
外傳）、header 插值（缺變數＝fail-closed，不送半成品憑證）。
"""

from __future__ import annotations

import pytest

from src.utils.mcp_remote import (
    is_remote_server_config,
    resolve_headers,
    validate_remote_url,
)


class TestShapeDetection:
    def test_url_means_remote_command_means_stdio(self):
        assert is_remote_server_config({"url": "https://x.example/mcp"}) is True
        assert is_remote_server_config({"command": "npx", "args": []}) is False
        assert is_remote_server_config({}) is False


class TestUrlSafetyGate:
    def test_https_accepted(self):
        validate_remote_url("https://mcp.example.com/mcp")

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:3000/mcp",
            "http://127.0.0.1:8080/mcp",
            "http://[::1]:8080/mcp",
        ],
    )
    def test_plain_http_allowed_only_on_loopback(self, url):
        """本機自架 server 沒有憑證外傳風險，允許明文。"""
        validate_remote_url(url)

    def test_plain_http_to_remote_host_rejected(self):
        """header 會帶憑證——明文 http 外傳等於把金鑰貼在網路上。"""
        with pytest.raises(ValueError, match="https"):
            validate_remote_url("http://mcp.example.com/mcp")

    @pytest.mark.parametrize(
        "url",
        ["file:///etc/passwd", "ftp://x.example/mcp", "javascript:alert(1)", "notaurl"],
    )
    def test_non_http_schemes_rejected(self, url):
        with pytest.raises(ValueError):
            validate_remote_url(url)


class TestHeaderInterpolation:
    def test_env_reference_is_substituted(self):
        headers = resolve_headers(
            {"Authorization": "Bearer ${GH_PAT}", "X-Fixed": "static"},
            {"GH_PAT": "ghp_secret"},
        )
        assert headers == {"Authorization": "Bearer ghp_secret", "X-Fixed": "static"}

    def test_multiple_references_in_one_value(self):
        headers = resolve_headers(
            {"X-Combo": "${A}/${B}"}, {"A": "one", "B": "two"}
        )
        assert headers == {"X-Combo": "one/two"}

    def test_missing_env_is_fail_closed(self):
        """缺憑證＝不連，而不是送出帶著 `${VAR}` 字面值的無效 header。

        對照 ADR-0191 的 Notion 教訓：能連上但每次呼叫都 401 的「殭屍伺服器」比
        乾脆連不上更難診斷。
        """
        with pytest.raises(ValueError, match="GH_PAT"):
            resolve_headers({"Authorization": "Bearer ${GH_PAT}"}, {})

    def test_none_and_empty_are_passthrough(self):
        assert resolve_headers(None, {}) == {}
        assert resolve_headers({}, {"A": "1"}) == {}
