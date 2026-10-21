"""Batch 2/3（ADR-0102）：技能↔MCP 接線——啟動遷移舊 server key ＋ 接線健檢。

以顯式 config_dir（tmp_path）測純邏輯，零真實 MCP/網路。
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from src.utils.skill_mcp_wiring import migrate_stale_server_keys, validate_skill_mcp_wiring

if TYPE_CHECKING:
    from pathlib import Path

_CURATED_HUB = {
    "hub": [
        {
            "id": "curated-mcp-docgen", "name": "mcp-docgen", "source": "curated",
            "command": "uvx", "args": ["mcp-docgen"],
        },
    ]
}


def _setup(tmp: Path, servers: dict, hub: dict | None = None) -> None:
    (tmp / "server_config.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )
    (tmp / "mcp_hub.json").write_text(
        json.dumps(hub if hub is not None else _CURATED_HUB), encoding="utf-8"
    )


def _servers(tmp: Path) -> dict:
    return json.loads((tmp / "server_config.json").read_text(encoding="utf-8"))["mcpServers"]


def _setup_skill(tmp: Path, skill_id: str, mcp: dict) -> None:
    sd = tmp / "skills" / skill_id
    sd.mkdir(parents=True, exist_ok=True)
    (sd / ".mcp.json").write_text(json.dumps(mcp), encoding="utf-8")


# ---- Batch 3：遷移舊 key ----

def test_migrate_renames_stale_curated_key(tmp_path):
    _setup(tmp_path, {
        "curated-mcp-docgen": {"command": "uvx", "args": ["mcp-docgen"]},
        "my-custom": {"command": "python", "args": ["x.py"]},
    })
    renames = migrate_stale_server_keys(config_dir=tmp_path)
    assert renames == [("curated-mcp-docgen", "mcp-docgen")]
    keys = set(_servers(tmp_path).keys())
    assert "mcp-docgen" in keys and "curated-mcp-docgen" not in keys
    assert "my-custom" in keys  # 自訂 server 不動


def test_migrate_idempotent(tmp_path):
    _setup(tmp_path, {"curated-mcp-docgen": {"command": "uvx", "args": ["mcp-docgen"]}})
    assert migrate_stale_server_keys(config_dir=tmp_path) == [("curated-mcp-docgen", "mcp-docgen")]
    assert migrate_stale_server_keys(config_dir=tmp_path) == []  # 再跑無變更


def test_migrate_skips_when_target_exists(tmp_path):
    """目標 key 已存在 → 不覆蓋、不改名（保護既有/重裝後的正確 key）。"""
    _setup(tmp_path, {
        "curated-mcp-docgen": {"command": "uvx", "args": ["old"]},
        "mcp-docgen": {"command": "uvx", "args": ["new"]},
    })
    assert migrate_stale_server_keys(config_dir=tmp_path) == []
    assert set(_servers(tmp_path).keys()) == {"curated-mcp-docgen", "mcp-docgen"}


# ---- Batch 2：接線健檢 ----

def test_validate_warns_when_unregistered(tmp_path):
    _setup_skill(
        tmp_path, "document-generator",
        {"mcp-docgen": {"command": "uvx", "args": ["mcp-docgen"]}},
    )
    warns = validate_skill_mcp_wiring(set(), config_dir=tmp_path)
    assert len(warns) == 1
    assert "mcp-docgen" in warns[0] and "document-generator" in warns[0]


def test_validate_ok_when_registered(tmp_path):
    _setup_skill(
        tmp_path, "document-generator",
        {"mcp-docgen": {"command": "uvx", "args": ["mcp-docgen"]}},
    )
    assert validate_skill_mcp_wiring({"mcp-docgen"}, config_dir=tmp_path) == []


def test_validate_ignores_sse_server(tmp_path):
    """SSE 非 Touka 支援的傳輸（ADR-0192 僅收 streamable HTTP）→ 不納入接線健檢。"""
    _setup_skill(
        tmp_path, "touka-dispatch-coordinator",
        {"touka-dispatch": {"type": "sse", "url": "${DISPATCH_URL}/sse"}},
    )
    assert validate_skill_mcp_wiring(set(), config_dir=tmp_path) == []


def test_validate_warns_for_unregistered_remote_server(tmp_path):
    """streamable HTTP remote 自 ADR-0192 起可安裝 → 未註冊時應比照 stdio 告警。

    舊判準「無 command 就略過」是 stdio-only 年代的產物：remote 條目現在裝得起來，
    卻會靜默逃過接線健檢（Guard #37 的盲區）。
    """
    _setup_skill(
        tmp_path, "docs-helper",
        {"ms-learn": {"url": "https://learn.microsoft.com/api/mcp"}},
    )
    warns = validate_skill_mcp_wiring(set(), config_dir=tmp_path)
    assert len(warns) == 1
    assert "ms-learn" in warns[0] and "docs-helper" in warns[0]


def test_validate_ok_when_remote_registered(tmp_path):
    _setup_skill(
        tmp_path, "docs-helper",
        {"ms-learn": {"url": "https://learn.microsoft.com/api/mcp"}},
    )
    assert validate_skill_mcp_wiring({"ms-learn"}, config_dir=tmp_path) == []
