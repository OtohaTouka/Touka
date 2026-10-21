"""ADR-0069 Phase D — ModelJudge 單元測試（注入假的 Completer；解析/clamp/fail-safe/單次呼叫；
啟用但沒設評分模型時不啟用）。ADR-0244 起評分走 complete_once。"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import src.main as main_module
from src.services.completion import CompletionError, CompletionResult, complete_once
from src.services.creation import model_judge
from src.services.creation.model_judge import ModelJudge
from src.services.creation.models import EvalTask

_TASKS = [EvalTask(id="t1", title="T", expectation="e")]


class _Completer:
    def __init__(self, text: str = "", *, reason: str | None = None,
                 raises: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._text, self._reason, self._raises = text, reason, raises

    async def __call__(self, *args) -> CompletionResult:
        self.calls.append(args)
        if self._raises is not None:
            raise self._raises
        error = (
            None if self._reason is None
            else CompletionError("upstream_error", self._reason, "x")
        )
        return CompletionResult(text=self._text, usage={}, cost_usd=0.0,
                                stop_reason="end" if error is None else "error", error=error)


async def test_score_parses_float():
    assert await ModelJudge(_Completer("0.82"), "m").score(_TASKS, "md") == 0.82


async def test_score_clamps_and_unparseable():
    assert await ModelJudge(_Completer("1.5"), "m").score(_TASKS, "md") == 1.0
    assert await ModelJudge(_Completer("great job!"), "m").score(_TASKS, "md") == 0.0
    assert await ModelJudge(_Completer("-0.3"), "m").score(_TASKS, "md") == 0.0


async def test_score_exception_failsafe():
    assert await ModelJudge(_Completer(raises=RuntimeError("boom")), "m").score(_TASKS, "md") == 0.0


@pytest.mark.parametrize("reason", ["over_budget", "timeout", "max_tokens", "refusal"])
async def test_score_unsuccessful_completion_failsafe(reason):
    """非成功（即使夾帶分數文字）一律 0.0。"""
    assert await ModelJudge(_Completer("0.9", reason=reason), "m").score(_TASKS, "md") == 0.0


async def test_score_calls_completer_once_bounded():
    completer = _Completer("0.5")
    await ModelJudge(completer, "m").score(_TASKS, "md")
    assert len(completer.calls) == 1
    assert completer.calls[0][1:3] == ("judge", "m")
    assert completer.calls[0][5] == 16


# --- 評分沒設模型就不啟用（不再有寫死的評分模型預設）---


def _judge_model_env_reads() -> list[ast.Call]:
    """main.py 中所有 `os.environ.get("TOUKA_EVAL_JUDGE_MODEL", ...)` 呼叫。"""
    tree = ast.parse(Path(main_module.__file__).read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "TOUKA_EVAL_JUDGE_MODEL"
    ]


def test_main_reads_judge_model_without_hardcoded_default():
    """啟用評分但沒設模型時，main.py 不得以寫死的模型名當預設值。"""
    reads = _judge_model_env_reads()
    assert reads, "main.py 應仍由環境變數讀取評分模型（禁令 42：env 讀取留在 main.py）"
    assert all(len(call.args) == 1 and not call.keywords for call in reads)


@pytest.mark.parametrize(
    ("enabled", "model", "expected"),
    [
        ("true", None, (True, None)),
        ("true", "   ", (True, None)),
        ("TRUE", "claude-haiku-4-5", (True, "claude-haiku-4-5")),
        ("1", " claude-haiku-4-5 ", (True, "claude-haiku-4-5")),
        ("yes", "claude-haiku-4-5", (True, "claude-haiku-4-5")),
        ("false", "claude-haiku-4-5", (False, None)),
        ("", "claude-haiku-4-5", (False, None)),
        (None, None, (False, None)),
    ],
)
def test_resolve_judge_model(enabled, model, expected):
    """純函式：只有旗標為真且明確指定模型才回模型名。"""
    assert model_judge.resolve_judge_model(enabled, model) == expected


def test_configure_judge_enabled_without_model_stays_off(monkeypatch):
    """啟用但沒設模型 → 不 set_judge，並記錄錯誤。"""
    runner, log = MagicMock(), MagicMock()
    monkeypatch.setattr(main_module, "eval_runner", runner)
    monkeypatch.setattr(main_module, "logger", log)
    main_module._configure_eval_judge("true", None)
    runner.set_judge.assert_not_called()
    log.error.assert_called_once()
    assert "TOUKA_EVAL_JUDGE_MODEL" in log.error.call_args.args[0]


def test_configure_judge_uses_explicit_model(monkeypatch):
    """啟用且指定模型 → 以該模型注入 ModelJudge。"""
    runner, log = MagicMock(), MagicMock()
    monkeypatch.setattr(main_module, "eval_runner", runner)
    monkeypatch.setattr(main_module, "logger", log)
    main_module._configure_eval_judge("true", "claude-haiku-4-5")
    runner.set_judge.assert_called_once()
    judge = runner.set_judge.call_args.args[0]
    assert isinstance(judge, ModelJudge)
    assert judge._model == "claude-haiku-4-5"
    assert judge._completer is complete_once  # 生產走 complete_once（記帳＋成本閘）
    log.error.assert_not_called()


def test_configure_judge_disabled_is_silent(monkeypatch):
    """沒啟用 → 不注入、不記錯誤。"""
    runner, log = MagicMock(), MagicMock()
    monkeypatch.setattr(main_module, "eval_runner", runner)
    monkeypatch.setattr(main_module, "logger", log)
    main_module._configure_eval_judge("false", "claude-haiku-4-5")
    runner.set_judge.assert_not_called()
    log.error.assert_not_called()
