"""G03（守衛涵蓋缺口待辦）— LDH 無卡路徑刻意不帶類型旗標＝身分冒用防護。

缺口背景（見 docs/architecture/proposals/guard-coverage-gap-backlog.md · G03）：
    ADR-0147 已把 identity_router.py 的 LDH（無卡首裝視窗）修成 plain
    ``get_role_key(target_id)``——不帶 is_toukasoul/is_custom 旗標。X-Agent-ID 為
    攻擊者可控；帶旗標會讓 ``X-Agent-ID=<既存靈魂 id>`` 解析成 Owner，鑄一張綁定該
    具名靈魂（記憶 vault/home 錨點）的 Owner 卡＝身分冒用／提權。

    **但此不變式先前零測試**：既有 LDH 測試（test_ldh_first_run_gate.py）全數
    ``patch(...get_role_key)``＝把被保護的解析整顆換掉，於是「有人為與帶卡路徑一致
    而在此呼叫補上旗標／改呼 resolve_role_and_caps」的回歸照樣全綠。

本檔補上真正驅動解析的回歸鎖：**不 mock get_role_key**，以受控 identity_map
（is_toukasoul→Owner）＋一個既存靈魂 id 走真實無旗標解析，斷言落回 Base（非 Owner）。
若有人在 identity_router.py:184 加回 is_toukasoul=True（或改呼帶旗標的
resolve_role_and_caps），本測試即變紅。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import Request
from memory_server import config_utils
from src.models.identity import IdentityMapConfig
from src.services.auth_service import AuthService
from src.services.identity_router import get_optional_identity
from src.services.permission_service import permission_service

# 一個「既存靈魂」形態的 X-Agent-ID（攻擊者可控輸入）。刻意不在 agent_overrides 內，
# 使其角色完全取決於「有沒有帶 is_toukasoul 旗標」——正是本不變式要鎖的分岔點。
SOUL_ID = "ToukaSoul_victim_abc123"

# 受控 identity_map：is_toukasoul→Owner（帶旗標即提權的溫床）、default→Base（無旗標的安全著陸）。
_OWNER_CAPS = ["heartbeat_awakening", "can_supervise_dispatch"]
_CONTROLLED_MAP = {
    "tag_rules": {
        "is_toukasoul": {"role": "Owner", "capabilities": _OWNER_CAPS},
    },
    "agent_overrides": {
        "DebugAgent": {"role": "Debugger", "capabilities": _OWNER_CAPS},
    },
    "default_role": "Base",
}


def _wipe_users(auth: AuthService) -> None:
    auth._atomic_users_write(lambda users: users.clear())  # noqa: SLF001


@pytest.fixture(autouse=True)
def _users_hygiene():
    """LDH 依賴「系統未認領」；測前清空、測後復原沙箱 users.json（順序依賴防護）。"""
    _wipe_users(AuthService())
    yield
    _wipe_users(AuthService())


@pytest.fixture
def _controlled_identity_map(monkeypatch: pytest.MonkeyPatch):
    """把 permission_service.identity_map 釘成受控值，並停用自動重載（否則 get_role_key
    內的 _auto_reload_check 會從磁碟覆蓋我們的受控 map）。"""
    controlled = IdentityMapConfig.model_validate(_CONTROLLED_MAP)
    monkeypatch.setattr(permission_service, "identity_map", controlled)
    monkeypatch.setattr(permission_service, "_auto_reload_check", lambda: None)
    # R-2a：靈魂旗標要經原始 ID 確認，所以在沙箱建一個真的靈魂目錄（conftest 每測試清空）。
    soul_dir = Path(config_utils.get_app_data_dir()) / "soul" / SOUL_ID
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "config.json").write_text(json.dumps({"is_toukasoul": True}), encoding="utf-8")
    return controlled


def _mk_request(headers: dict | None = None, host: str = "127.0.0.1") -> MagicMock:
    req = MagicMock(spec=Request)
    req.client.host = host
    req.headers = headers or {}
    req.state = SimpleNamespace()
    return req


# ── 非空砲前提：證明這個靈魂 id 確實是「帶旗標即 Owner」的真實威脅向量 ──────────────


def test_flagged_resolution_would_be_owner(_controlled_identity_map):
    """帶 is_toukasoul 旗標時，SOUL_ID 解析為 Owner——這正是 LDH 若補旗標會鑄出的提權卡。
    （本斷言確保下方的 Base 鎖非空砲：差別完全來自旗標。）"""
    assert permission_service.get_role_key(SOUL_ID, is_toukasoul=True) == "Owner"


def test_flagless_resolution_is_base(_controlled_identity_map):
    """不帶旗標（LDH 現行呼叫形態）時，同一 SOUL_ID 落回 default Base。"""
    assert permission_service.get_role_key(SOUL_ID) == "Base"


# ── 端到端回歸鎖：真實 LDH 分支，不 mock get_role_key ─────────────────────────────


@pytest.mark.asyncio
async def test_ldh_cardless_soul_id_resolves_base_not_owner(_controlled_identity_map):
    """回歸鎖：未認領（首裝）＋無卡＋X-Agent-ID=<既存靈魂 id> 走真實無旗標解析，
    必須鑄出 Base 卡、絕不得是 Owner。

    若 identity_router.py:184 回歸為帶 is_toukasoul=True（或改呼帶旗標的
    resolve_role_and_caps），ident.agent_role 會變 Owner，本測試變紅。
    """
    _wipe_users(AuthService())  # 未認領＝LDH 可用
    req = _mk_request(headers={"X-Agent-ID": SOUL_ID}, host="127.0.0.170")

    ident = await get_optional_identity(req, None)

    assert ident is not None, "首裝無卡本機請求應優雅降級（非 None）"
    assert ident.agent_id == SOUL_ID
    assert ident.agent_role == "Base", (
        f"LDH 無卡路徑把攻擊者可控的 X-Agent-ID 解析成 {ident.agent_role}"
        "（應為 Base）＝身分冒用面回歸（見 ADR-0147／G03）"
    )
    assert ident.agent_role != "Owner"
