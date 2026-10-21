"""IO Prison 路徑主權 pytest 回歸鎖（升格自 ``tests/smoke/smoke_io_prison.py``）。

早期 ADR 守衛缺口 E02（ADR-0022）：``_enforce_root_bound`` 是 vault 檔案操作唯一的
path-escape 防線（``ResourceManager.read_file_text/write_file_text`` 為 raw Path IO、
無自帶沙盒），但其唯一 exercise 位於 ``smoke_io_prison.py``——pytest
（``python_files=test_*.py``）永不收集、guard_all 不引、CI 不跑＝削弱逃逸檢查可
通過全部自動閘。本檔把 smoke 的安全斷言升格為 pytest 可收集的回歸鎖，並加上
「真實 core ops 接線」測試（守衛必須擋在讀寫路徑上，而非只有純函式可被呼叫）。
"""

import sys
from pathlib import Path

import pytest
from memory_server.tools.core_file_ops import (
    PathGuardError,
    _enforce_root_bound,
    core_read,
    core_write,
)
from src.models.identity import AgentIdentity
from src.services.resource_manager import resource_manager

# ---------------------------------------------------------------------------
# 1) _enforce_root_bound 純函式：逃逸必拒、合法必過
# ---------------------------------------------------------------------------

_TRAVERSAL_PAYLOADS = [
    "../",
    "../../etc/passwd",
    "personal/../../../Windows/System32",
    "drive/../../secret.txt",
]


@pytest.mark.parametrize("payload", _TRAVERSAL_PAYLOADS)
def test_traversal_payloads_rejected(tmp_path: Path, payload: str) -> None:
    """`../` 路徑穿越一律 PathGuardError（smoke §2 升格）。"""
    root = tmp_path / "vault"
    root.mkdir()
    with pytest.raises(PathGuardError):
        _enforce_root_bound(payload, str(root))


def test_absolute_path_injection_rejected(tmp_path: Path) -> None:
    """絕對路徑注入（指向 root 之外）必拒（smoke §4 升格，跨平台版）。"""
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside" / "secret.txt"
    with pytest.raises(PathGuardError):
        _enforce_root_bound(str(outside), str(root))
    # 平台代表性系統路徑（smoke 原始攻擊 payload，非業務路徑）
    sys_path = "C:\\Windows\\win.ini" if sys.platform == "win32" else "/etc/shadow"  # noqa: PATH_LITERAL_SSOT
    with pytest.raises(PathGuardError):
        _enforce_root_bound(sys_path, str(root))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 磁碟代號限定")
def test_cross_drive_escape_rejected(tmp_path: Path) -> None:
    """跨磁碟逃逸（Z:\\）必拒（smoke §5 升格）。"""
    with pytest.raises(PathGuardError):
        _enforce_root_bound("Z:\\some_file.txt", str(tmp_path))  # noqa: PATH_LITERAL_SSOT


def test_legit_paths_resolve_inside_root(tmp_path: Path) -> None:
    """非空砲：合法相對路徑解析為 root 內的絕對路徑、'.' 回 root 本身。"""
    root = tmp_path / "vault"
    root.mkdir()
    resolved = _enforce_root_bound("notes/today.md", str(root))
    assert resolved.is_relative_to(root.resolve())
    assert _enforce_root_bound(".", str(root)) == root.resolve()


# ---------------------------------------------------------------------------
# 2) core ops 接線：守衛必須擋在真實讀寫路徑上
# ---------------------------------------------------------------------------


async def test_core_read_blocks_traversal(tmp_path: Path) -> None:
    """core_read 帶穿越路徑：不得洩漏 root 外檔案內容、回 Error 字串。"""
    root = tmp_path / "vault"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")

    result = await core_read("../secret.txt", str(root))

    assert "TOP-SECRET-CONTENT" not in result
    assert "Error" in result


async def test_core_write_blocks_traversal(tmp_path: Path) -> None:
    """core_write 帶穿越路徑：root 外不得出現任何寫入產物。"""
    root = tmp_path / "vault"
    root.mkdir()

    result = await core_write("../evil.txt", str(root), "pwned")

    assert "Error" in result
    assert not (tmp_path / "evil.txt").exists()


# ---------------------------------------------------------------------------
# 3) 身分隔離與靈魂保護（smoke §1/§3 升格）
# ---------------------------------------------------------------------------


def test_identity_vaults_isolated() -> None:
    """兩個身分的 personal vault 路徑必不同、且各含自己的 agent_id。"""
    alice = AgentIdentity.for_role(agent_id="Alice", role="Base")
    bob = AgentIdentity.for_role(agent_id="Bob", role="Base")

    alice_vault = resource_manager.get_vault_path("personal", alice)
    bob_vault = resource_manager.get_vault_path("personal", bob)

    assert alice_vault != bob_vault
    assert "Alice" in str(alice_vault)
    assert "Bob" in str(bob_vault)


def test_soul_protection_blocks_direct_write() -> None:
    """ResourceManager 直寫 soul/ 禁區必 PermissionError（自動編修防護）。"""
    soul_file = Path(resource_manager.get_base_path()) / "soul" / "Alice" / "config.json"
    with pytest.raises(PermissionError):
        resource_manager.write_file_text(soul_file, "{}")
