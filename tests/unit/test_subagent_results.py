"""Phase 4（提案 §9 需求 3）：耐久可續交付——TDD.

``DispatchCallbackStore`` 是純記憶體 asyncio.Event——重啟即失、正在等的 await 掛不回
（結果雖已算出）。Phase 4 補：deliver 時**雙寫**耐久落盤（requester 的 personal
vault 下 ``dispatch_results/<token>.json``），await 改**耐久優先**——
已交付（含跨重啟）即直接重讀、不空等到 TIMEOUT。純 store 仍記憶體、耐久落在
subagent 層（分離：callback store 不耦合 identity/drive）。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import pytest
from src.models.identity import AgentIdentity
from src.services import subagent_results as sr
from src.services.dispatch_callback_store import _reset_callback_store, get_callback_store
from src.services.resource_manager import resource_manager
from src.services.subagent_results import persist_result, read_result
from src.tools import subagent_tools as st
from src.tools.mission_tools import mission_tools

from tests.support.links import LINK_FOLLOWED, make_dir_link

if TYPE_CHECKING:
    from pathlib import Path


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
# 4. E77（ADR-0178）：callback_token 路徑穿越 sanitize 回歸
#
# callback_token 是 mission_tools(await_dispatch_result) 的模型可控輸入；
# _token_path 的 _TOKEN_SAFE_RE.sub 把它變成一段名稱，區內路徑（ADR-0293）的 child 再拒絕
# 不能當一段的名稱。
# 既有 8 測只餵安全字元 token，移 sanitize／窄化 regex 全綠＝arbitrary-file-read
# 原語靜默出貨。以下敵意 token 釘死監獄邊界。
# --------------------------------------------------------------------------- #


_TRAVERSAL_TOKENS = [
    "../../../../etc/passwd",        # dots + forward slashes
    "..\\..\\..\\windows\\system32",  # backslashes（殺「只清 / 留 \\」窄化）
    "/etc/shadow",                   # 絕對路徑前綴
    "tok/../../escape",              # 中段穿越
    "....//....//secret",            # 折疊點序列
]


@pytest.mark.parametrize("token", _TRAVERSAL_TOKENS)
def test_token_path_never_escapes_dispatch_results(token):
    """任何敵意 token 解析後都必須是 dispatch_results/ 的**直接子檔**（監獄邊界）。

    移 sanitize（safe=token[:128]）或窄化 regex 讓分隔符倖存 → child 拒絕這一段 → 紅。
    """
    identity = _debug_identity()
    path = sr._token_path(identity, token)
    assert path is not None
    assert path.parent == sr._results_dir(identity)  # 就在該層、非往上逃逸
    assert re.fullmatch(r"[A-Za-z0-9_-]+\.json", path.name)


def test_read_traversal_token_cannot_exfil_decoy():
    """arbitrary-file-read 原語回歸：token 設計成「若不清洗」會解析到監獄外的誘餌
    （模擬他人 vault／秘密檔）。sanitize 在位 → 讀不到（read 端會把結果渲染回模型
    ＝跨 vault 外洩，故這是最危險的一半）。移 sanitize → 讀出誘餌 → 紅。"""
    identity = _debug_identity()
    # 監獄外一層（<vault>/decoy.json）＝ dispatch_results/../decoy.json 的逃逸目標
    decoy = sr._results_dir(identity).parent.child("decoy.json")
    resource_manager.zone_write_text(decoy, '{"secret": "cross-vault-data"}')

    assert read_result(identity, "../decoy") is None  # 清洗後指向監獄內不存在檔


def test_persist_traversal_token_cannot_overwrite_outside():
    """persist 側對稱：敵意 token 的落盤（原子寫入）不得逃出 dispatch_results/
    覆寫外部 .json。清洗後就地寫成扁平檔名；移 sanitize → 逃逸目標被建立 → 紅。"""
    identity = _debug_identity()
    results_dir = sr._results_dir(identity)
    escape_target = results_dir.parent.child("pwned.json")
    resource_manager.zone_remove_file(escape_target)

    persist_result(identity, "../pwned", {"x": 1})

    assert resource_manager.zone_kind(escape_target) == "missing"  # 監獄外未被寫入
    # 精確定位清洗後的落點（不依賴目錄為空——pytest-randomly 下同模組他測可能留檔）
    landed = sr._token_path(identity, "../pwned")
    assert landed.parent == results_dir  # 就地扁平落檔
    assert resource_manager.zone_kind(landed) == "file"


# --------------------------------------------------------------------------- #
# 5. ADR-0293：dispatch_results/ 或個人空間被換成連結時不跟隨（best-effort：記警告）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("swapped", ["drive", "results"])
def test_a_linked_results_folder_is_not_followed(
    zone_data_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture, swapped: str
):
    """連結另一端的結果讀不到、也不會被寫；兩邊都只記警告，不丟出例外。"""
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    drive = zone_data_root / "personal_drive" / "Alice"
    drive.mkdir(parents=True)
    fake = tmp_path.resolve() / "fake"
    planted = fake / "dispatch_results" if swapped == "drive" else fake
    planted.mkdir(parents=True)
    (planted / "tok.json").write_text('{"status": "DONE", "result_summary": "CANARY"}', "utf-8")
    before = sorted(p.name for p in planted.iterdir())
    if swapped == "drive":
        drive.rename(drive.with_name("Alice_old"))
        make_dir_link(drive, fake)
    else:
        make_dir_link(drive / "dispatch_results", fake)

    with caplog.at_level(logging.WARNING, logger=sr.__name__):
        assert read_result(identity, "tok") is None, LINK_FOLLOWED
        persist_result(identity, "new", {"x": 1})
    assert sorted(p.name for p in planted.iterdir()) == before, LINK_FOLLOWED
    assert sum(r.name == sr.__name__ for r in caplog.records) == 2
