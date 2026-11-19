"""Phase 4（提案 §9 需求 3）：耐久可續交付——TDD.

``DispatchCallbackStore`` 是純記憶體 asyncio.Event——重啟即失、正在等的 await 掛不回
（結果雖已算出）。Phase 4 補：deliver 時**雙寫**耐久落盤（ADR-0324 起在 data 底下的系統暫存
``temp/dispatch_results/<雜湊>.json``，代理人碰不到），await 改**耐久優先**——
已交付（含跨重啟）即直接重讀、不空等到 TIMEOUT。純 store 仍記憶體、耐久落在
subagent 層（分離：callback store 不耦合 identity/drive）。
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.services import subagent_results as sr
from src.services.dispatch_callback_store import _reset_callback_store, get_callback_store
from src.services.resource_manager import resource_manager
from src.services.subagent_results import persist_result, read_result
from src.tools import subagent_tools as st
from src.tools.mission_tools import mission_tools


@pytest.fixture(autouse=True)
def _fresh_store():
    _reset_callback_store()
    yield
    _reset_callback_store()


def _debug_identity() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")


# --------------------------------------------------------------------------- #
# 1. 耐久 store roundtrip
# --------------------------------------------------------------------------- #


def test_persist_and_read_roundtrip():
    identity = _debug_identity()
    payload = {"task_id": "t1", "status": "DONE", "result_summary": "ok"}
    persist_result(identity, "tok-abc", payload)
    got = read_result(identity, "tok-abc")
    assert got == payload


def test_read_missing_returns_none():
    assert read_result(_debug_identity(), "no-such-token") is None


# --------------------------------------------------------------------------- #
# 2. _run_and_deliver 雙寫（in-memory deliver ＋ 耐久落盤）
# --------------------------------------------------------------------------- #


async def test_run_and_deliver_dual_writes(monkeypatch):
    async def _fake_run(**_kw: Any) -> str:
        return "完成。\n```json\n{\"status\": \"done\", \"claims\": [\"x\"]}\n```"

    monkeypatch.setattr(st, "_run_subagent", _fake_run)

    identity = _debug_identity()
    store = get_callback_store()
    token = store.generate_token()
    store.register(token)

    await st._run_and_deliver(
        token=token,
        goal="g",
        parent_identity=identity,
        parent_request_id="req",
        base_ctx={},
        model=None,
        task_id="t9",
    )

    durable = read_result(identity, token)
    assert durable is not None
    assert durable["status"] == "DONE"
    assert durable["report_status"] == "done"


async def test_deliver_without_identity_skips_durable(monkeypatch):
    """parent_identity 缺失時不落耐久（無法解析 vault）——不炸、僅走記憶體。"""
    async def _fake_run(**_kw: Any) -> str:
        return "ok"

    monkeypatch.setattr(st, "_run_subagent", _fake_run)
    store = get_callback_store()
    token = store.generate_token()
    store.register(token)
    # 不應拋出
    await st._run_and_deliver(
        token=token,
        goal="g",
        parent_identity=None,
        parent_request_id="req",
        base_ctx={},
        model=None,
        task_id=None,
    )


# --------------------------------------------------------------------------- #
# 3. await_dispatch_result 耐久優先（模擬重啟：記憶體 Event 沒了、耐久還在）
# --------------------------------------------------------------------------- #


async def test_await_reads_durable_when_event_lost():
    """模擬重啟：token 未在（記憶體 store 空），但耐久有結果 → 不空等、直接重讀。"""
    identity = _debug_identity()
    # 沒有 register（記憶體 store 全新＝重啟後狀態）
    persist_result(
        identity,
        "tok-restart",
        {
            "task_id": "t1",
            "status": "DONE",
            "result_summary": "重啟前就算完了",
            "report_status": "done",
            "claims": ["A"],
            "artifact_refs": ["a.md"],
            "structured": True,
        },
    )
    out = str(
        await mission_tools(
            action="await_dispatch_result",
            callback_token="tok-restart",
            timeout=2,
            identity=identity,
        )
    )
    assert "重啟前就算完了" in out
    assert "A" in out  # 結構化 claims 也渲染


async def test_await_unknown_token_without_durable_still_reports():
    """既無記憶體 token 又無耐久＝真未知：回既有 UNKNOWN_TOKEN 提示（不假裝有結果）。"""
    out = str(
        await mission_tools(
            action="await_dispatch_result",
            callback_token="ghost",
            timeout=1,
            identity=_debug_identity(),
        )
    )
    assert "Unknown" in out or "unknown" in out


# --------------------------------------------------------------------------- #
# 4. E77（ADR-0178）：callback_token 路徑穿越回歸
#
# callback_token 是 mission_tools(await_dispatch_result) 的模型可控輸入。ADR-0324 起檔名是
# 「代理人編號＋憑證」的 SHA-256，敵意 token 在構造上就逃不出結果資料夾；以下釘死這個邊界。
# --------------------------------------------------------------------------- #


_TRAVERSAL_TOKENS = [
    "../../../../etc/passwd",        # dots + forward slashes
    "..\\..\\..\\windows\\system32",  # backslashes
    "/etc/shadow",                   # 絕對路徑前綴
    "tok/../../escape",              # 中段穿越
    "....//....//secret",            # 折疊點序列
]


@pytest.mark.parametrize("token", _TRAVERSAL_TOKENS)
def test_token_path_never_escapes_dispatch_results(token):
    """任何敵意 token 解析後都必須是結果資料夾的**直接子檔**，檔名只有雜湊。"""
    path = sr._result_path(_debug_identity(), token)
    assert path.parent == sr._results_dir()
    assert re.fullmatch(r"[0-9a-f]{64}\.json", path.name)


def test_read_traversal_token_cannot_exfil_decoy():
    """arbitrary-file-read 原語回歸：token 設計成「若直接拼路徑」會解析到資料夾外的誘餌。"""
    decoy = sr._results_dir().parent / "decoy.json"
    resource_manager.write_json(decoy, {"secret": "cross-vault-data"})
    try:
        assert read_result(_debug_identity(), "../decoy") is None
    finally:
        resource_manager.remove_file(decoy, missing_ok=True)


def test_persist_traversal_token_cannot_overwrite_outside():
    """persist 側對稱：敵意 token 的落盤不得逃出結果資料夾覆寫外部 .json。"""
    identity = _debug_identity()
    escape_target = sr._results_dir().parent / "pwned.json"
    resource_manager.remove_file(escape_target, missing_ok=True)

    persist_result(identity, "../pwned", {"x": 1})

    assert not resource_manager.exists(escape_target)
    landed = sr._result_path(identity, "../pwned")
    assert landed.parent == sr._results_dir()
    assert resource_manager.exists(landed)
