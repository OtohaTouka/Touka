"""ADR-0074 SHA-256 完整性：install_skill_from_catalog 逐檔內容雜湊驗證。

下載內容的 sha256 必須與 catalog entry 釘的 sha256 相符（防 GitHub 被黑/MITM/損毀）；
不符即拒絕、不留半成品。釘 commit 保證來源不可變，逐檔雜湊為其上的防禦縱深。
"""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.services.resource_manager import resource_manager
from src.services.skill_service import skill_service

_SKILL_MD = b"---\nname: catalog-skill\ndescription: x.\n---\n# Body\n"
_GOOD_SHA = hashlib.sha256(_SKILL_MD).hexdigest()

_ENTRY: dict = {
    "id": "integ",
    "name": "Integ",
    "category": "Dev",
    "description": "x",
    "repo": "anthropics/skills",
    "commit": "a" * 40,
    "base_path": "skills/integ",
    "files": [{"path": "SKILL.md", "sha256": _GOOD_SHA}],
    "author": "Anthropic",
    "license": "Apache-2.0",
}


def _mock_client(content: bytes):
    def _get(_url):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.content = content
        return resp

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=_get)
    return client


def _cleanup(skill_id: str) -> None:
    resource_manager.remove_dir(skill_service._skills_dir / skill_id, missing_ok=True)


@pytest.mark.asyncio
async def test_install_verifies_matching_sha256():
    """內容 sha256 與釘的相符 → 安裝成功落地。"""
    entry = {**_ENTRY, "id": "integ-ok"}
    try:
        with (
            patch(
                "src.services.skill_service.httpx.AsyncClient",
                return_value=_mock_client(_SKILL_MD),
            ),
            patch("src.services.skill_service.SkillCatalogService") as mock_cat,
        ):
            mock_cat.return_value.get_catalog.return_value = [entry]
            sid = await skill_service.install_skill_from_catalog("integ-ok")
        assert sid == "integ-ok"
        assert skill_service.get_skill("integ-ok") is not None
    finally:
        _cleanup("integ-ok")


@pytest.mark.asyncio
async def test_install_rejects_sha256_mismatch():
    """內容遭竄改（sha256 不符釘值）→ VALIDATION_ERROR，且不留半成品。"""
    entry = {**_ENTRY, "id": "integ-bad"}  # entry 釘 _GOOD_SHA
    # 竄改：仍是合法 SKILL.md（能過 _validate_skill_dir），但 bytes 不同 → sha 不符。
    # 確保唯一能攔下的防線是「內容雜湊驗證」，而非 frontmatter 結構檢查。
    tampered = b"---\nname: catalog-skill\ndescription: TAMPERED.\n---\n# Evil body\n"
    try:
        with (
            patch(
                "src.services.skill_service.httpx.AsyncClient",
                return_value=_mock_client(tampered),
            ),
            patch("src.services.skill_service.SkillCatalogService") as mock_cat,
        ):
            mock_cat.return_value.get_catalog.return_value = [entry]
            with pytest.raises(ToukaException) as exc:
                await skill_service.install_skill_from_catalog("integ-bad")
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert skill_service.get_skill("integ-bad") is None
    finally:
        _cleanup("integ-bad")
