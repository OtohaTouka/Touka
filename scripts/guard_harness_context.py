# ruff: noqa: T201
"""Harness Context 防漂移守衛 — 第 40 道（ADR-0139）。

<harness_context> 是「OS 環境事實宣告」：soul.md 管我是誰、本塊管我在哪與世界的
物理法則。它對模型做出**行為承諾**（子代理不可再派、看板未清不可停、派發結果耐久、
長對話會摘要進 <conversation_summary>）——承諾句與對應程式行為必須**一起變**：
改了行為沒改指南、或改了指南沒改行為，此守衛即 FAIL。

規則（純 AST／文字、零 import 副作用、秒級；ASCII 輸出）：
1. HARNESS_CONTEXT 已註冊進 ALL_BLOCK_TAGS 且**不在** VOLATILE_SYSTEM_TAGS
   （穩定層＝跨回合快取；有人把它改成波動層會靜默失去快取＋設計語義）。
2. 行為承諾句對釘（claim ⇄ code marker 雙向）：
   - 「cannot dispatch further sub-agents」 ⇄ subagent_tools 的 _SUBAGENT_STRIPPED_TOOLS
     （剝除 dispatch_subagent 與 dispatch_subagent_group，ADR-0269）
   - 「refuses to end the conversation」    ⇄ mission_stop_gate ＋ claude_handler 接線
   - 「after a harness restart」            ⇄ subagent_results.persist_result 耐久層
   - 「<conversation_summary>」             ⇄ SoulLayer inject_context_summary
3. builder 字串常數不得含 CJK（拍板：英文內文）。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
_SRC = PROJECT_ROOT / "ToukaProxy" / "src"
_PROMPT_BLOCKS = _SRC / "services" / "prompt_blocks.py"

_CJK_RE = re.compile(r"[一-鿿]")

# claim（builder 字串常數內必須出現的承諾句）⇄ code marker（目標檔內必須出現的行為錨）
_CLAIM_PAIRS: tuple[tuple[str, Path, str], ...] = (
    (
        "cannot dispatch further sub-agents",
        _SRC / "tools" / "subagent_tools.py",
        # 剝除集合本身：單一子代理與子代理群組都在裡面（ADR-0269）
        '_SUBAGENT_STRIPPED_TOOLS = frozenset({"dispatch_subagent", "dispatch_subagent_group"})',
    ),
    # hitl-unification H2/H4：state-changing 調用可能暫停等人類審批（統一 broker）
    (
        "pause for human approval",
        _SRC / "services" / "tool_executor.py",
        "_await_human_approval",
    ),
    # ADR-0146：nudge 自主續圈移除——「回文字＝回合結束」成為新的行為承諾
    # （舊「看板未清 harness 拒絕結束對話」承諾隨 board_allows_stop 唯一執法點
    # 退役；工具輪保險絲的收尾指引由執行期 WRAP_UP_NOTE 傳達、不佔本塊 token）。
    (
        "ends your turn; there is no automatic",
        _SRC / "services" / "rhythm_service.py",
        "def consume_round",
    ),
    (
        "after a harness restart",
        _SRC / "services" / "subagent_results.py",
        "def persist_result",
    ),
    (
        "<conversation_summary>",
        _SRC / "middleware" / "layers" / "soul.py",
        "inject_context_summary",
    ),
)


def _module_names_in_tuple(tree: ast.Module, var_name: str) -> set[str]:
    """模組層級 ``var_name`` tuple 內引用的常數名集合。"""
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
            value = node.value
        elif isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == var_name and isinstance(
                value, ast.Tuple | ast.List
            ):
                return {e.id for e in value.elts if isinstance(e, ast.Name)}
    return set()


def _builder_string_text(tree: ast.Module) -> str:
    """``build_harness_context`` 函式內全部字串常數串接（承諾句／CJK 檢查對象）。

    排除 docstring（repo 慣例中文）——只有實際進 prompt 的內文字串受英文約束。
    """
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "build_harness_context":
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]  # 跳過 docstring
            parts = [
                n.value
                for stmt in body
                for n in ast.walk(stmt)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            return "\n".join(parts)
    return ""


def main() -> int:
    """驗證 tag 註冊／穩定層／承諾句對釘／英文內文。ASCII 輸出（cp950 相容）。"""
    tree = ast.parse(_PROMPT_BLOCKS.read_text(encoding="utf-8"))
    errors: list[str] = []

    # 1. tag 註冊＋穩定層
    all_tags = _module_names_in_tuple(tree, "ALL_BLOCK_TAGS")
    volatile = _module_names_in_tuple(tree, "VOLATILE_SYSTEM_TAGS")
    if "HARNESS_CONTEXT" not in all_tags:
        errors.append("HARNESS_CONTEXT is not registered in ALL_BLOCK_TAGS")
    if "HARNESS_CONTEXT" in volatile:
        errors.append(
            "HARNESS_CONTEXT must stay OUT of VOLATILE_SYSTEM_TAGS "
            "(stable tier = cross-turn cache; see ADR-0139)"
        )

    # 2. 承諾句對釘（雙向）
    claims_text = _builder_string_text(tree)
    if not claims_text:
        errors.append("cannot locate build_harness_context() in prompt_blocks.py")
    for claim, code_path, marker in _CLAIM_PAIRS:
        if claims_text and claim not in claims_text:
            errors.append(
                f'claim "{claim}" missing from build_harness_context - '
                "guide text drifted from the pinned behavior promises"
            )
        code_text = code_path.read_text(encoding="utf-8") if code_path.exists() else ""
        if marker not in code_text:
            errors.append(
                f'behavior marker "{marker}" missing from {code_path.name} - '
                f'the code behind the promise "{claim}" changed; '
                "update the guide text AND this guard together"
            )

    # 3. 英文內文（builder 字串常數不得含 CJK）
    if claims_text and _CJK_RE.search(claims_text):
        errors.append(
            "build_harness_context contains CJK characters - "
            "harness_context body must be English (owner decision, ADR-0139)"
        )

    if errors:
        print("[Guard #40] FAIL: Harness Context drift:")
        for e in errors:
            print(f"   - {e}")
        return 1

    print(
        f"[Guard #40] PASS: Harness Context anti-drift - "
        f"{len(_CLAIM_PAIRS)} behavior promises pinned, stable tier, English body."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
