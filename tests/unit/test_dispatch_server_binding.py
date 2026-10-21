"""ADR-0082：dispatch server 綁定位址 secure-by-default 測試.

合併進 proxy 容器後，dispatch 與 proxy 同機（localhost IPC），程式碼預設改綁
127.0.0.1；需要對外（如 compose 發布 8083 給主機原生外部 AI）時以
DISPATCH_BIND_HOST 環境變數顯式升開——「預設安全、顯式開放」。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import server  # ToukaDispatch/server.py（conftest 已將 DISPATCH_DIR 掛入 sys.path）

DISPATCH_DIR = Path(__file__).resolve().parents[2] / "ToukaDispatch"


def test_bind_host_defaults_to_loopback(monkeypatch):
    """未設 DISPATCH_BIND_HOST → 預設 127.0.0.1（不再綁 0.0.0.0）。"""
    monkeypatch.delenv("DISPATCH_BIND_HOST", raising=False)
    assert server._bind_host() == "127.0.0.1"


def test_bind_host_env_override(monkeypatch):
    """DISPATCH_BIND_HOST 環境變數可顯式覆寫（compose 對外發布 8083 情境）。"""
    monkeypatch.setenv("DISPATCH_BIND_HOST", "10.0.0.7")
    assert server._bind_host() == "10.0.0.7"


def test_no_hardcoded_bind_all_in_source():
    """server.py 原始碼不得再出現硬編的 0.0.0.0 綁定字面值。"""
    src = (DISPATCH_DIR / "server.py").read_text(encoding="utf-8")
    assert '"0.0.0.0"' not in src, "綁定位址須走 _bind_host()（env 可覆寫、預設 loopback）"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
