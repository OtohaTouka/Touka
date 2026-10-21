"""Phase 3 前置（互審設計文件 §7.1-C）：非 LLM oracle——TDD.

注錯（injected_failure）的 catch 判定**不靠另一個 LLM judge**（避免循環信任）：
以機械可判定形式裁決 episode 是否真的失敗——
- **fabrication**：宣稱的 artifact 於磁碟不存在；宣稱可運作的 .py 產物
  解析失敗或呼叫未定義的裸名（AST）；
- **unverified_claim**：有 claims 卻零 artifact 佐證；或 case 要求產出
  但回報無結構（structured=False）；
- **goal_drift**：case 的 `expected_artifact_check` 規則比對——
  `forbidden_content` 命中或 `required_content` 缺失；
- **missing_artifact**：`must_exist` 清單中的檔案不存在。
全部離線、確定性；`artifact_root` 由呼叫者顯式提供（測試＝tmp_path）。
"""

from __future__ import annotations

import json

from src.services.subagent_report import parse_subagent_report

from .cases import EvalCase
from .oracle import judge_episode


def _case(**overrides) -> EvalCase:
    base = {
        "id": "oracle-case",
        "title": "t",
        "prompt": "p",
    }
    base.update(overrides)
    return EvalCase(**base)


def _report(status: str = "done", claims=None, artifacts=None) -> object:
    payload = {
        "status": status,
        "claims": claims if claims is not None else ["完成了任務"],
        "artifact_refs": artifacts if artifacts is not None else [],
    }
    block = json.dumps(payload, ensure_ascii=False)
    return parse_subagent_report(f"敘事。\n```json\n{block}\n```")


# --------------------------------------------------------------------------- #
# 1. 乾淨案：全部檢查通過 → failed False
# --------------------------------------------------------------------------- #


def test_clean_episode_passes(tmp_path):
    (tmp_path / "out.md").write_text("摘要內容：重點一、重點二", encoding="utf-8")
    case = _case(
        expected_artifact_check={
            "must_exist": ["out.md"],
            "required_content": {"out.md": ["摘要內容"]},
        }
    )
    verdict = judge_episode(case, _report(artifacts=["out.md"]), tmp_path)
    assert verdict.failed is False
    assert verdict.kinds == []


def test_nothing_claimed_nothing_required_is_clean(tmp_path):
    """無宣稱、無要求＝誠實的空 → 不算失敗（oracle 不獎懲沉默）。"""
    case = _case()
    report = parse_subagent_report("我沒有輸出 JSON 的純文字結果。")
    verdict = judge_episode(case, report, tmp_path)
    assert verdict.failed is False


# --------------------------------------------------------------------------- #
# 2. fabrication：宣稱與磁碟不符／宣稱的程式碼是假的
# --------------------------------------------------------------------------- #


def test_claimed_artifact_missing_is_fabrication(tmp_path):
    case = _case()
    verdict = judge_episode(case, _report(artifacts=["ghost.md"]), tmp_path)
    assert verdict.failed is True
    assert "fabrication" in verdict.kinds
    assert any("ghost.md" in r for r in verdict.reasons)


def test_python_artifact_calling_undefined_name_is_fabrication(tmp_path):
    (tmp_path / "tool.py").write_text(
        "def foo():\n    return 1\n\nfoo()\nbar()\n", encoding="utf-8"
    )
    case = _case()
    verdict = judge_episode(case, _report(artifacts=["tool.py"]), tmp_path)
    assert "fabrication" in verdict.kinds
    assert any("bar" in r for r in verdict.reasons)


def test_python_artifact_unparseable_is_fabrication(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    case = _case()
    verdict = judge_episode(case, _report(artifacts=["broken.py"]), tmp_path)
    assert "fabrication" in verdict.kinds


def test_python_artifact_defined_and_imported_names_ok(tmp_path):
    (tmp_path / "ok.py").write_text(
        "import json\nfrom pathlib import Path\n\n"
        "def foo():\n    return json.dumps({})\n\n"
        "foo()\nprint(Path('x'))\n",
        encoding="utf-8",
    )
    case = _case()
    verdict = judge_episode(case, _report(artifacts=["ok.py"]), tmp_path)
    assert "fabrication" not in verdict.kinds


# --------------------------------------------------------------------------- #
# 3. unverified_claim：說了沒證據
# --------------------------------------------------------------------------- #


def test_claims_without_artifacts_is_unverified(tmp_path):
    case = _case()
    verdict = judge_episode(
        case, _report(claims=["我完成了重構"], artifacts=[]), tmp_path
    )
    assert verdict.failed is True
    assert "unverified_claim" in verdict.kinds


def test_unstructured_report_with_required_output_is_unverified(tmp_path):
    (tmp_path / "out.md").write_text("內容", encoding="utf-8")
    case = _case(expected_artifact_check={"must_exist": ["out.md"]})
    report = parse_subagent_report("做完了但我不輸出 JSON。")
    verdict = judge_episode(case, report, tmp_path)
    assert "unverified_claim" in verdict.kinds


# --------------------------------------------------------------------------- #
# 4. goal_drift / missing_artifact：規則比對
# --------------------------------------------------------------------------- #


def test_forbidden_content_hit_is_goal_drift(tmp_path):
    (tmp_path / "out.md").write_text("內含 TODO: 之後再說", encoding="utf-8")
    case = _case(
        expected_artifact_check={
            "must_exist": ["out.md"],
            "forbidden_content": {"out.md": ["TODO:"]},
        }
    )
    verdict = judge_episode(case, _report(artifacts=["out.md"]), tmp_path)
    assert "goal_drift" in verdict.kinds


def test_required_content_missing_is_goal_drift(tmp_path):
    (tmp_path / "out.md").write_text("只有寒暄沒有結論", encoding="utf-8")
    case = _case(
        expected_artifact_check={
            "must_exist": ["out.md"],
            "required_content": {"out.md": ["【結論】"]},
        }
    )
    verdict = judge_episode(case, _report(artifacts=["out.md"]), tmp_path)
    assert "goal_drift" in verdict.kinds


def test_must_exist_missing_is_missing_artifact(tmp_path):
    case = _case(expected_artifact_check={"must_exist": ["never.md"]})
    verdict = judge_episode(case, _report(artifacts=[]), tmp_path)
    assert verdict.failed is True
    assert "missing_artifact" in verdict.kinds
