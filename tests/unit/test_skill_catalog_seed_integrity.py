"""ADR-0074：skill_catalog.json.example 結構誠信驗證。

`.example` 為 tracked 種子（bootstrap 自癒植入、`get_catalog()` fallback、公開後同步來源）。
Phase A 可為空 `{"catalog": []}`；Phase C 充實後，每筆須過 schema、釘 40-hex commit、
授權為開源許可（避開 source-available 的文件類 skill）、files 無路徑穿越。
"""
from __future__ import annotations

import json
from pathlib import Path

from src.services.skill_catalog_service import (
    COMMIT_SHA_RE,
    SHA256_RE,
    SkillCatalogEntry,
)

_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "skill_catalog.json.example"
)
# 只收公認開源許可；source-available（如 anthropics 文件類 pdf/docx）不自動散佈。
_ALLOWED_LICENSES = frozenset({"Apache-2.0", "MIT", "BSD-3-Clause"})


def _load_catalog() -> list[dict]:
    data = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    return data["catalog"]


def test_example_exists_and_is_catalog():
    """skill_catalog.json.example 為 tracked 種子，須存在且 catalog 為 list。"""
    assert _EXAMPLE.exists(), "skill_catalog.json.example 應 tracked 於 repo"
    data = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data.get("catalog"), list)


def test_each_entry_pinned_licensed_and_safe():
    """每筆（若有）過 schema、釘 40-hex commit、開源授權、files 含 64-hex sha256 且無穿越。"""
    for entry in _load_catalog():
        SkillCatalogEntry.model_validate(entry)
        assert COMMIT_SHA_RE.fullmatch(entry["commit"]), entry["id"]
        assert entry.get("license") in _ALLOWED_LICENSES, entry["id"]
        for f in entry["files"]:
            path = f["path"]
            assert not path.startswith("/") and ".." not in path.split("/"), entry["id"]
            assert SHA256_RE.fullmatch(f["sha256"]), entry["id"]
