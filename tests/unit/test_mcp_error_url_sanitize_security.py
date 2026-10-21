"""MCP 診斷遮罩不再弄壞網址，本機路徑與密鑰照樣遮（禁令 23）。

舊規則把 ``http://`` 的 ``p:/`` 當成磁碟代號（``http://localhost:8000`` → ``htt<path>``），
Unix 規則也把網址路徑吃掉，健康頁與 log 的連線錯誤讀不出是哪台伺服器。

網址片段改成「白名單前綴，其餘整段遮」：http(s) 只顯示合法主機、埠與小寫路徑段，
碰到第一個不符的段、``?`` 或 ``#`` 就把剩下的換成一個 ``<redacted>``；``file:`` 與其他 scheme
整段當本機路徑遮。網址結尾和舊規則相同（空白或引號），舊規則遮掉的字一律留在網址片段裡處理。
全部是純函式測試，不連網、不碰 data/。
"""

from __future__ import annotations

import pytest
from src.protocols.manager import ProtocolManager
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.utils.mcp_secrets import sanitize_diagnostic

# 拼接建構，避開 PATH_LITERAL_SSOT 把測試輸入誤判為硬編碼路徑（這些是要被遮的假路徑）。
_W = "\\"
_USER = "alice"
_UNIX_PATH = "/" + "home" + "/" + _USER + "/" + ".ssh" + "/" + "id_rsa"
_WIN_PATH = "C" + ":" + _W + "Users" + _W + _USER + _W + "server.js"


@pytest.mark.parametrize(
    "message",
    [
        "connect failed: http://localhost:8000/mcp",
        "connect failed: https://mcp.example.test:8443/v1/mcp",
        "connect failed: http://[::1]:8000/mcp",
        "Client error '404 Not Found' for url 'http://localhost:8083/sse'",
        "redirect blocked: http://127.0.0.1:8083/mcp/ -> https://server-filesystem.example.test/mcp",
        "connect to http://touka-proxy:8000/mcp failed",
        "http://localhost:8000, retrying",
        "url: http://localhost:8000/mcp: 404",
        "(see http://localhost:8000/configured)",
        "http://h.test:99999/",
        "無法連線到http://localhost:8000/mcp",
        "failed:http://localhost:8000/mcp",
    ],
)
def test_network_urls_stay_readable(message):
    assert sanitize_diagnostic(message) == message


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("url=http://h.test/p,path=" + _UNIX_PATH, "url=http://h.test/<redacted>"),
        ("GET http://h.test/api;key=abc123 failed", "GET http://h.test/<redacted> failed"),
        ("http://" + "C" + ":/Users/" + _USER + "/x", "http://<redacted>"),
        ("http://h.test/" + _WIN_PATH, "http://h.test/<redacted>"),
        ("http://" + _UNIX_PATH, "http://<redacted>"),
        ("http://h.test/使用者/" + _USER, "http://h.test/<redacted>"),
        ("http://localhost" + _UNIX_PATH, "http://localhost/<redacted>"),
        ("http://127.0.0.1:3000/files" + _UNIX_PATH, "http://127.0.0.1:3000/files/<redacted>"),
        ("http://home/" + _USER + "/x", "http://<redacted>"),
        ("https://" + _USER + ":pw123456@h.test/mcp", "https://<redacted>"),
        ("https://h.test/mcp?api_key=abc123&user=" + _USER, "https://h.test/mcp?<redacted>"),
        ("https://h.test/mcp#token=" + _USER, "https://h.test/mcp#<redacted>"),
        ("https://hooks.h.test/services/T0AAAA/B0BBBB/" + _USER, "https://hooks.h.test/services/<redacted>"),
        ("https://h.test/s/" + _USER + "1234567/mcp", "https://h.test/s/<redacted>"),
        ("https://h.test/s/Zm9vYmFy/" + _USER + "+/mcp", "https://h.test/s/<redacted>"),
        ("http://<redacted>:8000/mcp", "http://<redacted>"),
        ("http://h.test/<redacted>/" + _USER, "http://h.test/<redacted>"),
        ("file://" + _UNIX_PATH, "file://<path>"),
        ("FILE:///" + _WIN_PATH, "FILE://<path>"),
        ("file://fileserver/share/" + _USER + ".txt", "file://<path>"),
        ("open vscode://" + _USER + "/x", "open <path>"),
        ("/" + "home" + "/" + _USER + "/secrets.txthttp://h.test", "<path><path>"),
    ],
)
def test_url_parts_that_may_carry_paths_or_secrets_are_masked(message, expected):
    out = sanitize_diagnostic(message)
    assert out == expected
    assert _USER not in out


@pytest.mark.parametrize(
    "message",
    [
        "file:" + _UNIX_PATH,
        "spawn " + _WIN_PATH + " failed",
        "error at " + _UNIX_PATH + " line 3",
        "spawn " + _W + _W + "fileserver" + _W + "share" + _W + _USER + ".js failed",
    ],
)
def test_local_paths_outside_urls_are_still_masked(message):
    out = sanitize_diagnostic(message)
    assert "<path>" in out
    assert _USER not in out


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("/" + _USER + "7:s3://b", "<path><path>"),
        ("/" + _USER + ":s3://b", "<path><path>"),  # 字母＋冒號不能讓 Win 規則先吃掉哨兵
        ("see /" + _USER + "7(s3://bucket/key)", "see <path><path>)"),
        ("/" + _USER + "=h2://x", "<path><path>"),
    ],
)
def test_single_segment_path_touching_a_url_is_still_masked(message, expected):
    """舊 Unix 規則借網址 :// 的斜線湊成兩段而遮掉；把網址切開後不能因此露出來。"""
    assert sanitize_diagnostic(message) == expected


def test_urls_and_local_paths_in_one_message():
    out = sanitize_diagnostic("spawn " + _WIN_PATH + " then http://localhost:8000/mcp")
    assert out == "spawn <path> then http://localhost:8000/mcp"


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("https://h.test/mcp failed with sk-abcdef1234567890xyz", "sk-abcdef1234567890xyz"),
        ("https://" + "a" * 40 + ".example.test/mcp", "a" * 40),
        ("http://localhost:8000/mcp AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ],
)
def test_generic_secret_patterns_still_apply_to_urls(message, secret):
    out = sanitize_diagnostic(message)
    assert secret not in out
    assert "<redacted>" in out


def test_url_cut_by_the_scan_limit_is_masked_whole():
    """截斷切在帳密中間（前段像主機加埠）仍不能顯示。

    前面的長 token 被遮短，網址才會落在顯示範圍內。
    """
    tail = "https://" + _USER + ":12345678@h.test/mcp"
    for cut in range(1, len(tail)):
        out = sanitize_diagnostic("a" * (1999 - cut) + " " + tail)
        assert out.startswith("<redacted> ")
        assert _USER not in out
        assert ":12" not in out


def test_server_env_values_inside_urls_are_redacted_precisely():
    planted = "abc123DEF456"  # 短於通用門檻，只能靠逐一遮罩 env 值
    registry = ProtocolRegistry("__no_such_mcp_config__.json")
    registry.servers = {"s": ProtocolServerConfig(command="x", args=[], env={"API_KEY": planted})}
    message = "GET http://h.test/v1/" + planted + "/mcp failed"
    out = ProtocolManager(registry)._sanitize_for("s", message)
    assert planted not in out
    assert out == "GET http://h.test/v1/<redacted> failed"


@pytest.mark.parametrize(
    "message",
    [
        "http://h:abc/",
        "http://h＃x/mcp",
        "http://",
        "http://@",
        "http://[::1",
        "http://[zz]/x",
        "https://" + "a." * 1200,
        "https://" + "a." * 900 + "_x",
        "http://" + "a" * 3000,
    ],
)
def test_malformed_urls_never_raise(message):
    out = sanitize_diagnostic(message)
    assert isinstance(out, str)
    assert len(out) <= 300


def test_generated_inputs_never_raise():
    """固定公式產生的雜亂輸入：遮罩在例外處理與 log 裡被呼叫，自己不能丟例外。"""
    alphabet = "aZ09:/@[]%?#.-_~+=;,\\ '\"<>＃使"
    prefixes = ["http://", "https://", "file://", ""]
    for i in range(500):
        body = "".join(alphabet[(i * 7 + j * j * 13) % len(alphabet)] for j in range(i % 81))
        out = sanitize_diagnostic(prefixes[i % len(prefixes)] + body)
        assert isinstance(out, str)
