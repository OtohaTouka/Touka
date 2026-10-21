"""ADR-0057: Pydantic V2 .copy() 棄用修復 — 結構性測試

Red → Green 確認：
- group.py 不得對 PipelineContext 呼叫已棄用的 .copy()
- group.py 必須 import PipelineContext 並使用 .model_copy()
- PipelineContext.model_copy() 不觸發 Pydantic 棄用警告
"""
import warnings
from pathlib import Path

from src.models.context import PipelineContext

ROOT = Path(__file__).parent.parent.parent
GROUP_PY = ROOT / "ToukaProxy" / "src" / "services" / "dispatchers" / "group.py"


def test_group_py_no_pydantic_copy():
    """group.py 不得使用已棄用的 Pydantic .copy() 方法"""
    content = GROUP_PY.read_text(encoding="utf-8")
    assert "context.copy()" not in content, (
        "group.py 仍在對 PipelineContext 呼叫已棄用的 .copy()，"
        "請改用 .model_copy()"
    )


def test_group_py_imports_pipeline_context():
    """group.py 必須 import PipelineContext 以提供正確型別標註"""
    content = GROUP_PY.read_text(encoding="utf-8")
    assert "PipelineContext" in content, (
        "group.py 必須 import PipelineContext"
    )


def test_group_py_uses_model_copy():
    """group.py 必須對 context 呼叫 .model_copy()"""
    content = GROUP_PY.read_text(encoding="utf-8")
    assert "context.model_copy()" in content, (
        "group.py 必須使用 context.model_copy() 而非 context.copy()"
    )


def test_pipeline_context_model_copy_no_deprecation_warning():
    """PipelineContext.model_copy() 不應觸發 Pydantic 棄用警告"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ctx = PipelineContext()
        _ = ctx.model_copy()

    pydantic_warnings = [
        w for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "copy" in str(w.message).lower()
    ]
    assert len(pydantic_warnings) == 0, (
        f"model_copy() 仍觸發棄用警告：{[str(w.message) for w in pydantic_warnings]}"
    )
