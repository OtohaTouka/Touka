"""Phase 2（提案 §9）：結構化子代理回報 schema——TDD.

需求 4 的程式化裁決依據：子代理最終輸出以尾部 ```json 回報塊攜帶
status／claims／artifact_refs／confidence；解析容錯——弱模型不遵守格式時
**誠實 fallback**（status=unverified、structured=False、summary=全文），
絕不假裝有結構。
"""

from __future__ import annotations

from src.services.subagent_report import REPORT_INSTRUCTION, parse_subagent_report

_VALID_BLOCK = (
    "我完成了任務，寫出了摘要檔。\n\n"
    "```json\n"
    '{"status": "done", "claims": ["寫出 out.md 摘要"],'
    ' "artifact_refs": ["out.md"], "confidence": 0.8}\n'
    "```\n"
)


def test_parse_valid_report_block():
    report = parse_subagent_report(_VALID_BLOCK)
    assert report.structured is True
    assert report.status == "done"
    assert report.claims == ["寫出 out.md 摘要"]
    assert report.artifact_refs == ["out.md"]
    assert report.confidence == 0.8
    assert "我完成了任務" in report.summary
    assert '"status"' not in report.summary  # 回報塊本體不留在摘要裡


def test_parse_last_block_wins():
    text = (
        '前面有一個舊塊：\n```json\n{"status": "partial", "claims": ["舊"]}\n```\n'
        '後面才是最終回報：\n```json\n{"status": "done", "claims": ["新"]}\n```\n'
    )
    report = parse_subagent_report(text)
    assert report.status == "done"
    assert report.claims == ["新"]


def test_missing_block_falls_back_raw():
    text = "我做完了，但我不會輸出 JSON。"
    report = parse_subagent_report(text)
    assert report.structured is False
    assert report.status == "unverified"
    assert report.summary == text
    assert report.claims == []
    assert report.confidence is None


def test_malformed_json_falls_back_raw():
    text = '結果如下：\n```json\n{"status": "done", broken\n```\n'
    report = parse_subagent_report(text)
    assert report.structured is False
    assert report.status == "unverified"
    assert report.summary == text.strip()


def test_confidence_clamped_and_tolerant():
    def _with_conf(v: str):
        return parse_subagent_report(f'```json\n{{"status":"done","confidence":{v}}}\n```')

    assert _with_conf("1.5").confidence == 1.0
    assert _with_conf("-0.2").confidence == 0.0
    assert _with_conf('"high"').confidence is None


def test_claims_and_artifacts_coerced():
    report = parse_subagent_report(
        '```json\n{"status":"done","claims":"不是列表",'
        '"artifact_refs":["ok.md", 42, null]}\n```'
    )
    assert report.claims == []
    assert report.artifact_refs == ["ok.md"]


def test_status_normalized_and_unknown_maps_unverified():
    upper = parse_subagent_report('```json\n{"status":"DONE"}\n```')
    assert upper.status == "done"
    weird = parse_subagent_report('```json\n{"status":"awesome"}\n```')
    assert weird.status == "unverified"


def test_report_instruction_mentions_json_block():
    assert "```json" in REPORT_INSTRUCTION
    assert "claims" in REPORT_INSTRUCTION
