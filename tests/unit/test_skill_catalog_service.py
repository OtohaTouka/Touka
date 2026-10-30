"""ADR-0074：SkillCatalogService 讀取鏈 ＋ entry schema 測試。"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from src.services.skill_catalog_service import SkillCatalogEntry, SkillCatalogService

_VALID_ENTRY: dict = {
    "id": "skill-creator",
    "name": "Skill Creator",
    "category": "Dev",
    "description": "Build new skills.",
    "repo": "anthropics/skills",
    "commit": "a" * 40,
    "base_path": "skills/skill-creator",
    "files": [{"path": "SKILL.md", "sha256": "a" * 64}],
    "author": "Anthropic",
    "license": "Apache-2.0",
}


# 目錄檔放在測試自己的資料夾，經真的 ResourceManager 讀（ADR-0293 T3：不換掉 resource_manager）
def test_get_catalog_reads_from_json(tmp_path):
    catalog = tmp_path / "skill_catalog.json"
    catalog.write_text(json.dumps({"catalog": [_VALID_ENTRY]}), encoding="utf-8")
    svc = SkillCatalogService(catalog_path=str(catalog))
    assert svc.get_catalog() == [_VALID_ENTRY]


def test_get_catalog_falls_back_to_example(tmp_path):
    catalog = tmp_path / "skill_catalog.json"
    seed = tmp_path / "skill_catalog.json.example"
    seed.write_text(json.dumps({"catalog": [_VALID_ENTRY]}), encoding="utf-8")
    svc = SkillCatalogService(catalog_path=str(catalog))
    assert svc.get_catalog() == [_VALID_ENTRY]


def test_get_catalog_empty_when_both_missing(tmp_path):
    svc = SkillCatalogService(catalog_path=str(tmp_path / "skill_catalog.json"))
    assert svc.get_catalog() == []


def test_entry_validates_minimal_fields():
    entry = SkillCatalogEntry.model_validate(_VALID_ENTRY)
    assert entry.id == "skill-creator"
    assert entry.files[0].path == "SKILL.md"
    assert entry.files[0].sha256 == "a" * 64
    assert entry.license == "Apache-2.0"


def test_entry_rejects_missing_required_field():
    bad = {k: v for k, v in _VALID_ENTRY.items() if k != "repo"}
    with pytest.raises(ValidationError):
        SkillCatalogEntry.model_validate(bad)


def test_entry_rejects_bad_sha256():
    bad = {**_VALID_ENTRY, "files": [{"path": "SKILL.md", "sha256": "not-64-hex"}]}
    with pytest.raises(ValidationError):
        SkillCatalogEntry.model_validate(bad)
