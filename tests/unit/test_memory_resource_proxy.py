"""ToukaMemory 資源代理工廠測試（ADR-0116：整合模式單一部署）。

ToukaMemory 退役獨立 MCP 模式後，`get_resource_proxy()` 只回傳 ToukaProxy 的真實
`ResourceManager`（完整 I/O＋身分隔離＋路徑保護）；取不到即 fail-loud，不再靜默降級至
無保護的 `_FallbackResourceProxy`。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# 確保 ToukaMemory 模組可被導入
sys.path.insert(0, str(Path(__file__).parents[2] / "ToukaMemory" / "src"))

from memory_server.config_utils import _reset_resource_proxy_cache, get_resource_proxy
from src.services.resource_manager import resource_manager

# IResourceProxy 協定宣告的方法（整合模式下由真實 ResourceManager 實作）。
_IRESOURCE_PROXY_METHODS = (
    "read_file_text",
    "write_file_text",
    "append_file_text",
    "exists",
    "make_dir",
    "remove_file",
    "list_directory",
    "glob_search",
    "is_protected_path",
    "get_vault_path",
)


@pytest.fixture(autouse=True)
def reset_proxy_cache():
    """每個測試前後重置快取，避免跨測試污染。"""
    _reset_resource_proxy_cache()
    yield
    _reset_resource_proxy_cache()


def test_get_resource_proxy_returns_real_resource_manager() -> None:
    """整合模式：回傳 ToukaProxy 的 ResourceManager 單例（具完整 IResourceProxy 介面）。"""
    proxy = get_resource_proxy()
    assert proxy is resource_manager
    # 介面契約：身分隔離 / 路徑保護皆為真實實作（非降級代理的 None / False）。
    for method in _IRESOURCE_PROXY_METHODS:
        assert callable(getattr(proxy, method)), f"ResourceManager 缺 IResourceProxy 方法：{method}"


def test_get_resource_proxy_fail_loud_without_resource_manager() -> None:
    """fail-loud：取不到 ResourceManager 即拋錯（不再靜默降級至無保護 I/O）。"""
    bad_modules = {"src": None, "src.services": None, "src.services.resource_manager": None}
    with patch.dict("sys.modules", bad_modules):
        _reset_resource_proxy_cache()
        with pytest.raises((ImportError, ModuleNotFoundError)):
            get_resource_proxy()


def test_get_resource_proxy_cache_reuse() -> None:
    """連續兩次呼叫應回傳同一個實例（快取機制）。"""
    proxy1 = get_resource_proxy()
    proxy2 = get_resource_proxy()
    assert proxy1 is proxy2
