"""Responses 上游請求契約（3b-U4b-2；ADR-0248）。

錄製台包住 ResponsesAdapter 跑完整主迴圈：第 2 輪的 input 依序是使用者訊息、原樣回送的
加密 reasoning 與 function_call、以 call_id 對應的 function_call_output；store／include／
prompt_cache_key 每輪都在、串流旗標不進快照（D2）。期望值全部寫死。
"""

from __future__ import annotations

from src.adapters.schemas import OpenAIChatRequest
from src.handlers.adapters.responses_adapter import ResponsesAdapter

from .harness import drive

_TOOL = {"type": "function", "function": {
    "name": "vault_tools", "description": "檔案",
    "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
}}
_REASONING = {"type": "reasoning", "id": "rs_1", "encrypted_content": "gAAAAABfake==",
              "summary": [{"type": "summary_text", "text": "先查"}]}
_CALL = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "vault_tools",
         "arguments": '{"action": "list"}', "status": "completed"}
_DONE = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": "好了", "annotations": []}]}
_USAGE = {"input_tokens": 10, "output_tokens": 5}


def _response(*items: dict) -> dict:
    return {"id": "resp", "object": "response", "status": "completed", "model": "gpt-6-astra",
            "output": list(items), "usage": _USAGE}


async def test_tool_round_replays_items_and_answers_by_call_id(monkeypatch):
    request = OpenAIChatRequest(model="gpt-6-astra", tools=[_TOOL],
                                messages=[{"role": "user", "content": "查一下"}])
    run = await drive(monkeypatch, ResponsesAdapter(stream=True), request,
                      [_response(_REASONING, _CALL), _response(_DONE)], tag="responses-tool")
    first, second = (snap["kwargs"] for snap in run.snapshots)
    assert first["input"] == [{"role": "user", "content": "查一下"}]
    assert second["input"] == [
        {"role": "user", "content": "查一下"}, _REASONING, _CALL,
        {"type": "function_call_output", "call_id": "call_1", "output": "Tool Success"},
    ]
    for kwargs in (first, second):
        assert (kwargs["store"], kwargs["include"], "stream" in kwargs) == (
            False, ["reasoning.encrypted_content"], False)
        assert kwargs["prompt_cache_key"] == first["prompt_cache_key"]
    assert '"content": "好了"' in "".join(run.chunks)
