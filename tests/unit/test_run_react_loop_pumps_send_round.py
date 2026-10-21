"""G17（守衛涵蓋缺口待辦）— run_react_loop 必須把每個 send_round 經 pump_send_round 抽乾。

缺口背景（見 docs/architecture/proposals/guard-coverage-gap-backlog.md · G17）：
    ADR-0157 K1：keepalive 心跳升核心單一實作（pump_send_round）；`run_react_loop`
    必須把**每個** adapter 的 send_round 都經 pump_send_round 抽乾（單一 call site），
    三家慢上游（openai 長 POST／gemini 503 退避／claude SDK 呼叫）才同享心跳。

    此不變式**先前零測試**：唯一涉 pump 的 test_core_keepalive 只隔離測 pump 本體
    （直呼 pump，非經 run_react_loop）；golden 在即時 mock 下心跳不觸發（拿掉 line 165
    包裹仍逐位元相同）。若重構把包裹拿掉或新增旁路，心跳靜默消失、前端久候斷線回歸。

源碼守衛：run_react_loop 內每個 `adapter.send_round(` 呼叫都必須被 `pump_send_round(`
直接包裹（移除包裹或新增未包裹旁路皆變紅）。
"""

from __future__ import annotations

import inspect

from src.handlers.react_core import run_react_loop


def test_run_react_loop_wraps_every_send_round_in_pump():
    src = inspect.getsource(run_react_loop)
    n_send = src.count("adapter.send_round(")
    n_pumped = src.count("pump_send_round(adapter.send_round(")

    assert n_send >= 1, "run_react_loop 未呼叫 adapter.send_round（核心迴圈斷裂？）"
    assert n_send == n_pumped, (
        f"run_react_loop 有 {n_send - n_pumped} 處未經 pump_send_round 包裹的 "
        "adapter.send_round＝keepalive 心跳旁路（三家慢上游前端久候斷線回歸；ADR-0157／G17）"
    )
