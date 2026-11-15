"""重建工具用哪個嵌入模型（ADR-0315）.

沒給 ``--model`` 用設定的（環境或已載入的設定檔），並印出模型與來源；給了和設定不同的照跑、印警告。
工具在 scripts/（不是套件），用 importlib 載入；事實庫、向量庫與重建都換成替身，不載入嵌入模型、
不碰真的 vault 與 data/。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_TOOL = Path(__file__).resolve().parents[2] / "ToukaMemory" / "scripts" / "reembed_memory.py"
_spec = importlib.util.spec_from_file_location("reembed_memory", _TOOL)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

E5 = "intfloat/multilingual-e5-base"
MINILM = "all-MiniLM-L6-v2"


class _Factual:
    def query_all_facts(self, **_options: Any) -> list[dict[str, Any]]:
        return [{"agent_id": "Alice"}]


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Replace the stores and the rebuild; record which model the vector store was built with."""
    models: list[str] = []

    def vector_store(*, base_dir: str, model_name: str) -> object:
        models.append(model_name)
        return object()

    monkeypatch.setattr(tool, "open_factual_store_for_reembed", lambda _vault: _Factual())
    monkeypatch.setattr(tool, "VectorStore", vector_store)
    monkeypatch.setattr(tool, "reembed_agent", lambda _factual, _vectors, _identity: 1)
    monkeypatch.setattr(sys, "argv", ["reembed_memory.py", str(tmp_path / "vault")])
    return models


def _run(monkeypatch: pytest.MonkeyPatch, *extra: str) -> None:
    monkeypatch.setattr(sys, "argv", [*sys.argv, *extra])
    tool.main()


def test_without_model_uses_the_configured_one(
    built: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL", E5)

    _run(monkeypatch)

    out = capsys.readouterr().out
    assert built == [E5]
    assert f"model = {E5}" in out
    assert "EMBEDDING_MODEL" in out
    assert "[WARN]" not in out


def test_without_any_setting_uses_the_built_in_default(
    built: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

    _run(monkeypatch)

    assert built == [MINILM]
    assert "default" in capsys.readouterr().out


def test_a_different_model_runs_with_a_warning(
    built: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL", MINILM)

    _run(monkeypatch, "--model", E5)

    out = capsys.readouterr().out
    assert built == [E5]
    assert "--model" in out
    assert "[WARN]" in out
    assert MINILM in out


def test_the_same_model_in_another_spelling_has_no_warning(
    built: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL", MINILM)

    _run(monkeypatch, "--model", "sentence-transformers/all-minilm-l6-v2")

    assert "[WARN]" not in capsys.readouterr().out
