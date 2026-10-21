"""SkillCatalogService — 真技能策展目錄的讀取 (SSOT: skill_catalog.json / .example)。

ADR-0074：探索中心「真技能目錄」的清單來源。每筆描述一個已審的能力型 .skill：
``repo`` + 釘 40-hex ``commit`` + ``base_path`` + 確切 ``files``；安裝時逐檔從
``raw.githubusercontent.com``（已在 allowlist、不可變）抓取，runtime 零 api.github.com。

讀取鏈：``config/skill_catalog.json`` → fallback ``config/skill_catalog.json.example``
（tracked 種子，bootstrap 自癒植入）→ 空清單。實際安裝/驗證在 SkillService 邊界。
"""
from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

from src.config import get_config_dir
from src.services.resource_manager import resource_manager

# 釘版守則：commit 必須為 40-hex（非 branch ref）→ 來源不可變（安裝邊界強制）。
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# 內容完整性：每檔釘 sha256（64-hex）→ 安裝時逐檔比對下載內容（防 GitHub 被黑/MITM/損毀）。
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CatalogFile(BaseModel):
    """策展真技能的單一檔案：相對路徑 ＋ 釘 sha256（安裝時逐檔內容驗證）。"""

    path: str
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _check_sha256(cls, v: str) -> str:
        if not SHA256_RE.fullmatch(v):
            msg = "sha256 must be 64 lowercase hex chars"
            raise ValueError(msg)
        return v


class SkillCatalogEntry(BaseModel):
    """單筆策展真技能的結構 schema（安裝邊界 model_validate 用）。"""

    id: str
    name: str
    category: str
    description: str
    repo: str
    commit: str
    base_path: str
    files: list[CatalogFile]
    author: str = "Community"
    license: str = "Unknown"


class SkillCatalogService:
    """真技能策展目錄讀取 (SSOT: skill_catalog.json / .example)。"""

    def __init__(self, catalog_path: str | None = None) -> None:
        self.catalog_path = catalog_path or str(get_config_dir() / "skill_catalog.json")

    def get_catalog(self) -> list[dict]:
        """讀 skill_catalog.json；缺檔 fallback 讀 .example 種子；最終回空清單。"""
        for path in (self.catalog_path, self.catalog_path + ".example"):
            data = resource_manager.read_json(path)
            if data and data.get("catalog"):
                return data["catalog"]
        return []
