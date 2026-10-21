"""TDD — SSRF Outbound URL Guard（Phase 2a）。

威脅模型：自架、單一擁有者、連線 admin-gated；使用者合法會指向 loopback/私網/區網 LLM。
守衛**放行 loopback/私網/公網**，**封鎖** link-local/metadata、未指定、多播、保留、廣播、
非 http(s) scheme，並涵蓋 IPv4-mapped IPv6 與十進位整數型 IP 繞過。
"""

from __future__ import annotations

import pytest
from src.services.models.fetcher import fetch_openai_compatible_models
from src.utils.ssrf_guard import (
    UnsafeUrlError,
    is_safe_outbound_url,
    validate_outbound_url,
)

# --- 放行：自架/區網/公網（合法） ---

@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",            # Ollama
        "http://127.0.0.1:4000",             # LiteLLM
        "http://192.168.1.50:8000/v1",       # 區網 LLM 伺服器
        "http://10.0.0.5:8080",              # 私網
        "http://172.16.3.4",                 # 私網
        "http://touka-searxng:8080",         # 容器 DNS 名稱
        "https://api.openai.com/v1",         # 公網
        "https://api.deepseek.com/v1",       # 公網
        "http://[::1]:11434",                # IPv6 loopback
        "http://[fd00::1]:8000",             # IPv6 私網（ULA）
    ],
)
def test_allows_legitimate_targets(url):
    validate_outbound_url(url)  # 不應拋例外
    assert is_safe_outbound_url(url) is True


# --- 封鎖：雲端 metadata / link-local ---

@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",       # AWS/GCP metadata
        "http://169.254.169.254",
        "http://[fe80::1]/",                                # IPv6 link-local
        "http://[::ffff:169.254.169.254]/",                 # IPv4-mapped IPv6 繞過
        "http://2852039166/",                               # 十進位整數型 169.254.169.254
        "http://0xa9.0xfe.0xa9.0xfe/",                       # 十六進位點分（繞過 #1）
        "http://0251.0376.0251.0376/",                      # 八進位點分（繞過 #2）
        "http://0xa9fea9fe/",                               # 十六進位 dword
    ],
)
def test_blocks_metadata_and_link_local(url):
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(url)
    assert is_safe_outbound_url(url) is False


# --- 拒絕內嵌帳密（userinfo）的 URL（金鑰外洩面） ---

@pytest.mark.parametrize(
    "url",
    [
        "http://user:pass@localhost:11434",      # 即使是 localhost 也擋（內嵌帳密）
        "http://admin:secret@169.254.169.254",
        "http://token@api.example.com/v1",       # 僅 user 也擋
    ],
)
def test_blocks_url_with_userinfo(url):
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(url)


# --- 封鎖：未指定/多播/廣播/保留 ---

@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:8000",        # 未指定
        "http://[::]/",               # IPv6 未指定
        "http://224.0.0.1/",          # 多播
        "http://255.255.255.255/",    # 廣播
        "http://240.0.0.1/",          # 保留（240/4）
    ],
)
def test_blocks_unspecified_multicast_reserved(url):
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(url)


# --- 封鎖：非 http(s) scheme（協定走私） ---

@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/_INFO",
        "ftp://example.com/x",
        "data:text/plain,hi",
    ],
)
def test_blocks_non_http_schemes(url):
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(url)


# --- 邊界：空/無主機 ---

@pytest.mark.parametrize("url", ["", "   ", "http://", "https:///path"])
def test_blocks_empty_or_hostless(url):
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(url)


def test_metadata_not_blocked_via_private_allow():
    """確認 169.254.169.254 被擋，但同屬「非公網」的私網 10.x 不被誤擋。"""
    assert is_safe_outbound_url("http://169.254.169.254") is False
    assert is_safe_outbound_url("http://10.1.2.3") is True


# --- 縱深防禦：發現抓取器在不安全 base_url 時拋例外 ---

@pytest.mark.asyncio
async def test_fetch_openai_compatible_rejects_unsafe_base_url():
    with pytest.raises(UnsafeUrlError):
        await fetch_openai_compatible_models(
            "http://169.254.169.254", "key", "custom", lambda _x: False
        )

