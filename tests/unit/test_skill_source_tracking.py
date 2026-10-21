"""Skill 版本追蹤：catalog 安裝寫入 provenance sidecar 並暴露於 list_skills。

驗證（ADR-0074 版本追蹤擴充）：
- catalog 安裝寫 ``.touka_source.json``（catalog_id / commit）。
- ``list_skills`` 暴露 ``source_commit`` / ``source_catalog_id``。
- 重裝同 id 不同 commit → 升級更新 commit（版本追蹤）。
- 無 sidecar 的技能（自建/URL 安裝）source 欄位為 None（不誤報來源）。
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.services.resource_manager import resource_manager
from src.services.skill_service import skill_service

_SKILL_MD = b"---\nname: catalog-skill\ndescription: Build skills.\n---\n# Body\n"
_SKILL_MD_SHA = hashlib.sha256(_SKILL_MD).hexdigest()
_SOURCE_FILE = ".touka_source.json"

_ENTRY: dict = {
    "id": "src-track",
    "name": "Src Track",
    "category": "Dev",
    "description": "x",
    "repo": "anthropics/skills",
    "commit": "a" * 40,
    "base_path": "skills/src-track",
    "files": [{"path": "SKILL.md", "sha256": _SKILL_MD_SHA}],
    "author": "Anthropic",
    "license": "Apache-2.0",
}


def _mock_client():
    """async httpx client mock：client.get(url) 一律回傳 _SKILL_MD bytes。"""
    def _get(_url):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.content = _SKILL_MD
        return resp

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=_get)
    return client


def _cleanup(skill_id: str) -> None:
    resource_manager.remove_dir(skill_service._skills_dir / skill_id, missing_ok=True)


def _listed(skill_id: str) -> dict:
    return next((s for s in skill_service.list_skills() if s["id"] == skill_id), {})


async def _install(catalog_id: str, commit: str) -> str:
    entry = {**_ENTRY, "id": catalog_id, "commit": commit}
    client = _mock_client()
    with (
        patch("src.services.skill_service.httpx.AsyncClient", return_value=client),
        patch("src.services.skill_service.SkillCatalogService") as mock_cat,
    ):
        mock_cat.return_value.get_catalog.return_value = [entry]
        return await skill_service.install_skill_from_catalog(catalog_id)


@pytest.mark.asyncio
async def test_catalog_install_writes_source_sidecar():
    sid = "src-track-happy"
    try:
        await _install(sid, "a" * 40)
        sidecar = skill_service._skills_dir / sid / _SOURCE_FILE
        assert resource_manager.exists(sidecar)
        data = json.loads(resource_manager.read_file_text(sidecar))
        assert data["catalog_id"] == sid
        assert data["commit"] == "a" * 40
        listed = _listed(sid)
        assert listed.get("source_commit") == "a" * 40
        assert listed.get("source_catalog_id") == sid
    finally:
        _cleanup(sid)


@pytest.mark.asyncio
async def test_reinstall_updates_source_commit():
    sid = "src-track-upgrade"
    try:
        await _install(sid, "a" * 40)
        assert _listed(sid).get("source_commit") == "a" * 40
        await _install(sid, "b" * 40)  # 升級＝重裝同 id 新 commit
        assert _listed(sid).get("source_commit") == "b" * 40
    finally:
        _cleanup(sid)


def test_skill_without_sidecar_has_none_source():
    sid = "src-track-nosrc"
    try:
        skill_service.update_skill(
            sid, {"name": sid, "description": "x", "system_prompt": "y"}
        )
        listed = _listed(sid)
        assert listed.get("source_commit") is None
        assert listed.get("source_catalog_id") is None
    finally:
        _cleanup(sid)
