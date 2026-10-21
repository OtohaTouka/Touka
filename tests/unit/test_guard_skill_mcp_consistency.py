"""Guard #37（ADR-0102）自我測試：Skill ↔ MCP 命名一致性。

以 importlib 載入守衛、餵 in-memory 資料給純函式；並驗證現況 repo 全綠
（技能 .mcp.json 宣告的 hub-installable MCP key 皆與 Hub 安裝 key 一致）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "guard_skill_mcp_consistency.py"


def _load():
    spec = importlib.util.spec_from_file_location("guard_skill_mcp_under_test", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # @dataclass 需能由 sys.modules 解析 __module__
    spec.loader.exec_module(mod)
    return mod


_DOCGEN_CURATED = {
    "id": "curated-mcp-docgen",
    "name": "mcp-docgen",
    "source": "curated",
    "command": "uvx",
    "args": ["mcp-docgen"],
}


def test_derive_server_key():
    g = _load()
    assert g.derive_server_key("mcp-docgen") == "mcp-docgen"
    assert g.derive_server_key("My Cool Server") == "my-cool-server"


def test_find_violations_flags_key_mismatch():
    """技能用了 id（curated-mcp-docgen）而非安裝 key（mcp-docgen）→ 違規。"""
    g = _load()
    decls = [
        {
            "skill_id": "document-generator",
            "key": "curated-mcp-docgen",
            "recipe": ("uvx", ("mcp-docgen",)),
        },
    ]
    bad = g.find_violations([_DOCGEN_CURATED], decls)
    assert len(bad) == 1
    assert bad[0].declared_key == "curated-mcp-docgen"
    assert bad[0].expected_key == "mcp-docgen"


def test_find_violations_ok_when_key_matches():
    g = _load()
    decls = [
        {"skill_id": "document-generator", "key": "mcp-docgen", "recipe": ("uvx", ("mcp-docgen",))},
    ]
    assert g.find_violations([_DOCGEN_CURATED], decls) == []


def test_find_violations_ignores_non_hub_recipe():
    """SSE/url 類（無 command）不可 hub 安裝 → 即使 key 與某條目不同也不檢查。"""
    g = _load()
    decls = [
        {"skill_id": "touka-dispatch-coordinator", "key": "touka-dispatch", "recipe": (None, ())},
    ]
    assert g.find_violations([_DOCGEN_CURATED], decls) == []


def test_real_repo_skill_mcp_clean():
    """現況 repo：技能 .mcp.json 的 hub-installable MCP key 皆與 Hub 安裝 key 一致。"""
    g = _load()
    assert g.run_all() == []


def _owned_sources(tmp_path, monkeypatch):
    guard = _load()
    monkeypatch.setattr(guard, "_HUB_FILES", [tmp_path / p.name for p in guard._HUB_FILES])
    monkeypatch.setattr(guard, "_SKILLS_DIR", tmp_path / "skills")
    (tmp_path / "mcp_hub.json.example").write_text('{"hub":[]}', encoding="utf-8")
    declaration = tmp_path / "skills" / "document-generator" / ".mcp.json"
    declaration.parent.mkdir(parents=True)
    declaration.write_text(
        '{"mcp-docgen":{"command":"uvx","args":["mcp-docgen"]}}', encoding="utf-8"
    )
    return guard, declaration


def test_public_sources_no_private_hub_or_unlisted_declarations(tmp_path, monkeypatch):
    guard, declaration = _owned_sources(tmp_path, monkeypatch)
    original_exists, original_read = Path.exists, Path.read_text

    def allowed(path):
        assert path.name != "mcp_hub.json", "private hub probe/read"
        assert path.name != ".mcp.json" or path == declaration, "unlisted declaration"

    def exists(path):
        allowed(path)
        return original_exists(path)

    def read(path, *args, **kwargs):
        allowed(path)
        return original_read(path, *args, **kwargs)

    def glob(path, pattern, **kwargs):
        pytest.fail("declaration discovery must not glob private files")

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "glob", glob)
    assert guard.run_all() == []


@pytest.mark.parametrize("target", ["hub", "declaration"])
@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_public_sources_missing_or_malformed_fails(tmp_path, monkeypatch, target, damage):
    guard, declaration = _owned_sources(tmp_path, monkeypatch)
    path = tmp_path / "mcp_hub.json.example" if target == "hub" else declaration
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("[", encoding="utf-8")
    assert guard.run_all()
