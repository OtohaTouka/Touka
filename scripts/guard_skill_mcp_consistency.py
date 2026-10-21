# ruff: noqa: T201
"""Skill ↔ MCP 命名一致性守衛 — 第 37 道（ADR-0102）。

防止「first-party 技能 `.mcp.json` 宣告的 MCP server key」與「MCP Hub curated 條目安裝後
產生的 server key」不一致——這正是 docgen 計分卡略過的設計面根因（技能要 `mcp-docgen`、
Hub 安裝 key 若不符就連不上）。

規則：對明列出貨 `.mcp.json` 宣告的 server，若其 recipe(command+args) 與某
curated `mcp_hub.json.example` 條目相同，則該 server 的「宣告 key」必須等於該 hub 條目經
install 衍生的 key（`name.lower().replace(" ","-")`，鏡像 `MCPHubService.derive_server_key`）。
recipe 不匹配任何 curated 條目者不在此守衛範圍。

純函式 + json，零 import 副作用、秒級；掃描函式為純函式、可單測。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
_SKILLS_DIR = PROJECT_ROOT / "ToukaProxy" / "config" / "skills"
_HUB_FILES = [
    PROJECT_ROOT / "ToukaProxy" / "config" / "mcp_hub.json.example",
]
# 相對 _SKILLS_DIR 的出貨來源清單；不自動發現私有或未登記的宣告。
SHIPPED_MCP_DECLARATIONS = (Path("document-generator") / ".mcp.json",)


def derive_server_key(name: str) -> str:
    """安裝端 server key 衍生（必須鏡像 ``src...mcp_hub_service.derive_server_key``）。"""
    return name.lower().replace(" ", "-")


@dataclass
class Violation:
    """一條技能↔MCP 命名不符。"""

    skill_id: str
    declared_key: str
    expected_key: str
    hub_source: str
    reason: str = ""

    def __str__(self) -> str:
        if self.reason:
            return f"  必要出貨來源 `{self.hub_source}`：{self.reason}"
        return (
            f"  技能 `{self.skill_id}` 的 .mcp.json 宣告 server `{self.declared_key}`，"
            f"但對應 Hub 條目（{self.hub_source}）安裝後為 `{self.expected_key}`——名稱不符、"
            f"技能連不上。請將 .mcp.json 的 key 改為 `{self.expected_key}`。"
        )


def _recipe(entry: dict) -> tuple:
    """辨識 server 用的 recipe：(command, tuple(args))。無 command（SSE/url 類）→ 不可 hub 安裝。"""
    cmd = entry.get("command")
    args = entry.get("args") or []
    return (cmd, tuple(args)) if cmd else (None, ())


def load_curated(hub_path: Path) -> list[dict]:
    """讀取必要範本；缺失、讀取或格式錯誤交由入口報告 FAIL。"""
    data = json.loads(hub_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("hub"), list):
        raise TypeError
    if any(not isinstance(entry, dict) for entry in data["hub"]):
        raise TypeError
    return [e for e in data.get("hub", []) if e.get("source") == "curated"]


def load_skill_decls(skills_dir: Path) -> list[dict]:
    """只讀明列必要出貨宣告；未登記檔案不探查、不 glob。"""
    out: list[dict] = []
    for relative in SHIPPED_MCP_DECLARATIONS:
        mcp_file = skills_dir / relative
        data = json.loads(mcp_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError
        for key, cfg in data.items():
            if not isinstance(cfg, dict):
                raise TypeError
            out.append({"skill_id": mcp_file.parent.name, "key": key, "recipe": _recipe(cfg or {})})
    return out


def find_violations(curated: list[dict], decls: list[dict]) -> list[Violation]:
    """對 recipe 相符的 curated 條目，其衍生 key 必須等於技能宣告 key（純函式）。"""
    by_recipe: dict[tuple, tuple[str, str]] = {}
    for e in curated:
        name = e.get("name")
        if name:
            by_recipe[_recipe(e)] = (derive_server_key(name), e.get("id") or name)
    out: list[Violation] = []
    for d in decls:
        if d["recipe"] == (None, ()):
            continue  # 非 hub-installable（SSE/url 等）→ 不在範圍
        match = by_recipe.get(d["recipe"])
        if match and match[0] != d["key"]:
            out.append(Violation(d["skill_id"], d["key"], match[0], match[1]))
    return out


def run_all() -> list[Violation]:
    """核對必要出貨範本；不宣稱驗證現場私有設定或自動發現新宣告。"""
    curated: list[dict] = []
    errors: list[Violation] = []
    for hp in _HUB_FILES:
        try:
            curated.extend(load_curated(hp))
        except (OSError, ValueError, TypeError):
            errors.append(Violation("", "", "", hp.name, "缺失、無法讀取或格式錯誤"))
    try:
        declarations = load_skill_decls(_SKILLS_DIR)
    except (OSError, ValueError, TypeError):
        errors.append(Violation("", "", "", "SHIPPED_MCP_DECLARATIONS", "缺失、無法讀取或格式錯誤"))
        declarations = []
    return errors + find_violations(curated, declarations)


def main() -> int:
    """執行守衛並回傳退出碼。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 60)
    print("Skill <-> MCP Consistency Guard (Guard #37)")
    print("=" * 60)
    violations = run_all()
    if not violations:
        print("[OK] 技能 .mcp.json 宣告的 hub-installable MCP key 皆與 Hub 安裝 key 一致。")
        return 0
    print(f"[FAIL] 發現 {len(violations)} 條技能↔MCP 命名不符：")
    for v in violations:
        print(str(v))
    return 1


if __name__ == "__main__":
    sys.exit(main())
