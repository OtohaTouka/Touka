import json

import pytest
from src.adapters.gemini_to_openai import convert_gemini_to_openai


def test_convert_gemini_to_openai_normal():
    """驗證正常的 Gemini 回應可以被正確轉換為 OpenAI 格式。"""
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Hello, how can I help you today?"},
                        {
                            "functionCall": {
                                "name": "get_weather",
                                "args": {"location": "Taipei", "unit": "celsius"}
                            }
                        }
                    ]
                },
                "finishReason": "STOP"
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 15,
            "candidatesTokenCount": 8,
            "totalTokenCount": 23
        }
    }

    result = convert_gemini_to_openai(response)

    # 驗證結構
    assert result["object"] == "chat.completion"
    choice = result["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert "Hello, how can I help you today?" in choice["message"]["content"]
    
    # 驗證工具呼叫
    tool_call = choice["message"]["tool_calls"][0]
    assert tool_call["type"] == "function"
    assert tool_call["function"]["name"] == "get_weather"
    
    # 驗證 arguments 是否已轉成合法的 JSON 字串
    args = json.loads(tool_call["function"]["arguments"])
    assert args["location"] == "Taipei"
    assert args["unit"] == "celsius"


def test_convert_gemini_to_openai_rce_prevention():
    """TDD: 驗證 convert_gemini_to_openai 能夠安全防禦惡意參數中的 RCE 注入。"""
    
    # 惡意參數：利用 set 表達式繞過字典包裝，觸發 eval 執行 sys.exit(99)
    # 這繼承自 BaseException，因此不會被 except Exception: pass 吞掉，會直接中斷進程。
    malicious_payload = "{ __import__('sys').exit(99) }"
    
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "exploit_tool",
                                "args": malicious_payload  # args 為惡意字串，內含 {} 觸發 eval
                            }
                        }
                    ]
                },
                "finishReason": "STOP"
            }
        ]
    }

    # 執行 convert_gemini_to_openai。
    # 如果是舊有實作（使用 eval），將會觸發 sys.exit(99) 導致 pytest 異常中斷。
    # 如果修復成功，則會順利執行且不會中斷。
    # 為了測試的健全性，如果真的執行了並拋出 SystemExit，我們在測試中捕獲它。
    try:
        result = convert_gemini_to_openai(response)
        
        # 即使被 try-except 擋住了，我們仍然需要確保 arguments 沒有真的被 eval 執行。
        # 也就是 arguments 欄位不應該被轉換為 dict 的 json 字串形式。
        choice = result["choices"][0]
        tool_call = choice["message"]["tool_calls"][0]
        args_str = tool_call["function"]["arguments"]
        
        # 確保安全防護下，其值依然保持為原本的字串 JSON 序列化形式。
        assert json.loads(args_str) == malicious_payload
        
    except SystemExit as e:
        pytest.fail(f"🔴 RCE 漏洞被觸發！程式碼執行了 sys.exit({e.code})")
