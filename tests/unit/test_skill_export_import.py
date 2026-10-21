"""SkillService package/install 測試 (ADR-0066 Phase 4).

驗證 package_skill（打包）與 install_skill_from_url（URL 安裝）：
domain allowlist、大小上限、寬鬆驗證、.bak 原子覆寫、回滾。
mock httpx 模擬遠端下載。
"""

from __future__ import annotations

import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.core.exceptions import ToukaException
from src.services.resource_manager import resource_manager
from src.services.skill_service import skill_service


def _write_skill(skills_dir, sid, fm_lines, body):
    skill_dir = skills_dir / sid
    resource_manager.make_dir(skill_dir)
    content = "---\n" + "\n".join(fm_lines) + "\n---\n" + body
    resource_manager.write_file_text(skill_dir / "SKILL.md", content)


def _make_skill_bytes(tmp_path, skill_id="imported", body="body"):
    """建合法 skill 目錄並打包成 .skill bytes。"""
    src = tmp_path / "src" / skill_id
    resource_manager.make_dir(src)
    md = (
        f"---\nname: {skill_id}\ndescription: d\n"
        f"metadata:\n  display-name: 匯入技能\n---\n{body}"
    )
    resource_manager.write_file_text(src / "SKILL.md", md)
    archive = tmp_path / f"{skill_id}.skill"
    resource_manager.create_archive(src, archive, arc_base=src.parent)
    return resource_manager.read_file_binary(archive)


def _mock_httpx(content_bytes):
    """建 mock AsyncClient，client.get 回 content。"""
    mock_resp = MagicMock()
    mock_resp.content = content_bytes
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


@pytest.fixture
def svc(tmp_path):
    skills_dir = tmp_path / "skills"
    resource_manager.make_dir(skills_dir)
    skill_service._skills_dir = skills_dir
    skill_service.config_path = str(skills_dir)
    skill_service._metadata = {}
    skill_service.load_config()
    return skill_service


# --- package_skill ---

def test_package_skill_round_trip(svc, tmp_path):
    """打包後 zip 含 <id>/SKILL.md。"""
    _write_skill(tmp_path / "skills", "demo", ["name: demo", "description: d"], "body")
    svc.load_config()
    path = svc.package_skill("demo")
    with zipfile.ZipFile(path) as zf:
        assert "demo/SKILL.md" in zf.namelist()


def test_package_skill_not_found(svc):
    """不存在的 skill → ToukaException。"""
    with pytest.raises(ToukaException):
        svc.package_skill("ghost")


# --- install_skill_from_url ---

@pytest.mark.asyncio
async def test_install_bad_domain_blocked(svc):
    """非 allowlist domain → ToukaException（不發網路）。"""
    with pytest.raises(ToukaException):
        await svc.install_skill_from_url("http://evil.com/x.skill")


@pytest.mark.asyncio
async def test_install_happy_path(svc, tmp_path):
    """合法 URL + skill → 安裝到 skills_dir、get_skill 可讀。"""
    content = _make_skill_bytes(tmp_path, "imported")
    with patch("src.services.skill_service.httpx.AsyncClient",
               return_value=_mock_httpx(content)):
        skill_id = await svc.install_skill_from_url(
            "https://github.com/x/imported.skill"
        )
    assert skill_id == "imported"
    assert svc.get_skill("imported") is not None
    assert resource_manager.exists(tmp_path / "skills" / "imported" / "SKILL.md")


@pytest.mark.asyncio
async def test_install_oversize_blocked(svc, tmp_path):
    """下載超過大小上限 → ToukaException。"""
    big = b"x" * (21 * 1024 * 1024)  # > 20 MiB
    with patch("src.services.skill_service.httpx.AsyncClient",
               return_value=_mock_httpx(big)), pytest.raises(ToukaException):
        await svc.install_skill_from_url("https://github.com/x/big.skill")


@pytest.mark.asyncio
async def test_install_invalid_no_skill_md_rolls_back(svc, tmp_path):
    """zip 無 SKILL.md → ToukaException 且回滾（正式目錄無殘留）。"""
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("notskill/readme.txt", "x")
    with patch("src.services.skill_service.httpx.AsyncClient",
               return_value=_mock_httpx(resource_manager.read_file_binary(bad))), \
         pytest.raises(ToukaException):
        await svc.install_skill_from_url("https://github.com/x/bad.skill")
    assert not resource_manager.exists(tmp_path / "skills" / "notskill")


@pytest.mark.asyncio
async def test_install_overwrites_existing(svc, tmp_path):
    """安裝同 id → 覆寫更新，.bak 清除。"""
    _write_skill(
        tmp_path / "skills", "imported",
        ["name: imported", "description: OLD"], "old body",
    )
    svc.load_config()
    content = _make_skill_bytes(tmp_path, "imported", body="new body")
    with patch("src.services.skill_service.httpx.AsyncClient",
               return_value=_mock_httpx(content)):
        skill_id = await svc.install_skill_from_url(
            "https://github.com/x/imported.skill"
        )
    assert skill_id == "imported"
    assert svc.get_skill("imported")["system_prompt"] == "new body"
    assert not resource_manager.exists(tmp_path / "skills" / "imported.bak")
