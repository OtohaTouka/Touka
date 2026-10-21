"""SKILL.md 解析器 (ADR-0066 Phase 1).

純函式模組，負責 Anthropic 標準 SKILL.md 的 frontmatter/body 拆分、
YAML 解析、序列化，以及映射為 SkillService 內部契約。

設計原則 (SRP)：本模組零 IO、零環境變數存取，僅處理文字 ↔ 結構轉換，
因此完全可單元測試，且更換 YAML 後端只需改這裡 (OCP)。
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_FENCE = "---"


def split_frontmatter(raw: str) -> tuple[str, str]:
    """拆分 SKILL.md 為 (frontmatter_text, body)。

    只認檔案開頭、獨立成行的 ``---`` 圍欄作為 frontmatter 邊界；
    body 內部的 ``---``（表格分隔線、水平線）不會誤切。
    無合法 frontmatter 時回 ("", raw)，呼叫端據此視為純 body。
    """
    if not raw.startswith(_FENCE):
        return "", raw
    lines = raw.split("\n")
    if lines[0].strip() != _FENCE:
        return "", raw
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            fm_text = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:])
            return fm_text, body
    # 沒有結束圍欄 → 視為無 frontmatter，避免把整份內容當成 YAML
    return "", raw


def parse_frontmatter(text: str) -> dict[str, Any]:
    """以 ``yaml.safe_load`` 解析 frontmatter 文字。

    強制使用 ``safe_load`` 杜絕反序列化 RCE。非法 YAML 或非 mapping
    一律回 {}，讓單一壞檔不會中斷整個技能目錄掃描。
    """
    if not text.strip():
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        logger.warning("Skill frontmatter 解析失敗，回傳空 metadata。")
        return {}
    return data if isinstance(data, dict) else {}


def parse_skill_md(raw: str) -> tuple[dict[str, Any], str]:
    """便利函式：一次回 (frontmatter_dict, body)。"""
    fm_text, body = split_frontmatter(raw)
    return parse_frontmatter(fm_text), body


def serialize_skill_md(frontmatter: dict[str, Any], body: str) -> str:
    """將 frontmatter 與 body 組裝回標準 SKILL.md 文字 (寫檔用)。

    ``allow_unicode=True`` 確保中文不被轉義；``sort_keys=False`` 保留欄位順序。
    """
    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    return f"{_FENCE}\n{yaml_text}{_FENCE}\n{body}"


def read_touka_field(fm: dict[str, Any], key: str, default: Any = None) -> Any:
    """讀 Touka 擴充欄位：優先 metadata 子欄位，fallback top-level (向後相容舊檔)。

    Phase 4 後擴充欄位 (display-name/category/author/is_system/version) 收進
    ``metadata`` 子欄位以完全相容 Anthropic quick_validate；舊格式 (top-level)
    仍可正確讀取。
    """
    meta = fm.get("metadata")
    if isinstance(meta, dict) and key in meta:
        return meta[key]
    return fm.get(key, default)


def build_frontmatter(
    *,
    name: str,
    description: str,
    display_name: str,
    category: str,
    author: str,
    is_system: bool,
    allowed_tools: list[str] | None = None,
    version: float | str | None = None,
) -> dict[str, Any]:
    """組裝契約資料為標準 frontmatter (擴充欄位入 metadata 子欄位)。

    ``name``/``description``/``allowed-tools`` 為 Anthropic 標準欄位，保持 top-level；
    Touka 擴充欄位收進 ``metadata`` 子欄位，使 SKILL.md 通過 Anthropic quick_validate
    (其 ALLOWED_PROPERTIES 含 metadata 且不檢查其內部)。
    """
    fm: dict[str, Any] = {"name": name, "description": description}
    if allowed_tools:
        fm["allowed-tools"] = allowed_tools
    meta: dict[str, Any] = {
        "display-name": display_name,
        "category": category,
        "author": author,
        "is_system": is_system,
    }
    if version is not None:
        meta["version"] = version
    fm["metadata"] = meta
    return fm


def map_to_contract(
    fm: dict[str, Any], body: str, mcp_servers: list[str]
) -> dict[str, Any]:
    """將 frontmatter + body + 推導的 mcp_servers 映射為 SkillService 契約 9 key。

    標準欄位 ``name`` 是 kebab 識別碼 (= 目錄名)，作為契約 ``id``；
    Touka 擴充欄位透過 ``read_touka_field`` 讀取 (metadata 優先、top-level fallback)。
    標準欄位 ``allowed-tools`` 映射為契約 ``required_tools``。
    """
    kebab_id = fm.get("name", "")
    return {
        "id": kebab_id,
        "name": read_touka_field(fm, "display-name") or kebab_id,
        "description": fm.get("description", ""),
        "system_prompt": body,
        "required_tools": fm.get("allowed-tools", []) or [],
        "mcp_servers": mcp_servers,
        "is_system": bool(read_touka_field(fm, "is_system", False)),
        "author": read_touka_field(fm, "author", ""),
        "category": read_touka_field(fm, "category", "General"),
    }
