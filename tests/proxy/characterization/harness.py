"""特徵化 golden-SSE 共用鷹架（react-core-extraction E0，拍板 K5）。

正規化＝假時鐘（monkeypatch 各 handler 模組的 ``get_current_epoch``）＋固定
request_id（ctx 注入）——SSE chunk 內的 created/telemetry 全數決定性。
mock 慣例沿用 ADR-0155 教訓：執行器先 put log（``has_tool_call`` 由 log 事件
置真）再 put sentinel，並以真的記錄函式登記試過、執行過、存成功（追蹤生產訊號，T3）。

golden 生命週期：缺檔＝錄製＋skip（重跑即斷言）；CI 上缺檔＝失敗（model-p1a U4）；
**刻意**行為變化＝刪 golden 檔重錄（diff 進 code review）。檔案 I/O 僅此兩站點
（測試基建，戰術性豁免）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.models.identity import AgentIdentity
from src.utils.tool_usage import record_tool_executed, record_tool_succeeded, record_tool_usage

_GOLDEN_DIR = Path(__file__).parent / "goldens"
_CI_TRUE_VALUES = frozenset({"1", "true", "yes"})

# 假時鐘步進：上游呼叫 +7s、工具執行 +5s（其餘站點 0）——與 ADR-0155 C4 紅測同款，
# 使 telemetry（pure_gen_ms/total_ms/e_index 的 gen_time 分量）決定性且語意可讀。
UPSTREAM_S = 7.0
TOOL_S = 5.0
CLOCK_START = 1_700_000_000.0


def make_clock() -> dict:
    """可變假時鐘（dict 容器供閉包推進）。"""
    return {"now": CLOCK_START}


def install_clock(monkeypatch, handler_module: str, clock: dict) -> None:
    """把 handler 模組＋其終端包裝 adapter 的 get_current_epoch 全釘到假時鐘。

    非決定性教訓（E0 錄製時 golden 自己抓到的）：claude/gemini 的最終 chunk 由
    ``convert_*_to_openai``（adapters 模組）組裝，其 ``created`` 用**該模組**匯入的
    get_current_epoch——只釘 handler 模組會漏真實時間進 golden。
    """
    monkeypatch.setattr(f"{handler_module}.get_current_epoch", lambda: clock["now"])
    for adapter_mod in (
        "src.adapters.claude_to_openai",
        "src.adapters.gemini_to_openai",
        # E1 起：ReAct 核心的 SSE chunk（created）與計時（C4）由 react_core 產生。
        "src.handlers.react_core",
    ):
        monkeypatch.setattr(f"{adapter_mod}.get_current_epoch", lambda: clock["now"])


def make_ctx(request_id: str, session_id: str, **extra) -> dict:
    """三 handler 共用的決定性 context。"""
    ctx = {
        "api_key": "fake-key",
        "gemini_api_key": "fake-key",
        "url": "http://fake-api.test",
        "provider": "OpenAI",
        "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
        "tool_registry": {},
        "memory_tool_registry": {},
        "plugin_registry": {},
        "request_id": request_id,
        "t0": CLOCK_START,
        "agent_id": "Sumi",
        "identity": AgentIdentity.for_role("Sumi", "Owner", capabilities=["*"]),
        "is_toukasoul": True,
        "session_id": session_id,
        "model": "golden-model",
    }
    ctx.update(extra)
    return ctx


def msg(role: str, content: str) -> SimpleNamespace:
    """物件式訊息（adapter getattr 分支＋telemetry 尾段 .content 屬性皆相容）。"""
    return SimpleNamespace(role=role, content=content, tool_calls=None)


def make_mock_executor(clock: dict):
    """生產同構的假執行器：推進工具時鐘、發 log 事件、真的記錄函式（執行成功）。"""

    async def mock_execute(name, args, req_id, ctx, queue, **kwargs):
        clock["now"] += TOOL_S
        used = kwargs.get("actually_used_tools")
        record_tool_usage(used, name, args)
        record_tool_executed(used, name, args)
        record_tool_succeeded(used, name, args)
        await queue.put({"type": "log", "content": f"[{name} done]"})
        await queue.put(None)
        return "Tool Success"

    return mock_execute


async def drain(gen, limit: int = 400) -> list[str]:
    """抽乾 handler async generator（安全閥防無限迴圈）。"""
    chunks: list[str] = []
    async for c in gen:
        chunks.append(str(c))
        if len(chunks) > limit:
            raise AssertionError(f"chunk 數超過安全閥 {limit}：疑似無限迴圈")
    return chunks


def _is_ci() -> bool:
    """CI 環境（GitHub Actions 預設 CI=true）：小寫後屬於 {"1","true","yes"}。"""
    value = os.environ.get("CI", "")  # noqa: ENV_SSOT_VIOLATION - CI 門檻，非產品設定
    return value.lower() in _CI_TRUE_VALUES


def assert_golden(provider: str, scenario: str, chunks: list[str]) -> None:
    """與 golden 比對（逐位元）；缺檔＝錄製＋skip，CI 上缺檔＝失敗。

    刻意行為變化＝刪 golden 檔重錄（diff 進 code review）。
    """
    _GOLDEN_DIR.mkdir(exist_ok=True)
    golden_path = _GOLDEN_DIR / f"{provider}__{scenario}.json"
    if not golden_path.exists():
        if _is_ci():
            pytest.fail(f"CI 缺 golden：{golden_path.name}（本機跑一次錄製後提交；CI 不錄製）")
        golden_path.write_text(
            json.dumps(chunks, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        pytest.skip(f"golden 已錄製：{golden_path.name}（重跑即斷言）")
    golden: list[str] = json.loads(
        golden_path.read_text(encoding="utf-8")
    )
    if chunks == golden:
        return
    # 可讀 diff：指出第一個分歧點
    for i, (got, want) in enumerate(zip(chunks, golden)):
        assert got == want, (
            f"[{provider}/{scenario}] chunk #{i} 分歧：\n--- golden ---\n{want}\n--- got ---\n{got}"
        )
    raise AssertionError(
        f"[{provider}/{scenario}] chunk 數量分歧：golden={len(golden)} got={len(chunks)}"
        f"（多出/缺少的首個：{(chunks[len(golden):] or golden[len(chunks):])[0]!r}）"
    )
