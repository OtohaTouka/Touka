# ruff: noqa: T201
"""Context Snapshot 防漂移守衛 — 第 39 道（ADR-0133）。

Context Snapshot（Debug Center「Context」分頁 / `context-snapshot` 端點）以真實
ProxyPipeline 組裝 + `prompt_blocks.ALL_BLOCK_TAGS`（tag SSOT）有序解析，去硬編碼。
唯一的 per-block 知識是 `context_snapshot_service.BLOCK_META`（tag → 顯示 metadata）。

規則：**每個 `ALL_BLOCK_TAGS` 都必須在 `BLOCK_META` 有一筆 metadata**。日後有人新增
system 區塊 tag（加進 ALL_BLOCK_TAGS）卻忘了補 BLOCK_META，此守衛即 FAIL——逼補，
杜絕快照悄悄變 stale（正是本次補強要根治的問題）。

純 AST、零 import 副作用、秒級（鏡像其他守衛風格；不 import src 以免拉重依賴）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
_PROMPT_BLOCKS = PROJECT_ROOT / "ToukaProxy" / "src" / "services" / "prompt_blocks.py"
_SNAPSHOT_SVC = PROJECT_ROOT / "ToukaProxy" / "src" / "services" / "context_snapshot_service.py"


def _value_of(path: Path, var_name: str) -> ast.expr | None:
    """回傳模組層級 ``var_name`` 的賦值 AST（支援 Assign 與 AnnAssign）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == var_name:
                return node.value
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == var_name:
                    return node.value
    return None


def tag_names(path: Path) -> set[str]:
    """``ALL_BLOCK_TAGS`` 元組內的常數名（PERSONA、PROJECT_CONTEXT…）。"""
    val = _value_of(path, "ALL_BLOCK_TAGS")
    if isinstance(val, ast.Tuple | ast.List):
        return {e.id for e in val.elts if isinstance(e, ast.Name)}
    return set()


def meta_key_names(path: Path) -> set[str]:
    """``BLOCK_META`` 字典鍵所用的常數名。"""
    val = _value_of(path, "BLOCK_META")
    if isinstance(val, ast.Dict):
        return {k.id for k in val.keys if isinstance(k, ast.Name)}
    return set()


def main() -> int:
    """驗證 ALL_BLOCK_TAGS ⊆ BLOCK_META keys；缺者 FAIL。ASCII 輸出（Windows cp950 相容）。"""
    tags = tag_names(_PROMPT_BLOCKS)
    meta = meta_key_names(_SNAPSHOT_SVC)
    if not tags:
        print("[Guard #39] FAIL: cannot parse ALL_BLOCK_TAGS from prompt_blocks.py")
        return 1
    if not meta:
        print("[Guard #39] FAIL: cannot parse BLOCK_META from context_snapshot_service.py")
        return 1

    missing = tags - meta
    if missing:
        print("[Guard #39] FAIL: Context Snapshot drift - block tags without BLOCK_META:")
        for name in sorted(missing):
            print(f"   - {name}")
        print("   -> add display metadata for the tag in context_snapshot_service.BLOCK_META")
        return 1

    print(f"[Guard #39] PASS: Context Snapshot anti-drift - {len(tags)} block tags all covered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
