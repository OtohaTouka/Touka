"""characterization harness 的 CI 缺檔閘（model-p1a U4）。

CI（GitHub Actions 預設 ``CI=true``）缺 golden＝失敗並指出缺哪一份；本機（``CI``
未設或為假）維持錄製＋skip。``_GOLDEN_DIR`` 一律指到 ``tmp_path``——本檔絕不寫進
真正的 goldens 資料夾。
"""

from __future__ import annotations

import pytest

from . import harness

_CHUNKS = ['data: {"x": 1}\n\n', "data: [DONE]\n\n"]
_GOLDEN_NAME = "fake__missing.json"


@pytest.fixture
def golden_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "_GOLDEN_DIR", tmp_path)
    return tmp_path


def _run(chunks: list[str] = _CHUNKS) -> BaseException | None:
    """呼叫 assert_golden，把 skip／fail 當回傳值（不讓本測試自己被 skip 掉）。"""
    try:
        harness.assert_golden("fake", "missing", chunks)
    except (pytest.skip.Exception, pytest.fail.Exception) as outcome:
        return outcome
    return None


def test_ci_unset_records_and_skips(golden_dir, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    outcome = _run()
    assert isinstance(outcome, pytest.skip.Exception)
    assert _GOLDEN_NAME in outcome.msg
    assert (golden_dir / _GOLDEN_NAME).exists()
    assert _run() is None  # 重跑即斷言：錄下的內容與輸入相同


@pytest.mark.parametrize("value", ["false", "0", ""])
def test_ci_false_records_and_skips(golden_dir, monkeypatch, value):
    monkeypatch.setenv("CI", value)
    outcome = _run()
    assert isinstance(outcome, pytest.skip.Exception)
    assert (golden_dir / _GOLDEN_NAME).exists()


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
def test_ci_true_missing_golden_fails_and_names_it(golden_dir, monkeypatch, value):
    monkeypatch.setenv("CI", value)
    outcome = _run()
    assert isinstance(outcome, pytest.fail.Exception)
    assert _GOLDEN_NAME in outcome.msg
    assert not (golden_dir / _GOLDEN_NAME).exists()  # CI 不錄製


def test_ci_true_existing_golden_still_compares(golden_dir, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    assert isinstance(_run(), pytest.skip.Exception)
    monkeypatch.setenv("CI", "true")
    assert _run() is None
    with pytest.raises(AssertionError, match="chunk #1"):
        _run([_CHUNKS[0], "data: changed\n\n"])
