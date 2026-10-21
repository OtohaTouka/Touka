"""G-1：明列出貨的 document-generator 宣告維持 stdio recipe。

此檢查僅涵蓋 Guard37 共享清單，不讀本機未登記宣告，不把此 fixture 契約
延伸為 Proxy 全域 stdio-only；目前 modern HTTP 支援不受影響。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SKILLS_DIR = Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "skills"
_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "guard_skill_mcp_consistency.py"


def _shipped_declarations() -> tuple[Path, ...]:
    spec = importlib.util.spec_from_file_location("shipped_mcp_guard", _GUARD)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.SHIPPED_MCP_DECLARATIONS


def _mcp_files() -> list[Path]:
    return [_SKILLS_DIR / relative for relative in _shipped_declarations()]


def test_skills_dir_exists() -> None:
    assert _SKILLS_DIR.is_dir(), f"技能目錄不存在：{_SKILLS_DIR}"


@pytest.mark.parametrize("mcp_path", _mcp_files(), ids=lambda p: p.parent.name)
def test_skill_mcp_servers_are_stdio_mountable(mcp_path: Path) -> None:
    """本次明列出貨 fixture 的伺服器應維持 stdio/command recipe。"""
    data = json.loads(mcp_path.read_text(encoding="utf-8"))
    for name, cfg in data.items():
        assert isinstance(cfg, dict), f"{mcp_path.parent.name}: 伺服器 {name} config 非物件"
        assert cfg.get("command"), (
            f"技能 '{mcp_path.parent.name}' 宣告的 MCP 伺服器 '{name}' 無 command"
            f"（type={cfg.get('type')!r}）——不符合此出貨 fixture 的 stdio recipe 契約。"
        )
