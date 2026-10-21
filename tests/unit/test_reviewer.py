"""Phase 3 步驟 1-2（互審設計文件 §4.1）：Reviewer 介面與 fail-closed——TDD.

泛化既有 Judge Protocol（Judge/NoopJudge 原地保留供 creation EVAL）：
- CritiqueReport＝結構化 critique（verdict＋required_fixes＋grounded＋confidence）；
- **fail-closed**（與 ModelJudge fail-safe 0.0 方向相反——這是 gate 不是計分）：
  壞 JSON→REJECT、verdict 缺失→REJECT、APPROVE＋blocking fixes→REJECT（矛盾回報）、
  APPROVE＋低 confidence→REJECT、completer 例外→REJECT；
- grounded＝所有 blocking fix 皆有 evidence_anchor；
- EvidenceBundle 硬檢查（cmd 全 exit 0＋artifact 全存在）為 gate 的證據層。
"""

from __future__ import annotations

import json

from src.services.creation.evaluation import Judge, NoopJudge  # 原地保留的迴歸釘
from src.services.review.reviewer import (
    _MIN_APPROVE_CONFIDENCE,
    CmdResult,
    CritiqueReport,
    EvidenceBundle,
    GroundedReviewer,
    NoopReviewer,
    RequiredFix,
    parse_critique,
)


def _critique_json(**overrides) -> str:
    payload = {
        "verdict": "REJECT",
        "reasoning": "artifact 缺少結論段",
        "required_fixes": [
            {"issue": "缺【結論】", "evidence_anchor": "out.md", "severity": "blocking"}
        ],
        "confidence": 0.9,
    }
    payload.update(overrides)
    return f"評語。\n```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


# --------------------------------------------------------------------------- #
# 1. 哨兵與既有 Protocol 迴歸釘
# --------------------------------------------------------------------------- #


async def test_noop_reviewer_approves():
    report = await NoopReviewer().review(
        goal="g", boundary_rules=[], producer_claims="", evidence=EvidenceBundle()
    )
    assert report.verdict == "APPROVE"
    assert report.reviewer_id == "noop"


def test_creation_judge_protocol_untouched():
    """泛化不搬動 creation 的 Judge/NoopJudge（設計文件 §4.1）。"""
    assert Judge is not None
    assert NoopJudge is not None


# --------------------------------------------------------------------------- #
# 2. parse_critique：結構化解析＋fail-closed
# --------------------------------------------------------------------------- #


def test_parse_valid_reject():
    report = parse_critique(
        _critique_json(), reviewer_id="__reviewer_t1__", critic_model="m-x"
    )
    assert isinstance(report, CritiqueReport)
    assert report.verdict == "REJECT"
    assert report.required_fixes[0].issue == "缺【結論】"
    assert report.required_fixes[0].severity == "blocking"
    assert report.grounded is True  # blocking fix 有 anchor
    assert report.reviewer_id == "__reviewer_t1__"
    assert report.critic_model == "m-x"


def test_malformed_json_fail_closed_reject():
    report = parse_critique("我覺得沒問題！", reviewer_id="r", critic_model="m")
    assert report.verdict == "REJECT"
    assert "fail-closed" in report.reasoning


def test_missing_verdict_fail_closed_reject():
    report = parse_critique(
        '```json\n{"reasoning": "看起來不錯"}\n```', reviewer_id="r", critic_model="m"
    )
    assert report.verdict == "REJECT"


def test_approve_with_blocking_fixes_is_contradiction_reject():
    report = parse_critique(
        _critique_json(verdict="APPROVE"), reviewer_id="r", critic_model="m"
    )
    assert report.verdict == "REJECT"


def test_approve_low_confidence_fail_closed_reject():
    low = _MIN_APPROVE_CONFIDENCE - 0.1
    report = parse_critique(
        _critique_json(verdict="APPROVE", required_fixes=[], confidence=low),
        reviewer_id="r",
        critic_model="m",
    )
    assert report.verdict == "REJECT"


def test_approve_high_confidence_passes():
    report = parse_critique(
        _critique_json(verdict="APPROVE", required_fixes=[], confidence=0.9),
        reviewer_id="r",
        critic_model="m",
    )
    assert report.verdict == "APPROVE"
    assert report.confidence == 0.9


def test_grounded_false_when_blocking_fix_lacks_anchor():
    report = parse_critique(
        _critique_json(
            required_fixes=[{"issue": "純感覺不對", "severity": "blocking"}]
        ),
        reviewer_id="r",
        critic_model="m",
    )
    assert report.verdict == "REJECT"
    assert report.grounded is False


def test_advisory_only_fixes_do_not_block_approve():
    report = parse_critique(
        _critique_json(
            verdict="APPROVE",
            required_fixes=[
                {"issue": "建議補註解", "evidence_anchor": None, "severity": "advisory"}
            ],
            confidence=0.9,
        ),
        reviewer_id="r",
        critic_model="m",
    )
    assert report.verdict == "APPROVE"


# --------------------------------------------------------------------------- #
# 3. GroundedReviewer：completer 注入＋例外 fail-closed
# --------------------------------------------------------------------------- #


async def test_grounded_reviewer_end_to_end():
    async def _completer(system: str, user: str) -> str:
        assert "goal" in user or "目標" in user
        return _critique_json(verdict="APPROVE", required_fixes=[], confidence=0.8)

    reviewer = GroundedReviewer(
        _completer, critic_model="m-critic", reviewer_id="__reviewer_t9__"
    )
    report = await reviewer.review(
        goal="寫摘要", boundary_rules=["不碰 data/"], producer_claims="已完成",
        evidence=EvidenceBundle(),
    )
    assert report.verdict == "APPROVE"
    assert report.critic_model == "m-critic"


async def test_grounded_reviewer_completer_exception_fail_closed():
    async def _boom(system: str, user: str) -> str:
        raise RuntimeError("provider down")  # noqa: TRY003 — 測試替身模擬傳輸失敗

    reviewer = GroundedReviewer(_boom, critic_model="m", reviewer_id="r")
    report = await reviewer.review(
        goal="g", boundary_rules=[], producer_claims="", evidence=EvidenceBundle()
    )
    assert report.verdict == "REJECT"
    assert "fail-closed" in report.reasoning


# --------------------------------------------------------------------------- #
# 4. EvidenceBundle 硬檢查
# --------------------------------------------------------------------------- #


def test_evidence_hard_checks():
    good = EvidenceBundle(
        cmd_results=[CmdResult(cmd="pytest -q", exit_code=0, output_snippet="3 passed")],
        artifacts_present={"out.md": True},
    )
    assert good.hard_checks_pass is True
    assert good.has_evidence is True

    bad_cmd = EvidenceBundle(
        cmd_results=[CmdResult(cmd="pytest -q", exit_code=1, output_snippet="1 failed")],
        artifacts_present={"out.md": True},
    )
    assert bad_cmd.hard_checks_pass is False

    rejected_cmd = EvidenceBundle(
        cmd_results=[CmdResult(cmd="true", exit_code=None, rejected_reason="no-op")],
    )
    assert rejected_cmd.hard_checks_pass is False

    missing_artifact = EvidenceBundle(artifacts_present={"ghost.md": False})
    assert missing_artifact.hard_checks_pass is False

    empty = EvidenceBundle()
    assert empty.hard_checks_pass is True  # 空證據＝真空真；「軟任務」由 has_evidence 分流
    assert empty.has_evidence is False


def test_hard_checks_require_passing_assertions_when_test_log_present():
    """E75（ADR-0137 反橡皮圖章第 3 件接入 gate）：test_log 非空須含可數通過斷言。

    修前 has_passing_assertions 只餵顯示字串——producer 貼任意文字充當測試證據，
    cmd+artifact 齊備即過硬檢。變異退回（gate 不查 test_log）→ 第一斷言紅。
    """
    base = {
        "cmd_results": [CmdResult(cmd="pytest -q", exit_code=0, output_snippet="ok")],
        "artifacts_present": {"out.md": True},
    }
    # 偽造測試證據：有內容但零可數斷言 → 硬檢必須擋下
    forged = EvidenceBundle(**base, test_log_excerpt="looks great, all fine!")
    assert forged.hard_checks_pass is False

    # 真測試證據：「N passed」且 N>0 → 通過
    real = EvidenceBundle(**base, test_log_excerpt="3 passed in 0.12s")
    assert real.hard_checks_pass is True

    # 0 passed 不算通過斷言（寧嚴勿鬆，與 has_passing_assertions 同判準）
    zero = EvidenceBundle(**base, test_log_excerpt="0 passed, 2 skipped")
    assert zero.hard_checks_pass is False


def test_hard_checks_without_test_log_keeps_prior_semantics():
    """test_log 缺席（None/空）→ 原語意不變：cmd+artifact 決定、空證據真空真。"""
    no_log = EvidenceBundle(
        cmd_results=[CmdResult(cmd="pytest -q", exit_code=0, output_snippet="3 passed")],
        artifacts_present={"out.md": True},
        test_log_excerpt=None,
    )
    assert no_log.hard_checks_pass is True
    assert EvidenceBundle(test_log_excerpt="").hard_checks_pass is True  # falsy＝缺席


def test_required_fix_defaults():
    fix = RequiredFix(issue="x")
    assert fix.severity == "blocking"
    assert fix.evidence_anchor is None
