"""ADR-0074：install_skill_from_catalog 安裝器測試（mock httpx 逐檔抓取）。

驗證策展真技能安裝：happy（含 scripts 落地）／不存在 id／path traversal／單檔超量／
非 40-hex commit（branch ref）皆被擋，且失敗不留半成品。各測試用獨立 id 並自清沙箱。
"""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.services.resource_manager import resource_manager
from src.services.skill_service import skill_service

_SKILL_MD = b"---\nname: catalog-skill\ndescription: Build skills.\n---\n# Body\n"
_SKILL_MD_SHA = hashlib.sha256(_SKILL_MD).hexdigest()

_ENTRY: dict = {
    "id": "skill-creator",
    "name": "Skill Creator",
    "category": "Dev",
    "description": "Build new skills.",
    "repo": "anthropics/skills",
    "commit": "a" * 40,
    "base_path": "skills/skill-creator",
    "files": [{"path": "SKILL.md", "sha256": _SKILL_MD_SHA}],
    "author": "Anthropic",
    "license": "Apache-2.0",
}


def _mock_client(content_for):
    """建一個 async httpx client mock，client.get(url) 依 url 回傳對應 bytes。"""
    def _get(url):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.content = content_for(url)
        return resp

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=_get)
    return client


def _cleanup(skill_id: str) -> None:
    resource_manager.remove_dir(skill_service._skills_dir / skill_id, missing_ok=True)


@pytest.mark.asyncio
async def test_install_from_catalog_happy_path_lands_skill_and_scripts():
    entry = {
        **_ENTRY,
        "id": "catalog-happy",
        "files": [
            {"path": "SKILL.md", "sha256": _SKILL_MD_SHA},
            {"path": "scripts/run.py", "sha256": hashlib.sha256(b"print('hi')").hexdigest()},
        ],
    }
    client = _mock_client(lambda url: _SKILL_MD if url.endswith("SKILL.md") else b"print('hi')")
    try:
        with (
            patch("src.services.skill_service.httpx.AsyncClient", return_value=client),
            patch("src.services.skill_service.SkillCatalogService") as mock_cat,
        ):
            mock_cat.return_value.get_catalog.return_value = [entry]
            skill_id = await skill_service.install_skill_from_catalog("catalog-happy")

        assert skill_id == "catalog-happy"
        assert skill_service.get_skill("catalog-happy") is not None
        assert (skill_service._skills_dir / "catalog-happy" / "scripts" / "run.py").exists()
    finally:
        _cleanup("catalog-happy")


@pytest.mark.asyncio
async def test_install_from_catalog_unknown_id_raises_not_found():
    with patch("src.services.skill_service.SkillCatalogService") as mock_cat:
        mock_cat.return_value.get_catalog.return_value = []
        with pytest.raises(ToukaException) as exc:
            await skill_service.install_skill_from_catalog("nope")
    assert exc.value.code == ErrorCode.RESOURCE_NOT_FOUND


@pytest.mark.asyncio
async def test_install_from_catalog_rejects_path_traversal():
    entry = {
        **_ENTRY,
        "id": "catalog-trav",
        "files": [{"path": "../evil.py", "sha256": hashlib.sha256(b"malicious").hexdigest()}],
    }
    client = _mock_client(lambda _url: b"malicious")
    try:
        with (
            patch("src.services.skill_service.httpx.AsyncClient", return_value=client),
            patch("src.services.skill_service.SkillCatalogService") as mock_cat,
        ):
            mock_cat.return_value.get_catalog.return_value = [entry]
            with pytest.raises(ToukaException) as exc:
                await skill_service.install_skill_from_catalog("catalog-trav")
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert skill_service.get_skill("catalog-trav") is None
    finally:
        _cleanup("catalog-trav")


@pytest.mark.asyncio
async def test_install_from_catalog_rejects_oversize_file():
    entry = {**_ENTRY, "id": "catalog-big"}
    client = _mock_client(lambda _url: b"x" * 11)
    with (
        patch("src.services.skill_service.MAX_SKILL_DOWNLOAD_BYTES", 10),
        patch("src.services.skill_service.httpx.AsyncClient", return_value=client),
        patch("src.services.skill_service.SkillCatalogService") as mock_cat,
    ):
        mock_cat.return_value.get_catalog.return_value = [entry]
        with pytest.raises(ToukaException) as exc:
            await skill_service.install_skill_from_catalog("catalog-big")
    assert exc.value.code == ErrorCode.REQUEST_TOO_LARGE


@pytest.mark.asyncio
async def test_install_from_catalog_rejects_non_pinned_commit():
    entry = {**_ENTRY, "id": "catalog-branch", "commit": "main"}  # 非 40-hex branch ref
    with (
        patch("src.services.skill_service.httpx.AsyncClient") as mock_cls,
        patch("src.services.skill_service.SkillCatalogService") as mock_cat,
    ):
        mock_cat.return_value.get_catalog.return_value = [entry]
        with pytest.raises(ToukaException) as exc:
            await skill_service.install_skill_from_catalog("catalog-branch")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    mock_cls.assert_not_called()  # 釘版檢查在抓取之前
