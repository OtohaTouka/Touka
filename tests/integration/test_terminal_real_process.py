"""批⑤-D E70／E71（ADR-0120／0125）：_execute_core 真子進程逾時——殺樹接線＋增量寫入。

既有 execute_command／background／hitl 測試**整支 mock 掉 _execute_core**（AsyncMock／
_blocking_core／_fake_core）→ 逾時分支從不真跑；test_process_group_kill 直呼
terminate_process_tree 證本體有效但繞過「逾時偵測→殺樹」接線（家族③）。
訊號測試餵合成 record → 逐行增量寫入（stalled/runaway 唯一活資料源）亦無測。

本檔以**真 shell 子進程**（`sleep`/`echo` 在 powershell 與 bash 同語法）驅動
_execute_core 本體，不 mock。前例：test_process_group_kill。
"""

from __future__ import annotations

import inspect

from src.tools import terminal

# `sleep N` 在 powershell（Start-Sleep 別名）與 /bin/bash 同語法；`echo` 亦同。
_SLEEPER = "sleep 30"
_ECHO_THEN_HANG = "echo L1; sleep 30"


async def test_execute_core_timeout_triggers_process_tree_kill(tmp_path, monkeypatch):
    """E70：逾時 → terminate_process_tree(proc) 真的被接上（連坐殺孫進程防孤兒）。

    變異刪 :332-336 殺樹分支（只留 timed-out 訊息）→ killed 從未捕獲 → 紅。
    """
    killed: dict[str, object] = {}
    real_kill = terminal.terminate_process_tree

    def _spy_kill(proc):
        killed["proc"] = proc
        real_kill(proc)  # 真殺——測試不留孤兒

    monkeypatch.setattr(terminal, "terminate_process_tree", _spy_kill)

    exit_code, output, _ms = await terminal._execute_core(
        _SLEEPER, "powershell", tmp_path, None, timeout=1
    )

    assert exit_code == -1
    assert "timed out after 1 seconds" in output
    assert killed.get("proc") is not None  # 逾時偵測→殺樹接線真的走過


async def test_execute_core_streams_output_incrementally_before_timeout(tmp_path):
    """E71：逐行增量寫入 record——輸出在進程「完成前」就被捕獲（訊號活資料源）。

    子進程印一行後長眠、從未正常結束（逾時殺掉）——L1 仍在 output 與 record 中
    ＝增量捕獲的直接證明。變異刪 :321-323 record 增量寫入 → record 空 → 紅。
    """
    record: dict[str, object] = {}

    exit_code, output, _ms = await terminal._execute_core(
        _ECHO_THEN_HANG, "powershell", tmp_path, None, timeout=5, record=record
    )

    assert exit_code == -1
    assert "L1" in output  # 逾時前已捕獲的增量輸出
    assert "timed out after 5 seconds" in output
    assert "L1" in str(record.get("output", ""))  # 逐行寫入 record（get_command_result 資料源）
    assert record.get("last_output_at")  # 停滯偵測時間戳有更新


def test_timeout_defaults_pinned():
    """兩層看門狗預設值釘死：前景 30s／背景 backstop 600s（改 1 或天文數字皆紅）。"""
    assert terminal._FOREGROUND_TIMEOUT == 30
    assert terminal._BACKGROUND_TIMEOUT == 600
    sig = inspect.signature(terminal._execute_core)
    assert sig.parameters["timeout"].default == terminal._FOREGROUND_TIMEOUT
